"""WebSocket 流式控制台 —— 接口对齐 CLIConsole，通过队列桥接同步/异步。

consume_stream / _fire_tool 骨架继承自 BaseStreamConsumer（stream_consumer.py），
子类只实现钩子：chunk → ws 消息、工具 fire 通知、future 收尾解包。
"""

import json as _json
import queue
from typing import Optional

from fastapi import WebSocket
from stream_consumer import BaseStreamConsumer
from event_bus import event_bus


class WebSocketConsole(BaseStreamConsumer):
    """
    对齐 CLIConsole 接口，可由 AgentLoop.run() 同步调用。

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
        self.event_bus = event_bus

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

    def drain_events(self):
        """优雅地清空 event_bus 里的内容，防止上一会话事件泄漏到下一会话。"""
        while True:
            try:
                self.event_bus.get_nowait()
            except queue.Empty:
                break

    async def drain_outgoing(self):
        """一次排空所有积压消息（由 asyncio 侧定期调用）。

        顺序：先转发 consume_stream 产生的主消息流，再转发 event_bus 里的
        结构化事件（todo / diff / text / message / error / team_update）。这样
        工具线程、agent_loop 投递到 event_bus 的能力（CLI 已消费）也能透传到
        前端，避免断层。
        """
        while True:
            try:
                msg = self._outgoing.get_nowait()
                await self.ws.send_json(msg)
            except queue.Empty:
                break

        while True:
            try:
                ev = self.event_bus.get_nowait()
                payload = self._translate_event(ev)
                if payload:
                    await self.ws.send_json(payload)
            except queue.Empty:
                break

    def _translate_event(self, ev) -> Optional[dict]:
        """把 event_bus 的 UIEvent 翻译成前端可渲染的 ws 消息。

        对齐 CLIConsole._drain_sent_events 的消费语义；Token_Send 等纯计数事件
        在此消费掉但不转发（WebUI 暂不需要 token 动画）。
        """
        ev_type = getattr(ev, 'type', '')
        if ev_type == 'todo_update':
            return {"type": "todo_update", "todos": ev.data}
        if ev_type == 'DIFF':
            return {"type": "diff", "content": str(ev.data)}
        if ev_type == 'TEXT':
            data_str = str(ev.data)
            if data_str.startswith('* preparing '):
                return None
            return {"type": "text", "content": data_str}
        if ev_type == 'MESSAGE':
            return {"type": "message", "content": str(ev.data)}
        if ev_type == 'ERROR':
            return {"type": "error", "content": str(ev.data)}
        if ev_type == 'team_update':
            data = ev.data if isinstance(ev.data, dict) else {}
            return {
                "type": "team_update",
                "worker_id": data.get("worker_id"),
                "info": data.get("info"),
                "clear": data.get("clear", False),
            }
        if ev_type == 'workflow_step':
            return {"type": "workflow_step", "data": ev.data}
        return None

    # ---- BaseStreamConsumer 钩子实现 ----

    def _reset_stream_state(self) -> None:
        self._send({"type": "thinking_start"})

    def _on_content(self, text: str) -> None:
        self.total_tokens_recv += len(text)
        self._send({"type": "text_chunk", "content": text})

    def _on_thinking(self, text: str) -> None:
        self.last_reasoning_content += text
        self._send({"type": "thinking", "content": text})

    def _prepare_tool_submit(self, tc_id, name, args, args_str, registry):
        self._send({"type": "tool_start", "tool_name": name, "tool_id": tc_id})

        def _exec_tool(tid, tname, targs):
            try:
                result = registry.dispatch(tname, targs)
                return tid, result, None
            except Exception as e:
                return tid, _json.dumps({"error": str(e)}, ensure_ascii=False), str(e)

        return _exec_tool

    def _on_future_result(self, tc_id, future, pre_results, tc_names, tc_args) -> None:
        try:
            tid, raw, err = future.result(timeout=30)
            if err:
                self._send({"type": "tool_error", "tool_id": tid, "error": err})
            else:
                self._send({"type": "tool_result", "tool_id": tid, "result": raw})
            # 签名 key（name:json(args)），与 agent_loop._execute_tools 的预执行匹配逻辑对齐
            _name = tc_names.get(tc_id, "")
            _args = tc_args.get(tc_id, {})
            pre_results[f"{_name}:{_json.dumps(_args, sort_keys=True)}"] = raw
        except Exception as e:
            pre_results[tc_id] = {"error": str(e)}
