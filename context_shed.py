# -*- coding: utf-8 -*-
"""工具结果摄入侧闸门。

核心原则：**巨型工具结果不应先进入上下文再被压缩**。
一旦超过单条上限，就把全文落盘，上下文里只留 preview + 文件路径。
这样：
- 上下文从一开始就不会被一条 300K 的 exec 输出撑爆
- 全文仍可从磁盘 / stream 表完整取回，不是永久丢弃
- 闸门是确定性的（纯规则、零 LLM 调用、零延迟），不受摘要可用性影响

与压缩侧（工具结果清空、摘要）的分工：
- 本模块只做「进上下文前」的形变，不改变调用语义
- 压缩侧负责「已在上下文里」的历史内容减重
"""

import os
import re
from datetime import datetime

# 摘要消息的统一前缀。主 agent / team worker 注入的摘要消息都以它开头，
# 压缩侧（shed_tool_outputs、_compact_prefix）靠它识别并跳过，避免把摘要
# 当成普通正文再次折叠。集中定义，杜绝各处字面量漂移。
SUMMARY_HEADER = "=== 历史摘要 ==="

# 落盘目录（与旧版本 tool_dumps 保持同一位置，便于统一清理）
TOOL_DUMPS_DIR = os.path.expanduser("~/.jify/tool_dumps")

# 单条工具结果落盘时，上下文中保留的 preview 字符数
DEFAULT_PREVIEW_CHARS = 2000

# 落盘目录保留的文件数上限：超出后按 mtime 删最老的。
# 原文价值随时间衰减，而目录无上限会缓慢吃满磁盘；这是唯一的清理点。
MAX_DUMP_FILES = 500

# 落盘失败时的兜底：头尾各保留一段，中间标注省略，保证不丢失尾部（错误常在尾部）
_HEAD_FALLBACK = 1500
_TAIL_FALLBACK = 500


def _safe_name(s: str) -> str:
    """把工具名/调用 id 收敛成安全文件名片段。"""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s or "unknown")[:64]


def _preview_at_boundary(content: str, limit: int) -> tuple:
    """按行边界截取 preview。

    Returns:
        (preview, has_more)：has_more 表示原文还有未展示内容
    """
    if len(content) <= limit:
        return content, False
    cut = content.rfind("\n", 0, limit)
    # 找不到合理换行点时退化为硬切，避免 preview 只剩几十字节
    if cut < limit // 2:
        cut = limit
    return content[:cut], True


