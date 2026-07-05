# -*- coding: utf-8 -*-
"""批量补丁子系统 — 解析器

支持对多文件的原子性批量修改：Update / Add / Delete / Move。
"""

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, List, Optional, Tuple


# 预编译正则
_RE_BEGIN = re.compile(r"\*{3}\s*Begin\s+Patch", re.IGNORECASE)
_RE_END = re.compile(r"\*{3}\s*End\s+Patch", re.IGNORECASE)
_RE_HUNK_HINT = re.compile(r"@@\s*(.+?)\s*@@")

# 操作头匹配: 一行一个操作类型
_OP_PATTERNS: List[Tuple[re.Pattern, str, bool]] = [
    (re.compile(r"\*{3}\s*Update\s+File:\s*(.+)", re.IGNORECASE), "Update", False),
    (re.compile(r"\*{3}\s*Add\s+File:\s*(.+)", re.IGNORECASE), "Add", False),
    (re.compile(r"\*{3}\s*Delete\s+File:\s*(.+)", re.IGNORECASE), "Delete", True),
    (re.compile(r"\*{3}\s*Move\s+File:\s*(.+?)\s*->\s*(.+)", re.IGNORECASE), "Move", True),
]

_RE_UNIFIED_HUNK = re.compile(r"^@@\s*(.+?)\s*@@")


# 数据模型

class OperationType:
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"
    MOVE = "move"


@dataclass
class HunkLine:
    prefix: str   # " ", "+", "-"
    content: str


@dataclass
class Hunk:
    context_hint: Optional[str] = None
    lines: List[HunkLine] = field(default_factory=list)


@dataclass
class PatchOperation:
    operation: str
    file_path: str
    new_path: Optional[str] = None
    hunks: List[Hunk] = field(default_factory=list)
    content: Optional[str] = None



# 解析

def _flush_op(
    current_op: Optional[PatchOperation],
    current_hunk: Optional[Hunk],
    operations: List[PatchOperation],
) -> Tuple[Optional[PatchOperation], Optional[Hunk]]:
    """将当前操作及最后一个 hunk 刷入列表，返回清空后的状态。"""
    if current_op is not None:
        if current_hunk is not None and current_hunk.lines:
            current_op.hunks.append(current_hunk)
        operations.append(current_op)
    return None, None


def _parse_op_header(line: str) -> Optional[Tuple[str, str, Optional[str]]]:
    """尝试匹配操作头行，返回 (op_type, file_path, new_path_or_None) 或 None。"""
    for pattern, op_type, _ in _OP_PATTERNS:
        m = pattern.match(line)
        if m:
            groups = m.groups()
            if op_type == "Move":
                return (op_type, groups[0].strip(), groups[1].strip())
            return (op_type, groups[0].strip(), None)
    return None


def parse_batch_patch(patch_content: str) -> Tuple[List[PatchOperation], Optional[str]]:
    """解析批量 patch 格式。

    Returns:
        (operations, error_message) — error_message 为 None 表示解析成功。
    """
    lines = patch_content.split("\n")
    operations: List[PatchOperation] = []

    # 定位 Begin / End 边界
    start_idx = -1
    end_idx = len(lines)
    for i, line in enumerate(lines):
        if start_idx < 0 and _RE_BEGIN.search(line):
            start_idx = i
        elif _RE_END.search(line):
            end_idx = i
            break

    current_op: Optional[PatchOperation] = None
    current_hunk: Optional[Hunk] = None

    for i in range(start_idx + 1, end_idx):
        line = lines[i]

        # 操作头
        op_info = _parse_op_header(line)
        if op_info is not None:
            op_type, file_path, new_path = op_info
            current_op, current_hunk = _flush_op(current_op, current_hunk, operations)
            current_op = PatchOperation(
                operation=getattr(OperationType, op_type.upper()),
                file_path=file_path,
                new_path=new_path,
            )
            if op_type == "Add":
                current_hunk = Hunk()
            continue

        # @@ hunk 头
        if line.startswith("@@"):
            if current_op is not None:
                if current_hunk is not None and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                m = _RE_HUNK_HINT.match(line)
                current_hunk = Hunk(context_hint=m.group(1) if m else None)
            continue

        # 内容行
        if current_op is not None and line:
            if current_hunk is None:
                current_hunk = Hunk()
            if line.startswith("+") or line.startswith("-") or line.startswith(" "):
                current_hunk.lines.append(HunkLine(line[0], line[1:]))
            elif line.startswith("\\"):
                pass  # 忽略 \ No newline at end of file
            else:
                current_hunk.lines.append(HunkLine(" ", line))

    # 收尾
    _flush_op(current_op, current_hunk, operations)

    # 无 Begin marker → 不是有效的 batch patch
    if start_idx < 0:
        return [], "Not a valid batch patch: missing '*** Begin Patch' marker"

    if not operations:
        return [], "No operations found in patch"

    # 基础校验
    parse_errors: List[str] = []
    for op in operations:
        if not op.file_path:
            parse_errors.append("Operation with empty file path")
        if op.operation == OperationType.UPDATE and not op.hunks:
            parse_errors.append(f"UPDATE {op.file_path!r}: no hunks found")
        if op.operation == OperationType.MOVE and not op.new_path:
            parse_errors.append(f"MOVE {op.file_path!r}: missing destination path")

    if parse_errors:
        return [], "Parse error: " + "; ".join(parse_errors)

    return operations, None
