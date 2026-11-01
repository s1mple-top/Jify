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


def shed_placeholder(tool_name: str, original_len: int) -> str:
    """历史工具结果被清空后使用的最小占位符。

    不能是空串：空 content 会让模型误判为工具执行失败并重复调用。
    带上工具名与原始体积，让模型知道「这里查过什么、有多大的结果」。
    """
    return (
        f"[旧工具结果已清理] 工具: {tool_name or 'unknown'}，"
        f"原始 {original_len} 字符。原文可用 session_search 按关键词召回。"
    )
