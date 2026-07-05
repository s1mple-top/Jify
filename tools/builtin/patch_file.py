# -*- coding: utf-8 -*-
"""builtin tool — update/patch_file（统一文件修改入口）"""

import threading
from typing import Optional

from tools.file_ops import _read_file, _write_file, _unified_diff, _check_lint
from tools.fuzz_match import locate_and_substitute
from tools.models import PatchResult
from tools.registry import register_tool
from tools.batch_patch.applier import apply_batch_patch
from event_bus import UIEvent, event_bus

_preview_cache: dict = {}
_preview_cache_lock = threading.Lock()


def _preview_patch(path=None, old_string=None, new_string=None, replace_all=False,
                   mode="replace", patch=None):
    if mode == "replace":
        return _preview_patch_replace(path, old_string, new_string, replace_all)
    elif mode == "patch":
        return patch if patch else None
    return None


def _preview_patch_replace(path, old_string, new_string, replace_all):
    if not path or old_string is None or new_string is None:
        return None

    content, read_err = _read_file(path)
    if read_err:
        return f"[Error reading {path}: {read_err}]"

    new_content, match_count, strategy, fuzzy_err = locate_and_substitute(
        content, old_string, new_string, replace_all
    )

    if fuzzy_err:
        return f"[Error: {fuzzy_err}]"
    if match_count == 0:
        return f"[Could not find match for old_string in {path}]"

    diff = _unified_diff(content, new_content, path)

    with _preview_cache_lock:
        _preview_cache[(path, old_string, new_string)] = new_content

    return diff


def _patch_replace(
        path: Optional[str],
        old_string: Optional[str],
        new_string: Optional[str],
        replace_all: bool,
) -> PatchResult:
    if not path:
        return PatchResult(error="path required")
    if old_string is None or new_string is None:
        return PatchResult(error="old_string and new_string required")

    content, read_err = _read_file(path)
    if read_err:
        return PatchResult(error=f"Failed to read {path}: {read_err}")

    preview_key = (path, old_string, new_string)
    with _preview_cache_lock:
        cached = _preview_cache.pop(preview_key, None)

    if cached is not None:
        new_content = cached
    else:
        new_content, match_count, strategy, fuzzy_err = locate_and_substitute(
            content, old_string, new_string, replace_all
        )
        if fuzzy_err:
            return PatchResult(error=fuzzy_err)
        if match_count == 0:
            return PatchResult(error=f"Could not find match for old_string in {path}")

    write_err = _write_file(path, new_content)
    if write_err:
        return PatchResult(error=f"Failed to write changes: {write_err}")

    diff = _unified_diff(content, new_content, path)
    event_bus.put(UIEvent("DIFF", diff))

    lint_result = _check_lint(path)

    return PatchResult(
        success=True,
        diff=diff,
        files_modified=[path],
        lint=lint_result.to_dict() if lint_result else None,
    )


@register_tool(
    name="patch_file",
    description="update a file content,Use this tool for any file content changes.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to write"},
            "old_string": {"type": "string", "description": "The old content in the file that needs to be replaced."},
            "new_string": {"type": "string", "description": "The new content to replace the old."},
            "replace_all": {"type": "boolean",
                            "description": "Should all instances of \"old_content\" within the file be replaced"},
            "mode": {"type": "string", "description": "Specify edit mode"},
            "patch": {"type": "string",
                      "description": "Used only when mode=\"patch\", pass in a patch string in batch patch format."},
        },
        "required": ["path", "old_string", "new_string"],
    },
    parallel_safe=False,
    requires_approval=True,
    preview_handler=_preview_patch,
)
def patch_file(
        path: Optional[str] = None,
        old_string: Optional[str] = None,
        new_string: Optional[str] = None,
        replace_all: bool = False,
        mode: str = "replace",
        patch: Optional[str] = None,
) -> PatchResult:
    event_bus.put(UIEvent("TEXT", "* preparing patch_file ( " + str(path) + " )"))
    if mode == "replace":
        return _patch_replace(path, old_string, new_string, replace_all)
    elif mode == "patch":
        return apply_batch_patch(patch)
    else:
        return PatchResult(error=f"Unknown mode: {mode}")
