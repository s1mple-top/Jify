# -*- coding: utf-8 -*-
"""工具并行调度策略 — 判断一组 tool_calls 是否可以并行执行"""

from os.path import abspath
from pathlib import Path
from typing import Any, Dict, List, Optional

# 可并行的安全工具（只读、或者分析）
_PARALLEL_SAFE = frozenset({
    "read_file",
    "static_analysis",
    "mcp_list"
})

# 按路径隔离的工具（不同路径可并行）
_PATH_SCOPED = frozenset({"read_file", "write_file", "patch_file"})

_MAX_WORKERS = 8


def _extract_path(tool_name: str, args: Dict[str, Any]) -> Optional[Path]:
    """Extract the path from the tool parameters for parallel judgment"""
    if tool_name not in _PATH_SCOPED:
        return None
    raw = args.get("path", "")
    if not isinstance(raw, str) or not raw.strip():
        return None
    expanded = Path(raw).expanduser()
    if expanded.is_absolute():
        return Path(abspath(str(expanded)))
    return Path(abspath(str(Path.cwd() / expanded)))


def _paths_overlap(a: Path, b: Path) -> bool:
    """Determine whether two paths overlap (are ancestors of each other)"""
    a_parts = a.parts
    b_parts = b.parts
    common = min(len(a_parts), len(b_parts))
    return a_parts[:common] == b_parts[:common]


def _get_tc_name(tc) -> str:
    """Extract tool name from ToolCall dataclass or dict"""
    if isinstance(tc, dict):
        return tc.get("name", "")
    return getattr(tc, "name", "")


def _get_tc_args(tc) -> Dict[str, Any]:
    """Extract tool args from ToolCall dataclass or dict"""
    if isinstance(tc, dict):
        return tc.get("args", {})
    return getattr(tc, "args", {})


def should_parallel(tool_calls: List[Any]) -> bool:
    """
    判断一组 tool_calls 是否可以并行执行

    规则：
    1. 单个 tool_call -> 串行
    2. 含路径重叠的文件工具 -> 串行
    3. 其他只读工具可并行
    """
    if len(tool_calls) <= 1:
        return False

    names = [_get_tc_name(tc) for tc in tool_calls]

    reserved_paths: List[Path] = []

    for tc in tool_calls:
        name = _get_tc_name(tc)
        args = _get_tc_args(tc)

        # 路径隔离检查
        if name in _PATH_SCOPED:
            path = _extract_path(name, args)
            if path is None:
                # 路径解析失败，降级串行
                return False
            for existing in reserved_paths:
                if _paths_overlap(path, existing):
                    return False
            reserved_paths.append(path)
            continue

        # 非路径工具：必须在安全列表里才可并行
        if name not in _PARALLEL_SAFE:
            return False

    return True
