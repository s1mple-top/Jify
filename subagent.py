# -*- coding: utf-8 -*-
"""Subagent 执行器 — 复用 model_client，同步执行，contextvars 工具隔离，暂时不考虑异步执行"""

import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

from tools.registry import registry, _subagent_whitelist
from tools.call_context import is_cancelled

# 线程级 subagent 统计：由 SubagentRunner.run() 写入，CLI _fire_tool 读取
_subagent_stats: Dict[int, Dict] = {}

_CANCELLED_RESULT = "[subagent] 任务已被取消，未执行完。"


class SubagentRunner:
    """
    简化版 AgentLoop，在同一进程内同步执行，通过 contextvars 限制可用工具。

    使用方式:
        runner = SubagentRunner(model_client, config)
        result = runner.run(
            task="审查 xxxx.py",
            system_prompt="你是一个代码审查 subagent...",
            whitelist_schemas=[...],
            whitelist_names={"read_file", "static_analysis"},
            max_iterations=20,
        )
    """

    def __init__(self, model_client: Any, config: Any):
        self.model_client = model_client
        self.config = config

    def run(
        self,
        task: str,
        system_prompt: str,
        whitelist_schemas: List[Dict],
        whitelist_names: Set[str],
        max_iterations: int = 20,
        on_progress: Optional[Callable[[str, Dict], None]] = None,
        interrupt_event: Optional[threading.Event] = None,
        history: Optional[List[Dict]] = None,
        out_history: Optional[List] = None,
    ) -> str:
        """
        执行 subagent 任务并返回最终文本结果。

        Args:
            history: 传入的历史消息（不含 system），用于跨任务保留上下文记忆。
            out_history: 可变列表，任务结束后写入本轮完整消息（不含 system），
                供调用方（如 TeamWorker）保存为下一次的历史。

        Returns:
            最终回复文本；若达到最大迭代次数，返回带提示的文本。
        """
        token = _subagent_whitelist.set(whitelist_names)
        _start = time.time()
        tool_uses = 0
        recv_est = 0
        messages: List[Dict] = []
        try:
            messages.append({"role": "system", "content": system_prompt})
            if history:
                messages.extend(history)
            messages.append({"role": "user", "content": task})

            # 与主 Agent 对齐：sent 为累计发送量（本轮新增内容原始字符数累加），
            # 而非当前上下文全量大小；初始上下文不计入 sent。
            sent_est = 0
            if on_progress:
                on_progress("token_update", {"sent": sent_est, "recv": recv_est})

            last_content = ""

            def _record_stats() -> None:
                _subagent_stats[threading.get_ident()] = {
                    "tool_uses": tool_uses,
                    "elapsed": time.time() - _start,
                    "sent_est": sent_est,
                    "recv_est": recv_est,
                }

            def _bail_if_cancelled() -> Optional[str]:
                """取消信号命中时收尾并返回终止文案；未命中返回 None。

                只在自然边界调用：每轮迭代开头、流式响应每个 chunk、每次工具执行前。
                检查点之间的阻塞（单次模型响应、单个长工具）仍需等它自己结束。
                """
                if not is_cancelled(interrupt_event):
                    return None
                _record_stats()
                return _CANCELLED_RESULT

            for _ in range(max_iterations):
                # 中断检查：主 Agent Ctrl+C / 调用超时 / team 取消 都汇总到 is_cancelled
                halted = _bail_if_cancelled()
                if halted is not None:
                    return halted

                raw_response = self.model_client.chat(
                    messages=messages,
                    tool_schemas=whitelist_schemas,
                    model=self.config.model,
                    stream=True,
                )

                content = ""
                tool_call_chunks: Dict[int, Dict] = {}
                stream_cancelled = False

                for chunk in raw_response:
                    # 模型流式响应可能持续很久，必须在内层也检查，否则取消要等整段输出完
                    if is_cancelled(interrupt_event):
                        stream_cancelled = True
                        break
                    if chunk.content:
                        content += chunk.content
                        recv_est += len(chunk.content)
                        if on_progress:
                            on_progress("text", {"text": chunk.content})
                            on_progress("token_update", {"sent": sent_est, "recv": recv_est})

                    if chunk.thinking:
                        # thinking（推理）同样发给模型，按原始字符数计入 recv，与主 Agent 口径一致
                        recv_est += len(chunk.thinking)
                        if on_progress:
                            on_progress("thinking", {"text": chunk.thinking})
                            on_progress("token_update", {"sent": sent_est, "recv": recv_est})

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

                if stream_cancelled:
                    # 显式关闭生成器，立即释放底层 HTTP 连接，不等 GC
                    close = getattr(raw_response, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:
                            pass
                    _record_stats()
                    return _CANCELLED_RESULT

                last_content = content

                # 组装 tool_calls（一次性构建，同时用于 sent_est 更新和后续消息追加）
                tool_calls = [
                    {
                        "id": tool_call_chunks[idx]["id"],
                        "type": "function",
                        "function": {
                            "name": tool_call_chunks[idx]["function"]["name"],
                            "arguments": tool_call_chunks[idx]["function"]["arguments"],
                        },
                    }
                    for idx in sorted(tool_call_chunks.keys())
                ]

                if not tool_calls:
                    _record_stats()
                    return content

                assistant_msg = {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                }
                messages.append(assistant_msg)
                # assistant 正文在下一轮会发送给模型，按原始字符数计入 sent（与主 Agent 一致）
                sent_est += len(content)

                for tc in tool_calls:
                    # 逐工具检查：前一个工具可能跑了很久，期间外层已放弃等待
                    halted = _bail_if_cancelled()
                    if halted is not None:
                        return halted

                    tool_name = tc["function"]["name"]
                    try:
                        tool_args = json.loads(tc["function"]["arguments"])
                    except (json.JSONDecodeError, TypeError):
                        tool_args = {}

                    tool_uses += 1
                    if on_progress:
                        on_progress("tool_start", {"name": tool_name, "args": tool_args})
                    result = registry.dispatch(tool_name, tool_args)
                    if on_progress:
                        on_progress("tool_end", {"name": tool_name, "result": result[:200]})
                    tool_msg = {
                        "role": "tool",
                        "content": result,
                        "tool_call_id": tc["id"],
                    }
                    messages.append(tool_msg)
                    # 每追加一条 tool 结果消息，按原始字符数计入发送量（与主 Agent 一致）
                    sent_est += len(result)

                if on_progress:
                    on_progress("token_update", {"sent": sent_est, "recv": recv_est})

            _record_stats()
            return (
                f"[subagent] 达到最大迭代次数 ({max_iterations})，最后回复:\n{last_content}"
            )
        finally:
            if out_history is not None:
                out_history[:] = [m for m in messages if m.get("role") != "system"]
            _subagent_whitelist.reset(token)