def persist_large_result(session_id: str, tool_name: str, tool_call_id: str,
                         content: str, max_chars: int,
                         preview_chars: int = DEFAULT_PREVIEW_CHARS) -> str:
    """单条工具结果超限时落盘，返回用于上下文的替代文本。

    未超限时原样返回 content（零开销，不产生任何磁盘 IO）。

    Args:
        session_id: 会话标识，用于组织落盘文件
        tool_name: 工具名（写入替代文本，让模型知道这是什么结果）
        tool_call_id: 调用 id，用于文件名去重与定位
        content: 原始工具结果
        max_chars: 单条上限，<= 0 表示关闭闸门
        preview_chars: 上下文中保留的 preview 字符数

    Returns:
        未超限 -> 原文；超限 -> preview 包裹文本
    """
    if max_chars <= 0 or len(content) <= max_chars:
        return content

    original_len = len(content)
    preview, has_more = _preview_at_boundary(content, max(1, preview_chars))
    path = ""

    try:
        os.makedirs(TOOL_DUMPS_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        fname = (f"{_safe_name(session_id)}_{stamp}_"
                 f"{_safe_name(tool_name)}_{_safe_name(tool_call_id)}.txt")
        path = os.path.join(TOOL_DUMPS_DIR, fname)
        # 只写一次，不覆盖；写入失败走兜底分支
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        path = ""

    if path:
        _prune_dumps()

    if not path:
        # 落盘失败：保留头尾（尾部常含报错/退出码），绝不静默丢成空串
        head = content[:_HEAD_FALLBACK]
        tail = content[-_TAIL_FALLBACK:] if original_len > _HEAD_FALLBACK + _TAIL_FALLBACK else ""
        body = head + ("\n... [中间省略] ...\n" + tail if tail else "")
        return _wrap(tool_name, original_len, "", body, False)

    return _wrap(tool_name, original_len, path, preview, has_more)


def _prune_dumps() -> None:
    """落盘目录超过上限时按 mtime 删最老的。

    只在真正落盘后调用（低频路径），清理失败不影响主流程——
    最坏情况是目录偏大，而不是工具结果丢失。
    """
    try:
        names = os.listdir(TOOL_DUMPS_DIR)
        if len(names) <= MAX_DUMP_FILES:
            return
        paths = [(p, os.path.getmtime(p)) for p in
                 (os.path.join(TOOL_DUMPS_DIR, n) for n in names)]
        paths.sort(key=lambda x: x[1])
        for p, _ in paths[:len(paths) - MAX_DUMP_FILES]:
            try:
                os.remove(p)
            except OSError:
                pass
    except Exception:
        pass


def _wrap(tool_name: str, original_len: int, path: str,
          body: str, has_more: bool) -> str:
    """组装上下文中使用的替代文本。"""
    if path:
        header = (
            f"[工具结果过大，已落盘] 工具: {tool_name or 'unknown'}，"
            f"原始 {original_len} 字符。\n"
            f"完整输出: {path}\n"
            f"可用 read_file 按需读取该路径，或直接用 grep/exec 检索。\n"
            f"下方为预览（前 {len(body)} 字符）：\n"
            f"----- preview start -----\n"
        )
    else:
        header = (
            f"[工具结果过大且落盘失败] 工具: {tool_name or 'unknown'}，"
            f"原始 {original_len} 字符。下方仅保留头尾片段，"
            f"如需完整结果请缩小命令输出范围后重跑。\n"
            f"----- preview start -----\n"
        )
    tail = "\n----- preview end -----" if has_more else "\n----- end -----"
    return header + body + tail


def _get_role(m) -> str:
    """兼容 Message dataclass 与 plain dict 两种消息形态。"""
    return m.get("role", "") if isinstance(m, dict) else getattr(m, "role", "")


def _get_content(m) -> str:
    v = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
    return v or ""


def _set_content(m, value: str) -> None:
    if isinstance(m, dict):
        m["content"] = value
    else:
        m.content = value


def _get_tool_calls(m) -> list:
    v = m.get("tool_calls") if isinstance(m, dict) else getattr(m, "tool_calls", None)
    return v or []


def _get_tool_call_id(m) -> str:
    v = m.get("tool_call_id") if isinstance(m, dict) else getattr(m, "tool_call_id", None)
    return v or ""


def shed_tool_outputs(messages, config, notify=None, session_id=None,
                      protect_tail_from=None, require_converge=True) -> tuple:
    """级 1 减重：把早期工具结果内容换成占位符（零 LLM 调用）。

    主 agent / subagent / team worker 共用同一实现，消息既可是 Message
    dataclass 也可是 plain dict（subagent、team 用后者）。

    设计要点：只改 tool 消息的 content，消息本身与 tool_call_id 全部保留，
    因此 assistant(tool_calls) / tool 的配对关系天然不被破坏。

    按条数触发，一次清到保留窗口（而非按水位逐条清）：清空会改变前缀内容、
    破坏远程前缀缓存，逐条清空的累计代价远高于一次清够。

    Args:
        messages: 消息列表（Message 或 dict）
        config: AgentConfig，读取 tool_shed_* 与 context_compress_threshold
        notify: 可选回调 (str) -> None，用于提示清理结果
        session_id: 有值时把被清消息标记到 stream 表（主 agent 用，其余传 None）
        protect_tail_from: 该下标之后的 tool 消息不清（主 agent 传本轮 user 下标，
            保留工作现场）；None 表示只按 keep_recent 条数保护
        require_converge: True 时「清完仍超阈值」就不动手（把原文留给摘要级，
            折叠能连结构一起压）；subagent 没有摘要级兜底，须传 False——
            只要收益够就清，清了总比不清好

    Returns:
        (changed, saved_chars)：changed 为 True 表示确实清空了一批内容。
    """
    if not getattr(config, "tool_shed_enabled", True):
        return False, 0

    threshold = getattr(config, "context_compress_threshold", 1800000)
    keep_recent = max(1, getattr(config, "tool_shed_keep_recent", 10))
    total_before = sum(len(str(m)) for m in messages)
    if total_before <= threshold:
        return False, 0  # 未超阈值，没有减压必要

    # 跳过 system 与注入的摘要消息
    start = 0
    while start < len(messages) and _get_role(messages[start]) == "system":
        start += 1
    if (start < len(messages)
            and str(_get_content(messages[start])).startswith(SUMMARY_HEADER)):
        start += 1

    # tool_call_id -> 工具名，用于让占位符带上「这里查过什么」
    name_by_id = {}
    for m in messages:
        for tc in _get_tool_calls(m):
            if not isinstance(tc, dict):
                continue
            fid = tc.get("id")
            fn = tc.get("function") or {}
            if fid:
                name_by_id[fid] = fn.get("name") or "unknown"

    limit = len(messages) if protect_tail_from is None else min(protect_tail_from, len(messages))
    candidates = []
    for i in range(start, limit):
        m = messages[i]
        if _get_role(m) != "tool":
            continue
        content = _get_content(m)
        if not content:
            continue
        if content.startswith("[旧工具结果已清理]"):
            continue  # 已清过
        candidates.append((i, len(content)))

    # 条数触发：攒够一批才动手，避免每迭代清一条导致前缀反复改写
    if len(candidates) <= getattr(config, "tool_shed_trigger_count", 20):
        return False, 0

    # 先试算（占位符写入不可逆）：清空除最近 keep_recent 条之外的全部
    plan = []
    saved = 0
    for idx, ln in candidates[:-keep_recent]:
        name = name_by_id.get(_get_tool_call_id(messages[idx]), "tool")
        placeholder = shed_placeholder(name, ln)
        gain = ln - len(placeholder)
        if gain <= 0:
            continue
        plan.append((idx, placeholder))
        saved += gain

    # 收益门槛：清空会破坏前缀缓存，节省太小不值得
    if not plan or saved < total_before * 0.05:
        return False, 0
    # 收敛门槛：清完仍超阈值说明近期内容本身就撑爆了，级 1 帮不上，
    # 把原文留给摘要路径（折叠能连结构一起压，收益更大）。
    if require_converge and total_before - saved > threshold:
        return False, 0

    shed_ids = []
    for idx, placeholder in plan:
        m = messages[idx]
        _set_content(m, placeholder)
        sid = getattr(m, "_stream_id", None)
        if sid:
            shed_ids.append(sid)

    # 活上下文已无原文，标记 compacted=1（原文仍在 stream 表，可检索）
    if shed_ids and session_id:
        try:
            from storage.stream import get_stream_store
            get_stream_store().mark_compacted(session_id, shed_ids)
        except Exception:
            pass

    if notify:
        notify(
            f"[compact] 已清理 {len(plan)} 条早期工具结果，"
            f"节省约 {saved} 字符（原文可 session_search 召回）"
        )
    return True, saved


def shed_placeholder(tool_name: str, original_len: int) -> str:
    """历史工具结果被清空后使用的最小占位符。

    不能是空串：空 content 会让模型误判为工具执行失败并重复调用。
    带上工具名与原始体积，让模型知道「这里查过什么、有多大的结果」。
    """
    return (
        f"[旧工具结果已清理] 工具: {tool_name or 'unknown'}，"
        f"原始 {original_len} 字符。原文可用 session_search 按关键词召回。"
    )
