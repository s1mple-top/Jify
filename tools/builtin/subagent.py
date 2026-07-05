# -*- coding: utf-8 -*-
"""builtin tool — subagent_run """

from tools.registry import register_tool
from subagent import SubagentRunner

import os, sys as _sys

# 白名单工具: 只给 subagent 这些工具
SUBAGENT_WHITELIST = {
    "read_file",
    "write_file",
    "patch_file",
    "exec",
    "static_analysis",
}


def _build_whitelist_schemas() -> list:
    """从白名单工具名构建 tool schemas"""
    from tools.registry import registry

    schemas = []
    for name in SUBAGENT_WHITELIST:
        tool = registry.get(name)
        if tool:
            schemas.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            })
    return schemas


@register_tool(
    name="subagent_run",
    description=(
        "启动一个协程级子智能体执行任务。subagent 只能使用: "
        "read_file, write_file, patch_file, exec, static_analysis。"
        "subagent 复用主 Agent 的模型连接，在同一进程内同步执行，完成后自动销毁。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "要委托给 subagent 的具体任务描述，越具体越好",
            },
            "max_iterations": {
                "type": "integer",
                "description": "subagent 最大迭代次数。一次迭代 = 模型推理 → 执行工具 → 工具结果喂回模型，即「一轮对话」。单工具调用至少需要 2 次迭代（第 1 次执行工具，第 2 次把结果转成自然语言），复杂任务可能需要 5-10+ 次。默认 20，除非已知任务极简单，否则不要手动压低此值。",
                "default": 20,
            },
        },
        "required": ["task"],
    },
    parallel_safe=False,
    # requires_approval=False,
)
def subagent_run(task: str, max_iterations: int = 20) -> str:
    """
    启动协程级 subagent 执行指定任务。

    subagent 与主 Agent 共享 model_client，通过 contextvars 隔离工具白名单，
    执行完毕后栈帧销毁，无残留进程。
    """
    # 延迟导入避免循环依赖 (agent_loop → jify_tool → tools.builtin → subagent → agent_loop)
    from agent_loop import AgentConfig
    from model_client import get_model_client

    config = AgentConfig.load_from_yaml()
    model_client = get_model_client(
        provider=config.provider,
        api_key=config.api_key or None,
        base_url=config.base_url or None,
    )

    system_prompt = """你是一个 subagent，专门协助主 Agent 完成具体的代码任务。

规则:
- 你只能使用以下工具: read_file, write_file, patch_file, exec, static_analysis
- 直接输出任务结果，不要发送确认、感谢或追问
- 完成后直接输出最终结果文本，不需要询问下一步
- 不要调用 p2p_send、get_all_peer_names 等通信工具"""
    whitelist_schemas = _build_whitelist_schemas()

    # 构建实时状态追踪回调
    import time
    task_snippet = task[:42] + "…" if len(task) > 42 else task
    start_time = time.time()
    tool_counter = [0]

    def _on_progress(event_type: str, data: dict) -> None:
        from team import get_output_engine
        engine = get_output_engine()
        if not engine:
            return
        if event_type == "tool_start":
            tool_counter[0] += 1
        info = {
            "task": task_snippet,
            "_start": start_time,
            "tool_uses": tool_counter[0],
            "status": "running",
        }
        if event_type == "token_update":
            info["sent_est"] = data.get("sent", 0)
            info["recv_est"] = data.get("recv", 0)
        engine.set_subagent(info)

    # 立即在状态栏显示 subagent 运行中（不等工具回调）
    from team import get_output_engine as _get_oe
    _oe = _get_oe()
    if _oe:
        _oe.set_subagent({
            "task": task_snippet,
            "_start": start_time,
            "tool_uses": 0,
            "status": "running",
        })

    runner = SubagentRunner(model_client, config)
    try:
        result = runner.run(
            task=task,
            system_prompt=system_prompt,
            whitelist_schemas=whitelist_schemas,
            whitelist_names=SUBAGENT_WHITELIST,
            max_iterations=max_iterations,
            on_progress=_on_progress,
        )
        # 更新最终状态
        from team import get_output_engine
        engine = get_output_engine()
        if engine:
            engine.set_subagent({
                "task": task_snippet,
                "_start": start_time,
                "tool_uses": tool_counter[0],
                "status": "completed",
            })
        return result
    except Exception:
        from team import get_output_engine
        engine = get_output_engine()
        if engine:
            engine.set_subagent({
                "task": task_snippet,
                "_start": start_time,
                "tool_uses": tool_counter[0],
                "status": "failed",
            })
        raise
