"""CLIConsole - 流式渲染控制台，负责 LLM 输出消费和工具执行展示。"""

from __future__ import annotations

import concurrent.futures
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

console = JifyTheme.create_console()


class CLIConsole:
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
        self._stop_listener = threading.Event()
        self._listener_thread: Optional[threading.Thread] = None
        self._stream_error: Optional[Exception] = None
        self._last_todo_snapshot: Dict[str, str] = {}
        self._think_stream: bool = think_stream

        self._output = OutputEngine()
        self.set_input_active = self._output.set_input_active

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

    def consume_stream(self, response, interrupt_event=None):
        import concurrent.futures
        from jify_tool import registry as jf_registry

        complete_text = ""
        reasoning_text = ""
        tool_call_chunks: Dict[int, Dict] = {}
        finish_reason = ""
        pre_results: Dict[str, str] = {}
        pending_futures: Dict[str, concurrent.futures.Future] = {}
        _fired_indices: set = set()
        tc_names: Dict[str, str] = {}
        tc_args: Dict[str, dict] = {}
        tc_id_to_idx: Dict[str, int] = {}
        tool_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8)

        self._output.stream_buffer = ""
        self._output.think_buffer = ""
        self._output.reset_thinking()
        self._output.phrase = random.choice(self.THINK_PHRASES)
        token_recv = 0
        self._stream_count += 1
        token_sent_target = self._drain_sent_events()

        self._output.init_anim_state(
            self.total_tokens_sent,
            self.total_tokens_recv,
            token_sent_target
        )

        def _tool_current_index() -> int:
            return max(tool_call_chunks.keys()) if tool_call_chunks else -1

        last_seen_idx = -1

        try:
            _chunk_iter = iter(response)
        except TypeError:
            _chunk_iter = response

        stream_error: Optional[Exception] = None
        _last_chunk_time = time.monotonic()
        try:
            for chunk in _chunk_iter:
                now = time.monotonic()
                if now - _last_chunk_time > self._STREAM_STALL_TIMEOUT:
                    raise TimeoutError(
                        f"LLM 流式响应停滞 {self._STREAM_STALL_TIMEOUT}s 无数据，"
                        "API 服务端可能已静默断开连接"
                    )
                _last_chunk_time = now

                from tools.approval import break_requested # sys 缓存
                if interrupt_event is not None and interrupt_event.is_set():
                    break
                if break_requested.is_set(): # 审批选择 break 传递信号到此直接break掉
                    break

                if chunk.content:
                    if self._think_stream and self._output.model_phase == "thinking":
                        self._output.output_thinking(flush=True)
                    self._output.model_phase = "replying"
                    complete_text += chunk.content
                    token_recv += len(chunk.content)
                    self._output.stream_buffer += chunk.content
                    self._output.update_anim_target(-1, self.total_tokens_recv + token_recv)

                if chunk.thinking:
                    self._output.model_phase = "thinking"
                    reasoning_text += chunk.thinking
                    self.last_reasoning_content += chunk.thinking
                    self._output.think_buffer += chunk.thinking
                    token_recv += len(chunk.thinking)
                    self._output.update_anim_target(-1, self.total_tokens_recv + token_recv)
                    if self._think_stream and len(self._output.think_buffer) >= 120:
                        self._output.output_thinking(flush=True)

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

                last_seen_idx = _tool_current_index()

                if chunk.finish_reason:
                    finish_reason = chunk.finish_reason
                # 状态更新的设计，会出现recv的时候刷新掉send缓冲里的token，视觉效果上会感知send和recv同时在交互，增强交互力度
                new_sent = self._drain_sent_events()
                if new_sent > 0:
                    token_sent_target += new_sent
                self._output.update_anim_target(
                    self.total_tokens_sent + token_sent_target,
                    self.total_tokens_recv + token_recv
                )

        except Exception as e:
            stream_error = e
            error_msg = str(e)

            # ESC 中断导致的流关闭，不是真实错误，不打印错误消息
            if interrupt_event is not None and interrupt_event.is_set():
                self._output.stream_buffer = ""
            elif isinstance(e, TimeoutError):
                self._output.queue_output(Text(
                    f"⚠ 流式响应停滞：{self._STREAM_STALL_TIMEOUT}s 未收到数据。\\n"
                    "   API 服务端可能已静默断开连接。\\n"
                    "   请检查网络状况后重试，或使用 /clear 清除历史。",
                    style=JifyTheme.RED))
            elif "peer closed" in error_msg or "incomplete chunked" in error_msg:
                self._output.queue_output(Text(
                    "⚠ 连接中断：LLM 服务端在响应未完成时关闭了连接。\\n"
                    "   这通常是因为上下文过长超出模型窗口限制。\\n"
                    "   建议使用 /clear 清除对话历史后重试。",
                    style=JifyTheme.RED))
            else:
                self._output.queue_output(Text(f"⚠ 流读取异常: {error_msg}", style=JifyTheme.RED))

        if self._think_stream:
            self._output.output_thinking(flush=True)

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
            try:
                raw = future.result(timeout=120)
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
                        continue
                except (json.JSONDecodeError, TypeError):
                    pass

                _tool_name = tc_names.get(tc_id, "")
                _tool_args = tc_args.get(tc_id, {})
                pre_results[f"{_tool_name}:{json.dumps(_tool_args, sort_keys=True)}"] = raw
            except Exception as e:
                _err_name = tc_names.get(tc_id, "")
                _err_args = tc_args.get(tc_id, {})
                pre_results[f"{_err_name}:{json.dumps(_err_args, sort_keys=True)}"] = json.dumps({"error": str(e)}, ensure_ascii=False)

        # self._output._tool_running = False
        # self._output._tool_done = True
        tool_executor.shutdown(wait=False)

        self._stream_error = stream_error
        if stream_error is not None:
            self._output.stream_buffer = ""

        elapsed = time.time() - self._output.session_start_time
        self._output.model_phase = "idle"
        self._output.update_status(
            self._output.phrase, elapsed,
            self.total_tokens_sent + token_sent_target,
            self.total_tokens_recv + token_recv
        )

        self.total_tokens_sent += token_sent_target
        self.total_tokens_recv += token_recv

        return complete_text, tool_call_chunks, finish_reason, pre_results

    def _fire_tool(self, idx, chunks, pending, pre, executor, registry,
                   tc_names=None, tc_args=None, tc_id_to_idx=None):
        from tools.approval import break_requested
        if break_requested.is_set(): # 中断信号
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

            pending[tc_id] = executor.submit(_exec_subagent, tc_id, name, args)
            # self._output._tool_running = True
            return

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

            pending[tc_id] = executor.submit(registry.dispatch, name, args)
            # self._output._tool_running = True
            return

        # if name == "read_file":
        #     pass

        if name == "read_file":
            # pass
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
            pending[tc_id] = executor.submit(_exec, tc_id, name, args)
        # pending[tc_id] = executor.submit(_exec, tc_id, name, args)
        # self._output._tool_running = True 不显示toolcall，因为毫秒级

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
