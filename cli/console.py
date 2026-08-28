"""CLIConsole - 流式渲染控制台，负责 LLM 输出消费和工具执行展示。"""

from __future__ import annotations

import json
import queue
import random
import threading
import time
from typing import Dict, Optional

from rich.text import Text

from agent_p2p import set_p2p_busy
from event_bus import event_bus
from output_engine import OutputEngine, JifyTheme
from stream_consumer import BaseStreamConsumer

console = JifyTheme.create_console()


class CLIConsole(BaseStreamConsumer):
    THINK_PHRASES = OutputEngine.THINK_PHRASES

    _MIN_DRAIN_CAP = 50
    _MAX_DRAIN_CAP = 150
    _DRAIN_RATIO = 0.2
    _STREAM_STALL_TIMEOUT = 15 #

    def __init__(self, think_stream: bool = False) -> None:
        self.token_num = 0
        self.last_reasoning_content = ""
        self.event_bus = event_bus
        self.total_tokens_sent = 0
        self.total_tokens_recv = 0
        self._stream_count = 0
        self._pending_sent_tokens = 0
        self._token_recv = 0
        self._token_sent_target = 0
        self._stop_listener = threading.Event()
        self._listener_thread: Optional[threading.Thread] = None
        self._stream_error: Optional[Exception] = None
        self._last_todo_snapshot: Dict[str, str] = {}
        self._think_stream: bool = think_stream

        self._output = OutputEngine()
        self.set_input_active = self._output.set_input_active

    def prepare_for_input(self) -> None:
        self._output.prepare_for_input()

    @property
    def _model_phase(self) -> str:
        return self._output.model_phase

    @_model_phase.setter
    def _model_phase(self, value: str):
        self._output.model_phase = value

    @property
    def _stream_buffer(self) -> str:
        return self._output.stream_buffer

    @_stream_buffer.setter
    def _stream_buffer(self, value: str):
        self._output.stream_buffer = value

    def start_round(self) -> None:
        self._output.start_round()

    def stop_live(self) -> None:
        self._output.stop_live()

    def finalize(self) -> None:
        self._output.finalize()
        self.total_tokens_sent = 0
        self.total_tokens_recv = 0
        self._pending_sent_tokens = 0
        self._stream_error = None
        self._last_todo_snapshot = {}

    def stream_end(self) -> None:
        pass

    def Nanswer(self) -> None:
        pass

    # 计算出sent的量，最大150
    def _drain_sent_events(self) -> int:
        result = 0
        cap = max(self._MIN_DRAIN_CAP,
                  min(self._MAX_DRAIN_CAP,
                      int(self._pending_sent_tokens * self._DRAIN_RATIO) + self._MIN_DRAIN_CAP))

        if self._pending_sent_tokens > 0:
            released = min(self._pending_sent_tokens, cap)
            self._pending_sent_tokens -= released
            result += released
            cap -= released

        while not self.event_bus.empty() and cap > 0:
            try:
                ev = self.event_bus.get_nowait()
                if hasattr(ev, 'type'):
                    if ev.type == 'Token_Send':
                        incoming = int(ev.data) if ev.data else 0
                        if incoming <= cap:
                            result += incoming
                            cap -= incoming
                        else:
                            result += cap # 超出单次cap上限，多余的放到pending里

                            self._pending_sent_tokens += (incoming - cap)
                            cap = 0
                    elif ev.type == 'DIFF':
                        self._output.output_diff(str(ev.data))
                    elif ev.type == 'TEXT':
                        data_str = str(ev.data)
                        if not data_str.startswith('* preparing '):
                            self._output.queue_output(Text(data_str))
                    elif ev.type == 'todo_update':
                        self._output.set_todos(ev.data)
                    # elif ev.type == 'workflow_step':
                    #     self._output.process_workflow_event(ev.data)
            except queue.Empty:
                break

        if cap > 0 and self._pending_sent_tokens > 0:
            released = min(self._pending_sent_tokens, cap)
            self._pending_sent_tokens -= released
            result += released

        while not self.event_bus.empty():
            try:
                ev = self.event_bus.get_nowait()
                if hasattr(ev, 'type'):
                    if ev.type == 'Token_Send': # 考虑到计算结果来自工具执行线程，通过此解耦
                        self._pending_sent_tokens += int(ev.data) if ev.data else 0
                    elif ev.type == 'DIFF':
                        self._output.output_diff(str(ev.data))
                    elif ev.type == 'TEXT':
                        data_str = str(ev.data)
                        if not data_str.startswith('* preparing '):
                            self._output.queue_output(Text(data_str))
                    elif ev.type == 'todo_update':
                        self._output.set_todos(ev.data)
                    elif ev.type == 'workflow_step':
                        self._output.process_workflow_event(ev.data)
            except queue.Empty:
                break

        return result

    # 优雅的清空queue里的内容，防止泄露到下一轮对话
    def drain_events(self) -> None:
        while not self.event_bus.empty():
            try:
                ev = self.event_bus.get_nowait()
            except queue.Empty:
                break

    # ---- BaseStreamConsumer 钩子 ----

    def _reset_stream_state(self) -> None:
        self._output.stream_buffer = ""
        self._output.think_buffer = ""
        self._output.reset_thinking()
        self._output.phrase = random.choice(self.THINK_PHRASES)
        self._token_recv = 0
        self._stream_count += 1
        self._token_sent_target = self._drain_sent_events()
        self._output.init_anim_state(
            self.total_tokens_sent,
            self.total_tokens_recv,
            self._token_sent_target
        )

    def _stall_timeout(self) -> Optional[float]:
        return self._STREAM_STALL_TIMEOUT

    def _on_content(self, text: str) -> None:
        if self._think_stream and self._output.model_phase == "thinking":
            self._output.output_thinking(flush=True)
        self._output.model_phase = "replying"
        self._token_recv += len(text)
        self._output.stream_buffer += text
        self._output.update_anim_target(-1, self.total_tokens_recv + self._token_recv)

    def _on_thinking(self, text: str) -> None:
        self._output.model_phase = "thinking"
        self.last_reasoning_content += text
        self._output.think_buffer += text
        self._token_recv += len(text)
        self._output.update_anim_target(-1, self.total_tokens_recv + self._token_recv)
        if self._think_stream and len(self._output.think_buffer) >= 120:
            self._output.output_thinking(flush=True)

    def _on_tool_token(self, text: str) -> None:
        self._token_recv += len(text)

    def _post_chunk(self, chunk) -> None:
        # 状态更新的设计，会出现recv的时候刷新掉send缓冲里的token，视觉上感知send和recv同时在交互，增强交互力度
        new_sent = self._drain_sent_events()
        if new_sent > 0:
            self._token_sent_target += new_sent
        self._output.update_anim_target(
            self.total_tokens_sent + self._token_sent_target,
            self.total_tokens_recv + self._token_recv
        )

    def _handle_stream_error(self, e: Exception, interrupt_event=None) -> None:
        error_msg = str(e)

        # ESC 中断导致的流关闭，不是真实错误，不打印错误消息
        if interrupt_event is not None and interrupt_event.is_set():
            self._output.stream_buffer = ""
        elif isinstance(e, TimeoutError):
            self._output.queue_output(Text(
                f"⚠ 流式响应停滞：{self._STREAM_STALL_TIMEOUT}s 未收到数据。\n"
                "   API 服务端可能已静默断开连接。\n"
                "   请检查网络状况后重试，或使用 /clear 清除历史。",
                style=JifyTheme.RED))
        elif "peer closed" in error_msg or "incomplete chunked" in error_msg:
            self._output.queue_output(Text(
                "⚠ 连接中断：LLM 服务端在响应未完成时关闭了连接。\n"
                "   这通常是因为上下文过长超出模型窗口限制。\n"
                "   建议使用 /clear 清除对话历史后重试。",
                style=JifyTheme.RED))
        else:
            self._output.queue_output(Text(f"⚠ 流读取异常: {error_msg}", style=JifyTheme.RED))

    def _post_stream(self) -> None:
        if self._think_stream:
            self._output.output_thinking(flush=True)

    def _prepare_tool_submit(self, tc_id, name, args, args_str, registry):
        if self._output.think_buffer.strip():
            self._output.output_thinking(flush=True)

        if self._output.stream_buffer.strip():
            self._output.output_markdown(self._output.stream_buffer)
            self._output.stream_buffer = ""

        if name == "subagent_run":
            task_desc = args.get("task", "")
            if len(task_desc) > 100:
                task_desc = task_desc[:97] + "…"
            self._output.queue_output(Text(f"Task({task_desc})…", style="bold white"))

            def _exec_subagent(tid, tname, targs):
                import threading as _thr
                try:
                    inner_result = registry.dispatch(tname, targs)
                    from subagent import _subagent_stats
                    stats = _subagent_stats.pop(_thr.get_ident(), {"tool_uses": 0, "elapsed": 0})
                    return json.dumps({
                        "__sa_stats__": stats,
                        "result": inner_result,
                    }, ensure_ascii=False)
                except Exception as e:
                    return json.dumps({"error": str(e)}, ensure_ascii=False)

            return _exec_subagent

        if name.startswith("team_"):
            team_label = {
                "team_delegate": "委派",
                "team_delegate_parallel": "并行委派",
                "team_broadcast": "广播",
                "team_add_worker": "添加 Worker",
                "team_remove_worker": "移除 Worker",
                "team_status": "查询",
            }.get(name, "Team")
            self._output.queue_output(Text(""))
            self._output.queue_output(Text(f"⚙ {team_label}…", style="bold white"))

            def _exec_team(tid, tname, targs):
                return registry.dispatch(tname, targs)

            return _exec_team

        if name == "read_file":
            try:
                tool_args = json.loads(args_str) if args_str else {}
            except Exception:
                tool_args = {}
            path = tool_args.get("path", "")
            if isinstance(path, str):
                display_path = path.rsplit("/", 1)[-1] if path else "…"
            else:
                display_path = str(path) if path else "…"
            limit = tool_args.get("limit", "All")
            self._output.queue_output(Text(""))
            self._output.queue_output(Text(f"• Read({display_path})", style="bold white"))
            self._output.queue_output(Text(
                f"  ⎿  Read {limit} lines", style=JifyTheme.SUBTLE
            ))
        elif args_str:
            name_line, detail_line = OutputEngine.format_tool_call(name, args_str)
            self._output.queue_output(Text(""))
            self._output.queue_output(Text(name_line, style="bold white"))
            if detail_line:
                self._output.queue_output(Text(detail_line, style="white"))
        else:
            self._output.queue_output(Text(""))
            self._output.queue_output(Text(f"• {name}", style="bold white"))

        def _exec(tid, tname, targs):
            try:
                return registry.dispatch(tname, targs)
            except Exception as e:
                return json.dumps({"error": str(e)}, ensure_ascii=False)

        # 交给后续的_execute_tools执行 同步执行策略
        if name != "patch_file" and name != "write_file":
            return _exec
        return None

    def _on_future_result(self, tc_id, future, pre_results, tc_names, tc_args) -> None:
        try:
            raw = future.result(timeout=120)
        except Exception as e:
            _err_name = tc_names.get(tc_id, "")
            _err_args = tc_args.get(tc_id, {})
            pre_results[f"{_err_name}:{json.dumps(_err_args, sort_keys=True)}"] = json.dumps({"error": str(e)}, ensure_ascii=False)
            return

        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "__sa_stats__" in data:
                stats = data["__sa_stats__"]
                tool_uses = stats.get("tool_uses", 0)
                elapsed = stats.get("elapsed", 0)
                sent_est = stats.get("sent_est", 0)
                recv_est = stats.get("recv_est", 0)
                token_str = ""
                if sent_est:
                    # 初期架构设计的缺陷，暂时使用预估的token计数
                    token_str += f" · ↑ {OutputEngine.fmt_tokens(sent_est // 2)} tokens"
                if recv_est:
                    token_str += f" · ↓ {OutputEngine.fmt_tokens(recv_est // 2)} tokens"
                self._output.queue_output(Text(
                    f"  ⏻  Done ({tool_uses} tool uses · {OutputEngine.fmt_elapsed(elapsed)}{token_str})",
                    style=JifyTheme.SUBTLE
                ))
                self._output.clear_subagent()
                _sa_name = tc_names.get(tc_id, "subagent_run")
                _sa_args = tc_args.get(tc_id, {})
                pre_results[f"{_sa_name}:{json.dumps(_sa_args, sort_keys=True)}"] = data["result"]
                return
        except (json.JSONDecodeError, TypeError):
            pass

        _tool_name = tc_names.get(tc_id, "")
        _tool_args = tc_args.get(tc_id, {})
        pre_results[f"{_tool_name}:{json.dumps(_tool_args, sort_keys=True)}"] = raw

    def _finalize_stream(self, stream_error: Optional[Exception]) -> None:
        self._stream_error = stream_error
        if stream_error is not None:
            self._output.stream_buffer = ""

        elapsed = time.time() - self._output.session_start_time
        self._output.model_phase = "idle"
        self._output.update_status(
            self._output.phrase, elapsed,
            self.total_tokens_sent + self._token_sent_target,
            self.total_tokens_recv + self._token_recv
        )

        self.total_tokens_sent += self._token_sent_target
        self.total_tokens_recv += self._token_recv

    def flush_stream(self, is_final: bool = False) -> None:
        if self._stream_error is not None:
            self._output.stream_buffer = ""
            return
        if self._output.think_buffer.strip():
            self._output.output_thinking(flush=True)
        self._output.flush_stream_buffer(is_final)

    def start_p2p_listener(self) -> None:
        if self._listener_thread and self._listener_thread.is_alive():
            return
        set_p2p_busy(False)
        self._stop_listener.clear()
        self._listener_thread = threading.Thread(target=self._p2p_listen, daemon=True)
        self._listener_thread.start()

    def stop_p2p_listener(self) -> None:
        set_p2p_busy(True)
        self._stop_listener.set()
        if self._listener_thread and self._listener_thread.is_alive():
            self._listener_thread.join(timeout=1)
        self._listener_thread = None

    def _p2p_listen(self) -> None:
        while not self._stop_listener.is_set():
            try:
                ev = self.event_bus.get(timeout=0.3)
                if hasattr(ev, 'type'):
                    if ev.type == 'TEXT':
                        self._output.queue_output(Text(ev.data))
                    elif ev.type == 'DIFF':
                        self._output.output_diff(str(ev.data))
                self.event_bus.task_done()
            except queue.Empty:
                continue
