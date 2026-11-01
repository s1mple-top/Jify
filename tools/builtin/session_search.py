# -*- coding: utf-8 -*-
"""
session_search — 跨会话历史召回工具（P1）。

设计要点：检索返回**原文**而非 LLM 摘要（摘要会幻觉，原文不会），
零 LLM 成本。数据来自 stream 表（P0 实时落库的全量消息流，含 tool 消息），
压缩折叠过的内容（compacted=1）默认可召回——这正是「压缩黑洞」的逆向通道。

三种调用模式（由参数推断，无显式 mode 参数）：
  1. DISCOVERY — 只传 query：FTS5 BM25 检索，按会话去重，返回命中片段 +
     会话首条用户消息预览。默认排除当前会话（当前对话内容已在上下文里）。
  2. SCROLL — 传 session_id + around_message_id：返回该消息前后各 N 条原文窗口，
     用于展开 discover 命中的上下文。翻页：锚定返回窗口的首/尾 id 再调。
  3. BROWSE — 无参数：按时间列出最近会话（含首条用户消息预览与消息量）。
"""

import json
from datetime import datetime
from typing import Any, Dict, Optional

from tools.registry import register_tool

# scroll 单窗口默认宽度（前后各 5 条）
_DEFAULT_WINDOW = 5
# browse/discover 返回条数上限
_DEFAULT_LIMIT = 8
# 单条消息展示截断（超长工具输出只给头部，需要全文时 agent 可再 scroll）
_MSG_PREVIEW_CAP = 1500


def _fmt_time(ts: Optional[str]) -> str:
    if not ts:
        return "unknown"
    try:
        return datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(ts)


def _preview(content: str, cap: int = _MSG_PREVIEW_CAP) -> str:
    content = content or ""
    if len(content) <= cap:
        return content
    return content[:cap] + f"\n…[截断，全文 {len(content)} 字符]"


def _render_window(msgs: list) -> str:
    lines = []
    for m in msgs:
        role = m["role"]
        who = f"工具[{m['tool_name']}]" if role == "tool" and m.get("tool_name") else role
        flag = " (已被压缩归档,原文保留)" if m.get("compacted") else ""
        lines.append(f"[{who}]{flag} {_preview(m['content'])}")
    return "\n".join(lines) if lines else "(窗口为空)"


def session_search(
    query: Optional[str] = None,
    session_id: Optional[str] = None,
    around_message_id: Optional[int] = None,
    window: int = _DEFAULT_WINDOW,
    limit: int = _DEFAULT_LIMIT,
    include_current: bool = False,
) -> str:
    """跨会话历史检索。三模式：query=检索 / session_id+around=翻窗口 / 无参=浏览。"""
    try:
        from storage.stream import get_stream_store, get_current_session
        store = get_stream_store()
    except Exception as e:
        return json.dumps({"success": False, "error": f"存储不可用: {e}"}, ensure_ascii=False)

    # ── SCROLL 模式 ──
    if session_id and around_message_id is not None:
        msgs = store.window(session_id, around_message_id,
                            before=window, after=window)
        payload = {
            "mode": "scroll", "session_id": session_id,
            "anchor": around_message_id, "count": len(msgs),
            "messages": [
                {"id": m["id"], "role": m["role"], "tool_name": m["tool_name"],
                 "compacted": m["compacted"], "content": _preview(m["content"])}
                for m in msgs
            ],
        }
        rng = store.id_range(session_id)
        if rng is None:
            payload["hint"] = f"会话 {session_id} 在历史流中没有任何消息，session_id 可能有误。"
        elif not msgs:
            payload["hint"] = (f"该会话无消息落在锚点附近；它在历史流中的 id 区间是 "
                               f"{rng[0]}~{rng[1]}，请用区间内的 anchor_message_id 重试。")
        elif around_message_id not in [m["id"] for m in msgs]:
            payload["hint"] = (f"锚点 {around_message_id} 不属于会话 {session_id}（该会话 id 区间 "
                               f"{rng[0]}~{rng[1]}），返回的是该会话中与锚点位置相邻的窗口。")
        return json.dumps(payload, ensure_ascii=False)

    # ── BROWSE 模式 ──
    if not query:
        try:
            from storage.db import get_db
            sessions = get_db().list_sessions(limit=limit)
        except Exception:
            sessions = []
        items = []
        for s in sessions:
            heads = store.session_heads(s["id"], n=1)
            items.append({
                "session_id": s["id"],
                "created": _fmt_time(s["created_at"]),
                "messages": store.session_message_count(s["id"]),
                "preview": heads[0] if heads else "",
            })
        return json.dumps(
            {"mode": "browse", "sessions": items}, ensure_ascii=False)

    # ── DISCOVERY 模式 ──
    exclude = None if include_current else get_current_session()
    hits = store.search(query, limit=limit, exclude_session=exclude)
    results = []
    for h in hits:
        heads = store.session_heads(h["session_id"], n=2)
        results.append({
            "session_id": h["session_id"],
            "created": _fmt_time(h["created_at"]),
            "hit_role": h["role"],
            "hit_tool": h["tool_name"],
            "compacted": h["compacted"],
            "snippet": h["snippet"],
            "session_preview": " | ".join(heads)[:200],
            "anchor_message_id": h["id"],
        })
    payload = {
        "mode": "discover", "query": query, "count": len(results),
        "results": results,
        "hint": ("用 session_id + anchor_message_id 调 scroll 模式展开命中点前后原文；"
                 "compacted=true 表示该内容已被上下文压缩、仅存在于历史流中") if results else
                "无命中。可换关键词重试，或无参调用浏览最近会话。",
    }
    return json.dumps(payload, ensure_ascii=False)


register_tool(
    name="session_search",
    description=(
        "跨会话历史检索：搜索自己过去会话的完整记录（含工具输出原文，"
        "被上下文压缩掉的内容也在内）。当用户提到'之前/上次/以前怎么做的'、"
        "需要找回历史项目的具体细节（当时的命令、报错、文件路径、结论依据）时，"
        "先用本工具检索，不要凭印象回答。"
        "三模式：传 query 做关键词检索（返回命中会话+片段+锚点）；"
        "传 session_id+around_message_id 展开该消息前后窗口原文；"
        "无参数浏览最近会话列表。检索零 LLM 成本，返回原文不是摘要。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "检索关键词（中文或英文，如 'docker 网络 修复'、'SQL 注入 payload'）",
            },
            "session_id": {
                "type": "string",
                "description": "scroll 模式：目标会话 id（来自 discover 结果）",
            },
            "around_message_id": {
                "type": "integer",
                "description": "scroll 模式：锚定消息 id（来自 discover 的 anchor_message_id），返回前后各 window 条",
            },
            "window": {
                "type": "integer",
                "description": "scroll 模式前后窗口条数，默认 5",
            },
            "limit": {
                "type": "integer",
                "description": "discover/browse 返回条数，默认 8",
            },
            "include_current": {
                "type": "boolean",
                "description": "是否包含当前会话（默认排除，当前对话内容已在上下文中）",
            },
        },
    },
)(session_search)
