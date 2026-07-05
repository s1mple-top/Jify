# -*- coding: utf-8 -*-
"""Team Mode 工具 — 暴露给主 Agent (Leader) 调用的 team_* 工具"""

import json

from tools.registry import register_tool
from team import get_leader



# team_delegate — 委派任务给某个 Worker
@register_tool(
    name="team_delegate",
    description=(
        "委派任务给团队中的某个 Worker Agent 并等待结果。"
        "Worker 是预先定义角色的子智能体 (如 reviewer / fixer / generator), "
        "各有独立的 system_prompt 和工具白名单。"
        "若不指定 worker_id, 自动选择空闲 Worker。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "要委派的任务描述, 越具体越好",
            },
            "worker_id": {
                "type": "string",
                "description": "指定 Worker ID (如 'reviewer', 'fixer'), 不传则自动选择空闲 Worker",
            },
            "timeout": {
                "type": "number",
                "description": "超时秒数, 默认 300",
                "default": 300,
            },
        },
        "required": ["task"],
    },
    parallel_safe=True,
    # requires_approval=False,
)
def team_delegate(task: str, worker_id: str = None, timeout: float = 300) -> str:
    leader = get_leader()
    if leader is None:
        return json.dumps({"error": "Team 模式未启动, 请先初始化 TeamOrchestrator"})
    return leader.delegate(task_content=task, worker_id=worker_id, timeout=timeout)



# team_broadcast — 广播同一任务给所有 Worker
@register_tool(
    name="team_broadcast",
    description=(
        "广播同一任务给所有 Worker, 并行执行后返回汇总结果。"
        "适用于需要多角色共同审查同一段代码的场景。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "要广播给所有 Worker 的任务",
            },
            "timeout": {
                "type": "number",
                "description": "超时秒数, 默认 300",
                "default": 300,
            },
        },
        "required": ["task"],
    },
    parallel_safe=True,
    # requires_approval=False,
)
def team_broadcast(task: str, timeout: float = 300) -> str:
    leader = get_leader()
    if leader is None:
        return json.dumps({"error": "Team 模式未启动, 请先初始化 TeamOrchestrator"})
    return leader.broadcast(task_content=task, timeout=timeout)



# team_delegate_parallel — 并行委派多个不同任务
@register_tool(
    name="team_delegate_parallel",
    description=(
        "并行委派多个不同的任务给 Worker, 等待全部完成后返回汇总。"
        "若提供 worker_ids, 任务与 Worker 按顺序一一对应; "
        "否则自动分配。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "任务描述列表",
            },
            "worker_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Worker ID 列表 (与 tasks 一一对应), 不传则自动分配",
            },
            "timeout": {
                "type": "number",
                "description": "超时秒数, 默认 300",
                "default": 300,
            },
        },
        "required": ["tasks"],
    },
    parallel_safe=True,
    # requires_approval=False,
)
def team_delegate_parallel(
    tasks: list, worker_ids: list = None, timeout: float = 300
) -> str:
    leader = get_leader()
    if leader is None:
        return json.dumps({"error": "Team 模式未启动, 请先初始化 TeamOrchestrator"})
    return leader.delegate_parallel(
        tasks=tasks, worker_ids=worker_ids, timeout=timeout
    )



# team_status — 查询所有 Worker 状态
@register_tool(
    name="team_status",
    description="查询团队中所有 Worker 的状态: 是否繁忙、已完成任务数、总耗时等",
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
    parallel_safe=True,
    # requires_approval=False,
)
def team_status() -> str:
    leader = get_leader()
    if leader is None:
        return json.dumps({"error": "Team 模式未启动, 请先初始化 TeamOrchestrator"})
    return leader.status()



# team_add_worker — 动态添加 Worker
@register_tool(
    name="team_add_worker",
    description=(
        "动态添加一个 Worker 到团队中。Worker 是预定义角色的子智能体, "
        "各有独立的 system_prompt 和工具白名单。添加后立即可用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "worker_id": {
                "type": "string",
                "description": "Worker 唯一标识 (如 'reviewer', 'fixer', 'generator')",
            },
            "system_prompt": {
                "type": "string",
                "description": "Worker 的角色 system prompt, 定义其职责和行为",
            },
            "whitelist": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Worker 可用的工具白名单 (如 ['read_file', 'patch_file', 'exec'])",
            },
            "max_iterations": {
                "type": "number",
                "description": "最大迭代次数, 默认 20",
                "default": 20,
            },
        },
        "required": ["worker_id", "system_prompt", "whitelist"],
    },
    parallel_safe=True,
    # requires_approval=False,
)
def team_add_worker(
    worker_id: str,
    system_prompt: str,
    whitelist: list,
    max_iterations: int = 20,
) -> str:
    leader = get_leader()
    if leader is None:
        return json.dumps({"error": "Team 模式未启动, 请先初始化 TeamOrchestrator"})
    try:
        leader.add_worker(
            worker_id=worker_id,
            system_prompt=system_prompt,
            whitelist=set(whitelist),
            max_iterations=max_iterations,
        )
        return json.dumps({"ok": f"Worker '{worker_id}' 已添加并启动"})
    except ValueError as e:
        return json.dumps({"error": str(e)})


# team_remove_worker — 移除指定 Worker
@register_tool(
    name="team_remove_worker",
    description=(
        "从团队中移除指定 Worker 并关闭其线程。"
        "传入 worker_id 将该 Worker 关闭并从 Worker 池中移除。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "worker_id": {
                "type": "string",
                "description": "要移除的 Worker ID",
            },
        },
        "required": ["worker_id"],
    },
    parallel_safe=True,
)
def team_remove_worker(worker_id: str) -> str:
    leader = get_leader()
    if leader is None:
        return json.dumps({"error": "Team 模式未启动, 请先初始化 TeamOrchestrator"})
    try:
        leader.shutdown(worker_id=worker_id)
        return json.dumps({"ok": f"Worker '{worker_id}' 已移除并关闭"})
    except Exception as e:
        return json.dumps({"error": str(e)})
