# -*- coding: utf-8 -*-
"""builtin tool — P2P 多智能体通信"""


from agent_p2p import init_p2p, get_p2p, is_processing_p2p_request

from tools.registry import register_tool
from event_bus import UIEvent, event_bus


def _ensure_p2p():
    """复用 agent_p2p.py 中的全局 P2P 单例，避免创建多个互不通的实例"""
    p2p = get_p2p()
    return p2p



def _collect_conversation_history(max_rounds: int = 10) -> list:
    """从当前 AgentLoop 中收集最近的对话历史"""
    try:
        from agent_loop import Jify_Agent
        messages = Jify_Agent._agent_loop.messages
        history = []
        for msg in messages[-max_rounds:]:
            history.append({
                "role": msg.role,
                "content": msg.content[:500],  # 截断过长内容
            })
        return history
    except Exception:
        return []


@register_tool(
    name="p2p_send",
    description="""发送消息给另一个 Jify 智能体并等待回复。

    使用场景:
    - 需要其他 Jify 协助处理特定任务时
    - 分发工作给专门的智能体时
    - 请求特定领域专家的帮助

    参数:
    - peer_name: 目标 Jify 的名称 (如 "Alpha", "Beta", "Expert")
    - message: 要发送的消息内容 (可以是简单的字符串描述任务)
    - wait: 是否阻塞等待回复 (默认 True)
      * True: 阻塞等待对方回复后才继续。适用于需要对方信息才能继续的场景（如对接口入参）。
      * False: 发送后立即返回 task_id。适用于双方并行工作的场景，稍后用 p2p_check 获取回复。
    - conversation_id: 可选，多轮对话复用同一个 ID
    - reply_to: 可选，回复特定消息的 ID
    - include_history: 是否自动附带当前对话历史 (默认 True)
    - context: 额外的结构化上下文 (如 files, intent 等)

    ⚠️ 关键规则：此工具的返回结果就是对方的最终交付物。
    拿到结果后你必须直接向用户汇报，严禁再次调用 p2p_send
    向对方发送确认、感谢或追问（除非用户明确要求新的任务）。
    违反此规则会导致无限对话循环。

    返回: 目标 Jify 的回复结果""",
    parameters={
        "type": "object",
        "properties": {
            "peer_name": {
                "type": "string",
                "description": "目标 Jify 的名称 (如 'Alpha', 'Beta')"
            },
            "message": {
                "type": "string",
                "description": "要发送的消息内容，描述你希望对方完成的任务"
            },
            "wait": {
                "type": "boolean",
                "description": "是否同步等待回复。True=阻塞等待(默认)，False=异步发送返回task_id"
            },
            "conversation_id": {
                "type": "string",
                "description": "多轮对话复用同一个对话ID，不传则自动生成"
            },
            "reply_to": {
                "type": "string",
                "description": "回复特定消息的ID"
            },
            "include_history": {
                "type": "boolean",
                "description": "是否自动附带当前对话历史，默认 True"
            },
            "context": {
                "type": "object",
                "description": "额外结构化上下文，如 {\"files\": [\"path/to/file.py\"], \"intent\": \"code_review\"}"
            }
        },
        "required": ["peer_name", "message"]
    },
    parallel_safe=True,
    # requires_approval=False,
)
def p2p_send(peer_name: str, message: str,
             wait: bool = True,
             conversation_id: str = None,
             reply_to: str = None,
             include_history: bool = True,
             context: dict = None) -> str:
    """
    P2P 发送消息工具 - 向指定 Jify 发送消息并等待回复

    Agent 可以使用此工具委托任务给其他 Jify 智能体。
    """
    event_bus.put(UIEvent("TEXT", "* preparing p2p_send ( " + peer_name + " " + message + " )"))
    p2p = _ensure_p2p()

    # 构建上下文
    ctx = context or {}
    if include_history:
        history = _collect_conversation_history()
        if history:
            ctx["history"] = history

    # 确定消息类型：reply_to 设定 或 正在处理 P2P 请求中 → result（不触发对方 agent）
    if reply_to or is_processing_p2p_request():
        msg_type = "result"
    else:
        msg_type = "task"

    # 异步模式：发送后立即返回 task_id
    if not wait:
        return p2p.send_to_one_async(
            target_name=peer_name,
            content=message,
            conversation_id=conversation_id,
            reply_to=reply_to,
            context=ctx,
            msg_type=msg_type,
        )

    # 同步模式：阻塞等待回复
    return p2p.send_to_one(
        target_name=peer_name,
        content=message,
        conversation_id=conversation_id,
        reply_to=reply_to,
        context=ctx,
        msg_type=msg_type,
    )


@register_tool(
    name="get_all_peer_names",
    description="""列出所有在线的 Jify 智能体。

    使用场景:
    - 查看当前有哪些 Jify 可以协作
    - 在发送消息前确认目标是否存在

    返回: 在线 Jify 名称列表 (逗号分隔)，如果没有则返回提示""",
    parameters={
        "type": "object",
        "properties": {}
    },
    parallel_safe=True,
    # requires_approval=False,
)
def get_all_peer_names() -> str:
    """P2P 列出在线节点"""
    # event_bus.put(UIEvent("TEXT", "* preparing p2p_list_peers "))

    p2p = _ensure_p2p()
    peers = p2p.get_all_peer_names()
    if not peers:
        return "当前没有其他在线的 Jify 智能体"
    return "在线的 Jify 智能体: " + ", ".join(peers)


@register_tool(
    name="p2p_check",
    description="""检查异步 P2P 任务的回复（非阻塞）。

    使用场景:
    - 使用 p2p_send(wait=False) 异步发送消息后，定期检查对方是否已回复
    - 非阻塞：如果有回复就返回，没有回复会提示"等待中"

    参数:
    - task_id: p2p_send(wait=False) 返回的 task_id

    返回:
    - 如果对方已回复: 回复内容
    - 如果尚未回复: {"status": "等待中", "提示": "对方尚未回复，可稍后再次检查"}
    - 如果 task_id 无效: {"status": "错误", "message": "..."}
    """,
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "p2p_send(wait=False) 返回的 task_id"
            }
        },
        "required": ["task_id"]
    },
    parallel_safe=True,
    # requires_approval=False,
)
def p2p_check(task_id: str) -> str:
    """检查异步 P2P 任务的回复状态（非阻塞）"""
    p2p = _ensure_p2p()
    return p2p.get_async_reply(task_id)
