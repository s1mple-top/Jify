"""WebSocket 流式控制台 —— 接口对齐 CLIConsole，通过队列桥接同步/异步。"""

import json as _json
import queue
import time
import concurrent.futures
from typing import Dict, Optional, Any

from fastapi import WebSocket
from jify_tool import registry as jf_registry


class WebSocketConsole:
    """对齐 CLIConsole 接口，可由 AgentLoop.run() 同步调用。

    消息通过 _outgoing 队列产出，由 asyncio 侧 drain_outgoing() 异步推送到 WebSocket。
    """

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.token_num = 0
        self.last_reasoning_content = ""
        self.total_tokens_sent = 0
        self.total_tokens_recv = 0
        self._stream_buffer = ""
        self._model_phase = "idle"
        self._outgoing: queue.Queue = queue.Queue()

    # AgentLoop.run() 要求的接口

    def start_round(self):
        pass

    def stop_live(self):
        pass

    def finalize(self):
        self._stream_buffer = ""
        self.last_reasoning_content = ""

    def stream_end(self):
        pass

    def flush_stream(self, is_final: bool = False):
        pass

    # 消息队列桥接

    def _send(self, msg: dict):
        self._outgoing.put(msg)

    async def drain_outgoing(self):
        """一次排空所有积压消息（由 asyncio 侧定期调用）"""
        while True:
            try:
                msg = self._outgoing.get_nowait()
                await self.ws.send_json(msg)
            except queue.Empty:
                break

    # 同步 consume_stream（接口对齐 CLIConsole）

    def consume_stream(self, response, interrupt_event=None):
        tool_call_chunks: Dict[int, Dict] = {}
        finish_reason = ""
        pre_results: Dict[str, Any] = {}
        pending_futures: Dict[str, concurrent.futures.Future] = {}
        _fired_indices: set = set()
        fired_signatures: set = set()
        tc_names: Dict[str, str] = {}
        tc_args: Dict[str, dict] = {}
        tc_id_to_idx: Dict[str, int] = {}
        tool_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8)
        complete_text = ""
        reasoning_text = ""

        self._send({"type": "thinking_start"})

        try:
            _chunk_iter = iter(response)
        except TypeError:
            _chunk_iter = response

        last_seen_idx = -1

        try:
            for chunk in _chunk_iter:
                if interrupt_event is not None and interrupt_event.is_set():
                    break

                from tools.approval import break_requested
                if break_requested.is_set():
                    break

                if not hasattr(chunk, "tool_call_deltas"):
                    continue

                if chunk.content:
                    complete_text += chunk.content
                    self.total_tokens_recv += len(chunk.content)
                    self._send({"type": "text_chunk", "content": chunk.content})

                if chunk.thinking:
                    reasoning_text += chunk.thinking
                    self.last_reasoning_content += chunk.thinking
                    self._send({"type": "thinking", "content": chunk.thinking})

                if chunk.tool_call_deltas:
                    for tc in chunk.tool_call_deltas:
                        idx = tc.index
                        if idx not in tool_call_chunks:
                            tool_call_chunks[idx] = {
                                "id": "",
                                "function": {"name": "", "arguments": ""},
                            }
                        if tc.id:
                            tool_call_chunks[idx]["id"] = tc.id
                        if tc.name:
                            tool_call_chunks[idx]["function"]["name"] = tc.name
                        if tc.arguments:
                            tool_call_chunks[idx]["function"]["arguments"] += tc.arguments

                    current_indices = {tc.index for tc in chunk.tool_call_deltas}
                    for cidx in list(tool_call_chunks.keys()):
                        if cidx not in current_indices and cidx not in _fired_indices:
                            self._fire_tool(cidx, tool_call_chunks, pending_futures,
                                            pre_results, tool_executor,
                                            tc_names, tc_args, tc_id_to_idx,
                                            fired_signatures)
                            _fired_indices.add(cidx)

                elif last_seen_idx >= 0 and pending_futures:
                    for cidx in list(tool_call_chunks.keys()):
                        if cidx not in _fired_indices:
                            self._fire_tool(cidx, tool_call_chunks, pending_futures,
                                            pre_results, tool_executor,
                                            tc_names, tc_args, tc_id_to_idx,
                                            fired_signatures)
                            _fired_indices.add(cidx)

                last_seen_idx = max(tool_call_chunks.keys()) if tool_call_chunks else -1

                if chunk.finish_reason:
                    finish_reason = chunk.finish_reason

        except Exception:
            pass

        # 收尾：触发剩余未处理的 tool chunk
        from tools.approval import break_requested
        if not break_requested.is_set():
            for cidx in list(tool_call_chunks.keys()):
                if cidx not in _fired_indices:
                    self._fire_tool(cidx, tool_call_chunks, pending_futures,
                                    pre_results, tool_executor,
                                    tc_names, tc_args, tc_id_to_idx,
                                    fired_signatures)
                    _fired_indices.add(cidx)

        # 等待所有预执行完成
        for tc_id, future in pending_futures.items():
            if tc_id in pre_results:
                continue
            try:
                tid, raw, err = future.result(timeout=30)
                if err:
                    self._send({"type": "tool_error", "tool_id": tid, "error": err})
                else:
                    self._send({"type": "tool_result", "tool_id": tid, "result": raw})
                pre_results[tid] = raw
            except Exception as e:
                pre_results[tc_id] = {"error": str(e)}

        tool_executor.shutdown(wait=False)

        return complete_text, tool_call_chunks, finish_reason, pre_results, fired_signatures

    # 流式预执行 _fire_tool（对齐 CLIConsole）

    def _fire_tool(self, idx, chunks, pending, pre, executor,
                   tc_names=None, tc_args=None, tc_id_to_idx=None,
                   fired_signatures=None):
        from tools.approval import break_requested
        if break_requested.is_set():
            return

        tc = chunks[idx]
        tc_id = tc.get("id") or f"call_{idx}"
        name = tc.get("function", {}).get("name", "")
        args_str = tc.get("function", {}).get("arguments", "{}")

        if not name or tc_id in pending:
            return

        if tc_names is not None:
            tc_names[tc_id] = name
        if tc_id_to_idx is not None:
            tc_id_to_idx[tc_id] = idx

        try:
            args = _json.loads(args_str) if args_str else {}
        except Exception:
            args = {}

        if tc_args is not None:
            tc_args[tc_id] = args

        self._send({"type": "tool_start", "tool_name": name})

        def _exec_tool(tid, tname, targs):
            try:
                result = jf_registry.dispatch(tname, targs)
                return tid, result, None
            except Exception as e:
                return tid, {"error": str(e)}, str(e)

        pending[tc_id] = executor.submit(_exec_tool, tc_id, name, args)
        if fired_signatures is not None:
            fired_signatures.add(f"{name}:{_json.dumps(args, sort_keys=True)}")
