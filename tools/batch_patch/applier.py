# -*- coding: utf-8 -*-
"""
批量补丁子系统 — 应用引擎
"""

import difflib
from typing import Any, Dict, List, Optional, Tuple

from tools.file_ops import (
    _count_occurrences, _read_file, _write_file,
    _delete_file, _move_file, _unified_diff, _check_lint,
)
from tools.fuzz_match import locate_and_substitute
from tools.models import PatchResult
from tools.batch_patch import parse_batch_patch, OperationType, Hunk, PatchOperation


# 入口

def apply_batch_patch(patch_content: Optional[str]) -> PatchResult:
    """应用批量 patch 内容。先解析，再两阶段执行。"""
    if not patch_content:
        return PatchResult(error="patch content required")

    operations, parse_err = parse_batch_patch(patch_content)
    if parse_err:
        return PatchResult(error=f"Failed to parse patch: {parse_err}")

    file_ops = _PatchFileOps()
    return _apply_operations(operations, file_ops)


# 内部: 文件操作抽象

class _PatchFileResult:
    """单个文件操作的结果。"""

    def __init__(self, content: str, error: Optional[str]):
        self.content = content
        self.error = error


class _PatchFileOps:
    """对 file_ops 的薄封装，统一返回 _PatchFileResult。"""

    def read(self, path: str) -> _PatchFileResult:
        c, e = _read_file(path)
        return _PatchFileResult(c, e)

    def write(self, path: str, content: str) -> _PatchFileResult:
        e = _write_file(path, content)
        return _PatchFileResult(content, e)

    def delete(self, path: str) -> _PatchFileResult:
        e = _delete_file(path)
        return _PatchFileResult("", e)

    def move(self, src: str, dst: str) -> _PatchFileResult:
        e = _move_file(src, dst)
        return _PatchFileResult("", e)


# 辅助: hunk → (search_pattern, replacement)

def _build_hunk_patterns(hunk: Hunk) -> Tuple[Optional[str], Optional[str]]:
    """从 hunk 中提取搜索模式与替换文本。

    search_pattern 由 ' ' 行和 '-' 行拼接而成，
    replacement 由 ' ' 行和 '+' 行拼接而成。

    Returns:
        (search_pattern, replacement) —
        search_pattern 为 None 表示纯新增 hunk。
    """
    search_lines = [l.content for l in hunk.lines if l.prefix in (" ", "-")]
    replace_lines = [l.content for l in hunk.lines if l.prefix in (" ", "+")]

    if search_lines:
        return "\n".join(search_lines), "\n".join(replace_lines)
    return None, "\n".join(replace_lines)


# 验证

def _validate_operations(operations: List[PatchOperation], file_ops: _PatchFileOps) -> List[str]:
    """模拟应用所有操作，收集验证错误。"""
    errors: List[str] = []

    for op in operations:
        if op.operation == OperationType.UPDATE:
            errors.extend(_validate_update(op, file_ops))
        elif op.operation == OperationType.DELETE:
            res = file_ops.read(op.file_path)
            if res.error:
                errors.append(f"{op.file_path}: file not found for deletion")
        elif op.operation == OperationType.MOVE:
            if not op.new_path:
                errors.append(f"{op.file_path}: MOVE operation missing destination path")
                continue
            src_res = file_ops.read(op.file_path)
            if src_res.error:
                errors.append(f"{op.file_path}: source file not found for move")
            dst_res = file_ops.read(op.new_path)
            if not dst_res.error:
                errors.append(f"{op.new_path}: destination already exists — move would overwrite")

    return errors


