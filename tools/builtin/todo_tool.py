# -*- coding: utf-8 -*-
"""builtin tool — update_todos：任务规划与进度追踪"""

import json
from event_bus import event_bus, UIEvent
from tools.registry import register_tool


@register_tool(
    name="update_todos",
    description="创建和管理任务列表，追踪多步骤任务的进度。用于规划复杂任务并更新执行状态。",
    parameters={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "任务列表，每项包含: content(内容), status(pending|in_progress|completed|cancelled), priority(high|medium|low), id(唯一标识)",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "任务描述"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
                        "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                        "id": {"type": "string", "description": "任务唯一标识"},
                    },
                    "required": ["content", "status", "priority", "id"],
                },
            },
        },
        "required": ["todos"],
    },
    # requires_approval=False,
)
def update_todos(todos: list) -> str:
    """更新任务列表并在终端展示进度。

    用法示例:
      - 规划任务: update_todos([{content:"创建文件", status:"pending", priority:"high", id:"1"}, ...])
      - 标记进行中: 将 status 改为 "in_progress"
      - 标记完成: 将 status 改为 "completed"
      - 取消任务: 将 status 改为 "cancelled"
    """
    event_bus.put(UIEvent("todo_update", todos))

    counts = {
        "completed": 0, "in_progress": 0, "pending": 0, "cancelled": 0,
    }
    for t in todos:
        s = t.get("status", "pending")
        counts[s] = counts.get(s, 0) + 1

    total = len(todos)
    done = counts["completed"] + counts["cancelled"]
    parts = []
    if counts["completed"]:
        parts.append(f"{counts['completed']} 已完成")
    if counts["in_progress"]:
        parts.append(f"{counts['in_progress']} 进行中")
    if counts["pending"]:
        parts.append(f"{counts['pending']} 待处理")
    if counts["cancelled"]:
        parts.append(f"{counts['cancelled']} 已取消")

    return json.dumps({
        "success": True,
        "message": f"任务列表已更新 ({done}/{total}): {', '.join(parts)}",
        "summary": counts,
    }, ensure_ascii=False)
