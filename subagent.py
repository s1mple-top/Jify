# -*- coding: utf-8 -*-
"""协程级 Subagent 执行器 — 复用 model_client，同步执行，contextvars 工具隔离"""

import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

from tools.registry import registry, _subagent_whitelist

# 线程级 subagent 统计：由 SubagentRunner.run() 写入，CLI _fire_tool 读取
_subagent_stats: Dict[int, Dict] = {}


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
    ) -> str:
        """
        执行 subagent 任务并返回最终文本结果。

        Returns:
            最终回复文本；若达到最大迭代次数，返回带提示的文本。
        """
        token = _subagent_whitelist.set(whitelist_names)
        _start = time.time()
        tool_uses = 0
        recv_est = 0
        try:
            messages: List[Dict] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            # 用消息字符串长度估算初始发送 token（与主 agent 的估算方式一致）
            sent_est = len(json.dumps(messages, ensure_ascii=False))
            if on_progress:
                on_progress("token_update", {"sent": sent_est, "recv": recv_est})

            last_content = ""

            for _ in range(max_iterations):
                raw_response = self.model_client.chat(
                    messages=messages,
                    tool_schemas=whitelist_schemas,
                    model=self.config.model,
                    stream=True,
                )

                content = ""
                tool_call_chunks: Dict[int, Dict] = {}

                for chunk in raw_response:
                    if chunk.content:
                        content += chunk.content
                        recv_est += len(chunk.content)
                        if on_progress:
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
                    _subagent_stats[threading.get_ident()] = {
                        "tool_uses": tool_uses,
                        "elapsed": time.time() - _start,
                        "sent_est": sent_est,
                        "recv_est": recv_est,
                    }
                    return content

                assistant_msg = {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                }
                messages.append(assistant_msg)
                # assistant 消息在下一轮会发送给模型，提前计入 sent_est
                sent_est += len(json.dumps(assistant_msg, ensure_ascii=False))

                for tc in tool_calls:
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
                    # 每追加一条 tool 结果消息，提前计入下一轮发送量
                    sent_est += len(json.dumps(tool_msg, ensure_ascii=False))

                if on_progress:
                    on_progress("token_update", {"sent": sent_est, "recv": recv_est})

            _subagent_stats[threading.get_ident()] = {
                "tool_uses": tool_uses,
                "elapsed": time.time() - _start,
                "sent_est": sent_est,
                "recv_est": recv_est,
            }
            return (
                f"[subagent] 达到最大迭代次数 ({max_iterations})，最后回复:\n{last_content}"
            )
        finally:
            _subagent_whitelist.reset(token)
