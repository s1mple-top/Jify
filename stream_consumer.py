# -*- coding: utf-8 -*-
"""
流式响应消费基类。

把 consume_stream 的骨架逻辑（chunk 循环、tool_call 增量组装、fire 时机判定、
future 等待、返回值组装）抽到基类，CLI / Web 两个子类各自实现钩子，
消除 cli/console.py 与 gateway/ws_console.py 的重复实现。
"""

from __future__ import annotations

import concurrent.futures
import json
import time
from typing import Any, Dict, Optional


class BaseStreamConsumer:
    """流式响应消费骨架（模板方法）。

    子类通过实现以下钩子定制行为：
    - _reset_stream_state(): 每轮流开始前重置子类状态
    - _stall_timeout(): 返回 stall 检测超时（秒），None 表示禁用
    - _on_content(text): 正文 chunk
    - _on_thinking(text): 思考 chunk
    - _on_tool_token(text): tool_call 的 name/arguments 增量（token 计数）
    - _post_chunk(chunk): 每个 chunk 结尾
    - _handle_stream_error(e, interrupt_event): 流读取异常
    - _post_stream(): 流结束后（所有 chunk 处理完）
    - _prepare_tool_submit(tc_id, name, args, args_str, registry): fire 前准备，返回 exec 函数或 None
    - _on_future_result(tc_id, future, pre_results, tc_names, tc_args): 收尾解包 future
    - _finalize_stream(stream_error): 收尾更新状态 / token 计数
    """

    def consume_stream(self, response, interrupt_event=None):
        from jify_tool import registry as jf_registry

        complete_text = ""
        tool_call_chunks: Dict[int, Dict] = {}
        finish_reason = ""
        pre_results: Dict[str, Any] = {}
        pending_futures: Dict[str, concurrent.futures.Future] = {}
        _fired_indices: set = set()
        tc_names: Dict[str, str] = {}
        tc_args: Dict[str, dict] = {}
        tc_id_to_idx: Dict[str, int] = {}
        tool_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8)

        self._reset_stream_state()

        try:
            _chunk_iter = iter(response)
        except TypeError:
            _chunk_iter = response

        stream_error: Optional[Exception] = None
        last_seen_idx = -1
        _last_chunk_time = time.monotonic()
        try:
            for chunk in _chunk_iter:
                stall = self._stall_timeout()
                if stall is not None:
                    now = time.monotonic()
                    if now - _last_chunk_time > stall:
                        raise TimeoutError(
                            f"LLM 流式响应停滞 {stall}s 无数据，"
                            "API 服务端可能已静默断开连接"
                        )
                    _last_chunk_time = now

                from tools.approval import break_requested  # sys 缓存
                if interrupt_event is not None and interrupt_event.is_set():
                    break
                if break_requested.is_set():  # 审批选择 break 传递信号到此直接 break 掉
                    break

                if chunk.content:
                    complete_text += chunk.content
                    self._on_content(chunk.content)

                if chunk.thinking:
                    self._on_thinking(chunk.thinking)

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
                            self._on_tool_token(tc.name)
                        if tc.arguments:
                            tool_call_chunks[idx]["function"]["arguments"] += tc.arguments
                            self._on_tool_token(tc.arguments)

                    current_indices = {tc.index for tc in chunk.tool_call_deltas}
                    for cidx in list(tool_call_chunks.keys()):
                        if cidx not in current_indices and cidx not in _fired_indices:
                            self._fire_tool(cidx, tool_call_chunks, pending_futures,
                                            pre_results, tool_executor, jf_registry,
                                            tc_names, tc_args, tc_id_to_idx)
                            _fired_indices.add(cidx)

                elif last_seen_idx >= 0 and pending_futures:
                    for cidx in list(tool_call_chunks.keys()):
                        if cidx not in _fired_indices:
                            self._fire_tool(cidx, tool_call_chunks, pending_futures,
                                            pre_results, tool_executor, jf_registry,
                                            tc_names, tc_args, tc_id_to_idx)
                            _fired_indices.add(cidx)

                last_seen_idx = max(tool_call_chunks.keys()) if tool_call_chunks else -1

                if chunk.finish_reason:
                    finish_reason = chunk.finish_reason

                self._post_chunk(chunk)

        except KeyboardInterrupt:
            # Ctrl+C 中断路径：释放线程池后向上抛出，避免跳过 shutdown 泄漏
            tool_executor.shutdown(wait=False)
            raise
        except Exception as e:
            stream_error = e
            self._handle_stream_error(e, interrupt_event)

        self._post_stream()

        from tools.approval import break_requested
        if not break_requested.is_set():
            for cidx in list(tool_call_chunks.keys()):
                if cidx not in _fired_indices:
                    self._fire_tool(cidx, tool_call_chunks, pending_futures,
                                    pre_results, tool_executor, jf_registry,
                                    tc_names, tc_args, tc_id_to_idx)
                    _fired_indices.add(cidx)

        for tc_id, future in pending_futures.items():
            if tc_id in pre_results:
                continue
            self._on_future_result(tc_id, future, pre_results, tc_names, tc_args)

        tool_executor.shutdown(wait=False)

        self._finalize_stream(stream_error)

        return complete_text, tool_call_chunks, finish_reason, pre_results

    def _fire_tool(self, idx, chunks, pending, pre, executor, registry,
                   tc_names=None, tc_args=None, tc_id_to_idx=None):
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
            args = json.loads(args_str) if args_str else {}
        except Exception:
            args = {}

        if tc_args is not None:
            tc_args[tc_id] = args

        exec_fn = self._prepare_tool_submit(tc_id, name, args, args_str, registry)
        if exec_fn is not None:
            pending[tc_id] = executor.submit(exec_fn, tc_id, name, args)


    # 钩子（子类实现）
    def _reset_stream_state(self) -> None:
        pass

    def _stall_timeout(self) -> Optional[float]:
        return None

    def _on_content(self, text: str) -> None:
        pass

    def _on_thinking(self, text: str) -> None:
        pass

    def _on_tool_token(self, text: str) -> None:
        pass

    def _post_chunk(self, chunk) -> None:
        pass

    def _handle_stream_error(self, e: Exception, interrupt_event=None) -> None:
        pass

    def _post_stream(self) -> None:
        pass

    def _prepare_tool_submit(self, tc_id, name, args, args_str, registry):
        return None

    def _on_future_result(self, tc_id, future, pre_results, tc_names, tc_args) -> None:
        pass

    def _finalize_stream(self, stream_error: Optional[Exception]) -> None:
        pass