def _validate_update(op: PatchOperation, file_ops: _PatchFileOps) -> List[str]:
    errors: List[str] = []
    res = file_ops.read(op.file_path)
    if res.error:
        errors.append(f"{op.file_path}: {res.error}")
        return errors

    simulated = res.content
    for hunk in op.hunks:
        search_pattern, replacement = _build_hunk_patterns(hunk)

        if search_pattern is None:
            # 纯新增 hunk：仅校验 context_hint
            if hunk.context_hint:
                occ = _count_occurrences(simulated, hunk.context_hint)
                if occ == 0:
                    errors.append(
                        f"{op.file_path}: addition-only hunk context hint "
                        f"'{hunk.context_hint}' not found"
                    )
                elif occ > 1:
                    errors.append(
                        f"{op.file_path}: addition-only hunk context hint "
                        f"'{hunk.context_hint}' is ambiguous ({occ} occurrences)"
                    )
            continue

        new_sim, count, _, match_err = locate_and_substitute(
            simulated, search_pattern, replacement, replace_all=False
        )
        if count == 0:
            label = f"'{hunk.context_hint}'" if hunk.context_hint else "(no hint)"
            errors.append(
                f"{op.file_path}: hunk {label} not found"
                + (f" — {match_err}" if match_err else "")
            )
        else:
            simulated = new_sim

    return errors


# 应用
# Dispatch 表: op_type → apply handler
_APPLY_HANDLERS: Dict[str, Any] = {}


def _register(op_type: str):
    def decorator(fn):
        _APPLY_HANDLERS[op_type] = fn
        return fn
    return decorator


@_register(OperationType.ADD)
def _apply_add_file(op: PatchOperation, file_ops: _PatchFileOps) -> Tuple[bool, str]:
    content_lines = []
    for hunk in op.hunks:
        for line in hunk.lines:
            if line.prefix == "+":
                content_lines.append(line.content)
    content = "\n".join(content_lines)
    res = file_ops.write(op.file_path, content)
    if res.error:
        return False, res.error
    diff = f"--- /dev/null\n+++ b/{op.file_path}\n"
    diff += "\n".join(f"+{line}" for line in content_lines)
    return True, diff


@_register(OperationType.DELETE)
def _apply_delete_file(op: PatchOperation, file_ops: _PatchFileOps) -> Tuple[bool, str]:
    res = file_ops.read(op.file_path)
    if res.error:
        return False, f"Cannot delete {op.file_path}: file not found"
    err = file_ops.delete(op.file_path).error
    if err:
        return False, err
    removed_lines = res.content.splitlines(keepends=True)
    diff = "".join(difflib.unified_diff(
        removed_lines, [],
        fromfile=f"a/{op.file_path}",
        tofile="/dev/null",
    ))
    return True, diff or f"# Deleted: {op.file_path}"


@_register(OperationType.MOVE)
def _apply_move_file(op: PatchOperation, file_ops: _PatchFileOps) -> Tuple[bool, str]:
    res = file_ops.move(op.file_path, op.new_path)
    if res.error:
        return False, res.error
    return True, f"# Moved: {op.file_path} -> {op.new_path}"


@_register(OperationType.UPDATE)
def _apply_update_file(op: PatchOperation, file_ops: _PatchFileOps) -> Tuple[bool, str]:
    res = file_ops.read(op.file_path)
    if res.error:
        return False, f"Cannot read file: {res.error}"

    current = res.content
    new_content = current

    for hunk in op.hunks:
        search_pattern, replacement = _build_hunk_patterns(hunk)

        if search_pattern is not None:
            new_content, count, _, error = locate_and_substitute(
                new_content, search_pattern, replacement, replace_all=False
            )
            if error and count == 0:
                # 尝试通过 context_hint 缩小搜索窗口
                if hunk.context_hint:
                    new_content, error = _retry_with_hint_window(
                        new_content, hunk, search_pattern, replacement
                    )
                if error:
                    return False, f"Could not apply hunk: {error}"
        else:
            # 纯新增 hunk
            insert_text = replacement
            if hunk.context_hint:
                occ = _count_occurrences(new_content, hunk.context_hint)
                if occ == 0:
                    new_content = new_content.rstrip("\n") + "\n" + insert_text + "\n"
                elif occ > 1:
                    return False, (
                        f"Addition-only hunk: context hint '{hunk.context_hint}' "
                        f"is ambiguous ({occ} occurrences)"
                    )
                else:
                    hint_pos = new_content.find(hunk.context_hint)
                    eol = new_content.find("\n", hint_pos)
                    if eol != -1:
                        new_content = (
                            new_content[:eol + 1]
                            + insert_text + "\n"
                            + new_content[eol + 1:]
                        )
                    else:
                        new_content = new_content + "\n" + insert_text
            else:
                new_content = new_content.rstrip("\n") + "\n" + insert_text + "\n"

    write_err = file_ops.write(op.file_path, new_content).error
    if write_err:
        return False, write_err

    diff_lines = difflib.unified_diff(
        current.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"a/{op.file_path}",
        tofile=f"b/{op.file_path}",
    )
    return True, "".join(diff_lines)


