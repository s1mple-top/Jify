# -*- coding: utf-8 -*-
"""
流式响应消费基类。

把 consume_stream 的骨架逻辑（chunk 循环、tool_call 增量组装、fire 时机判定、
future 等待、返回值组装）抽到基类，CLI / Web 两个子类各自实现钩子，
消除 cli/console.py 与 gateway/ws_console.py 的重复实现。
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import json
import time
from typing import Any, Dict, Optional

from tools.call_context import CallContext, set_call_context, reset_call_context
from event_bus import event_bus, UIEvent


def _is_timeout_error(exc: BaseException) -> bool:
    """判断异常是否表示超时。

    httpx.ReadTimeout / anthropic.APITimeoutError 都不是内置 TimeoutError
    的子类（httpx 走 TimeoutException，anthropic 走 APIError），因此不能
    用 isinstance(exc, TimeoutError) 判定。
    """
    if isinstance(exc, (TimeoutError,)):
        return True
    # 按类名匹配，避免为可选依赖强引入 httpx / anthropic
    names = {type(exc).__name__}
    for base in type(exc).__mro__:
        names.add(base.__name__)
    if names & {"TimeoutException", "ReadTimeout", "ConnectTimeout",
                "WriteTimeout", "PoolTimeout", "APITimeoutError"}:
        return True
    msg = str(exc).lower()
    return "timed out" in msg or "timeout" in type(exc).__name__.lower()


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
    - _on_future_result(tc_id, future, pre_results, tc_names, tc_args, timeout): 收尾解包 future
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
        # 每个 fire 出去的工具一个 CallContext：超时只能终止该工具名下的外部资源，不牵连并行的其它工具
        fire_ctxs: Dict[str, Any] = {}
        tool_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8)

        self._reset_stream_state()
        # 本轮流读取错误（stall / 断流等），供 agent_loop 区分「模型自然结束」
        # 与「流出错被吞后返回空」——后者若当正常完成，长任务会静默终止
        self.last_stream_error: Optional[Exception] = None

        try:
            _chunk_iter = iter(response)
        except TypeError:
            _chunk_iter = response

        stream_error: Optional[Exception] = None
        last_seen_idx = -1
        _last_chunk_time = time.monotonic()
        _first_chunk_done = False
        try:
            for chunk in _chunk_iter:
                stall = self._stall_timeout()
                if stall is not None:
                    now = time.monotonic()
                    # 首 chunk 前的等待是服务端 prefill（压缩/长历史下常超 15s，
                    # 且前缀缓存失效时必然全量 prefill），不应按「流停滞」误杀；
                    # 收到首个 payload 后才恢复严格的中途停滞检测
                    if not _first_chunk_done:
                        _ft = self._first_chunk_stall_timeout()
                        if _ft is not None:
                            stall = max(stall, _ft)
                    if now - _last_chunk_time > stall:
                        raise TimeoutError(
                            f"LLM 流式响应停滞 {stall}s 无有效数据，"
                            "API 服务端可能已静默断开连接"
                        )

                from tools.approval import break_requested  # sys 缓存
                if interrupt_event is not None and interrupt_event.is_set():
                    break
                if break_requested.is_set():  # 审批选择 break 传递信号到此直接 break 掉
                    break

                if (chunk.content or chunk.thinking or chunk.tool_call_deltas
                        or chunk.finish_reason
                        or getattr(chunk, "thinking_signature", "")
                        or getattr(chunk, "thinking_redacted", "")):
                    _first_chunk_done = True
                    # 活动时间只由 payload chunk 刷新：网关 prefill 大上下文期间
                    # 发送的 SSE 心跳（空 chunk）不能绕过停滞检测，否则流可被
                    # 无限期维持（表现为压缩完成后 loop 永久卡死）
                    _last_chunk_time = time.monotonic()

                if chunk.content:
                    complete_text += chunk.content
                    self._on_content(chunk.content)

                if chunk.thinking:
                    self._on_thinking(chunk.thinking, getattr(chunk, "thinking_index", -1))

                if getattr(chunk, "thinking_signature", "") or getattr(chunk, "thinking_redacted", ""):
                    self._on_thinking_signature(
                        getattr(chunk, "thinking_signature", "") or "",
                        getattr(chunk, "thinking_redacted", "") or "",
                        getattr(chunk, "thinking_index", -1),
                    )

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
                                            tc_names, tc_args, tc_id_to_idx, fire_ctxs)
                            _fired_indices.add(cidx)

                elif last_seen_idx >= 0 and pending_futures:
                    for cidx in list(tool_call_chunks.keys()):
                        if cidx not in _fired_indices:
                            self._fire_tool(cidx, tool_call_chunks, pending_futures,
                                            pre_results, tool_executor, jf_registry,
                                            tc_names, tc_args, tc_id_to_idx, fire_ctxs)
                            _fired_indices.add(cidx)

                last_seen_idx = max(tool_call_chunks.keys()) if tool_call_chunks else -1

                if chunk.finish_reason:
                    finish_reason = chunk.finish_reason

                self._post_chunk(chunk)

        except KeyboardInterrupt:
            # Ctrl+C 中断路径：先终止已 fire 出去的资源（否则工具继续跑），再释放线程池并向上抛出
            for _ctx in list(fire_ctxs.values()):
                _ctx.cancel()
            tool_executor.shutdown(wait=False)
            raise
        except Exception as e:
            stream_error = e
            self._handle_stream_error(e, interrupt_event)

        self._post_stream()

        from tools.approval import break_requested
        # 流出错（stall / 断流）时不再 fire 剩余工具：此时 tool_call 的 arguments
        # 可能只收到半截，json 解析降级为 {}，fire 出去等于用空参数执行工具
        # （实测 exec 收到 '{"command": "rm -rf' 被解析成 {} 后照常执行）。
        if not break_requested.is_set() and stream_error is None:
            for cidx in list(tool_call_chunks.keys()):
                if cidx not in _fired_indices:
                    self._fire_tool(cidx, tool_call_chunks, pending_futures,
                                    pre_results, tool_executor, jf_registry,
                                    tc_names, tc_args, tc_id_to_idx, fire_ctxs)
                    _fired_indices.add(cidx)

        _aborted = (stream_error is not None
                    or (interrupt_event is not None and interrupt_event.is_set())
                    or break_requested.is_set())
        for tc_id, future in pending_futures.items():
            if tc_id in pre_results:
                continue
            # 用户已中断 / 审批 break：不再按满额超时等待已 fire 的工具（否则 Ctrl+C 后界面仍卡住）
            if _aborted:
                _ctx = fire_ctxs.get(tc_id)
                if _ctx is not None:
                    _ctx.cancel()
                pre_results[f"{tc_names.get(tc_id, '')}:{json.dumps(tc_args.get(tc_id, {}), sort_keys=True)}"] = \
                    "[Skipped — user interrupt]"
                continue
            self._on_future_result(
                tc_id, future, pre_results, tc_names, tc_args,
                self._resolve_fire_timeout(tc_names.get(tc_id, ""), tc_args.get(tc_id)),
                fire_ctxs.get(tc_id),
            )

        tool_executor.shutdown(wait=False)

        self.last_stream_error = stream_error
        self._finalize_stream(stream_error)

        return complete_text, tool_call_chunks, finish_reason, pre_results

    def _fire_tool(self, idx, chunks, pending, pre, executor, registry,
                   tc_names=None, tc_args=None, tc_id_to_idx=None, call_ctxs=None):
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
            # 显式传播当前 context（含主 Agent config），使 worker 线程中的 subagent_run 能复用
            ctx = contextvars.copy_context()
            call_ctx = CallContext()
            if call_ctxs is not None:
                call_ctxs[tc_id] = call_ctx

            def _run(_fn=exec_fn, _tid=tc_id, _name=name, _args=args, _call_ctx=call_ctx):
                # 注入调用上下文：工具（exec / code_sandbox 等）据此登记可终止的外部资源。
                # 不注入则 killer 无法登记，超时只能"放弃等待"，进程/容器会残留。
                token = set_call_context(_call_ctx)
                try:
                    return _fn(_tid, _name, _args)
                finally:
                    reset_call_context(token)

            pending[tc_id] = executor.submit(ctx.run, _run)


    # 钩子（子类实现）
    def _reset_stream_state(self) -> None:
        pass

    def _stall_timeout(self) -> Optional[float]:
        return None

    def notify(self, message: str) -> None:
        """运行期进度/状态提示（压缩等同步长耗时操作）。

        默认走 event_bus（WebUI 有常驻 drain_loop，实时可见）；CLI 运行期
        没有 event_bus 消费者（chat() 起始即 stop_p2p_listener），需覆写为
        直接打印，否则提示会积压到下一轮流开始才批量刷出，用户全程看不到。
        """
        event_bus.put(UIEvent("MESSAGE", message))

    def _first_chunk_stall_timeout(self) -> Optional[float]:
        """首 chunk 前的宽限阈值（服务端 prefill 阶段）。None = 与 stall 一致。"""
        return None

    def _on_content(self, text: str) -> None:
        pass

    def _on_thinking(self, text: str, index: int = -1) -> None:
        pass

    def _on_thinking_signature(self, signature: str, redacted: str, index: int = -1) -> None:
        """Anthropic 扩展思考签名 / redacted_thinking 数据（需原样回传）。

        index 为该 thinking 块在 content 中的序号，interleaved thinking 下
        一轮会有多个块，必须按 index 分别累积。
        """
        pass

    def _on_tool_token(self, text: str) -> None:
        pass

    def _post_chunk(self, chunk) -> None:
        pass

    def _handle_stream_error(self, e: Exception, interrupt_event=None) -> None:
        pass

    def report_api_error(self, message: str) -> None:
        """展示一条单行 API 错误（红色小提示），由各 console 覆写。"""
        pass

    def _post_stream(self) -> None:
        pass

    def _prepare_tool_submit(self, tc_id, name, args, args_str, registry):
        return None

    def _resolve_fire_timeout(self, name: str, args: Any = None) -> float:
        """解析 fire 预执行的收尾等待超时，与 agent_loop 的兜底执行保持同一套配置。

        fire 路径曾各自硬编码（CLI 120s / Gateway 30s），导致 config.tool_timeout
        和工具自定义 timeout 在这条路上完全不生效。args 一并传入，使模型在参数里
        请求的长 timeout 在 fire 路径同样生效。
        """
        try:
            from agent_loop import _resolve_call_timeout
            from jify_tool import registry as jf_registry
            from tools.registry import get_agent_config

            config = get_agent_config()
            if config is not None:
                return _resolve_call_timeout(jf_registry.get(name), config, args)
        except Exception:
            pass
        return 120.0

    def _on_future_result(self, tc_id, future, pre_results, tc_names, tc_args,
                          timeout: float = 120.0,
                          call_ctx: Optional[CallContext] = None) -> None:
        pass

    def _finalize_stream(self, stream_error: Optional[Exception]) -> None:
        pass