def _retry_with_hint_window(
    content: str, hunk: Hunk, search_pattern: str, replacement: str
) -> Tuple[str, Optional[str]]:
    """通过 context_hint 定位窗口，在窗口内重试匹配。

    Returns (new_content, error_or_None).
    """
    hint_pos = content.find(hunk.context_hint)
    if hint_pos == -1:
        return content, f"Could not apply hunk: context hint '{hunk.context_hint}' not found"

    window_start = max(0, hint_pos - 500)
    window_end = min(len(content), hint_pos + 2000)
    window = content[window_start:window_end]

    window_new, count, _, error = locate_and_substitute(
        window, search_pattern, replacement, replace_all=False
    )
    if count > 0:
        return (
            content[:window_start] + window_new + content[window_end:],
            None,
        )
    return content, error


# 两阶段编排
def _apply_operations(operations: List[PatchOperation], file_ops: _PatchFileOps) -> PatchResult:
    """两阶段应用: 验证通过后才执行。"""

    # validate
    validation_errors = _validate_operations(operations, file_ops)
    if validation_errors:
        return PatchResult(
            success=False,
            error="Patch validation failed (no files were modified):\n"
                  + "\n".join(f"  • {e}" for e in validation_errors),
        )

    # apply
    files_modified: List[str] = []
    files_created: List[str] = []
    files_deleted: List[str] = []
    all_diffs: List[str] = []
    errors: List[str] = []

    for op in operations:
        handler = _APPLY_HANDLERS.get(op.operation)
        if handler is None:
            errors.append(f"Unknown operation type: {op.operation}")
            continue

        try:
            ok, diff = handler(op, file_ops)
        except Exception as e:
            errors.append(f"Error processing {op.file_path}: {str(e)}")
            continue

        if not ok:
            errors.append(f"Failed on {op.file_path}: {diff}")
            continue

        # 按操作类型分类记录
        if op.operation == OperationType.ADD:
            files_created.append(op.file_path)
        elif op.operation == OperationType.DELETE:
            files_deleted.append(op.file_path)
        elif op.operation == OperationType.MOVE:
            files_modified.append(f"{op.file_path} -> {op.new_path}")
        else:
            files_modified.append(op.file_path)
        all_diffs.append(diff)

    combined_diff = "\n".join(all_diffs)

    # Lint
    lint_results: Dict[str, Any] = {}
    for f in files_modified + files_created:
        try:
            lr = _check_lint(f)
            lint_results[f] = lr.to_dict()
        except Exception:
            pass

    if errors:
        return PatchResult(
            success=False,
            diff=combined_diff,
            files_modified=files_modified,
            files_created=files_created,
            files_deleted=files_deleted,
            lint=lint_results if lint_results else None,
            error="Apply phase failed:\n" + "\n".join(f"  • {e}" for e in errors),
        )

    return PatchResult(
        success=True,
        diff=combined_diff,
        files_modified=files_modified,
        files_created=files_created,
        files_deleted=files_deleted,
        lint=lint_results if lint_results else None,
    )
