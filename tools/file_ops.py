# -*- coding: utf-8 -*-
"""通用文件操作 + 语法检查 — 消除三套文件读写的重复"""

import difflib
import os
import shlex
from typing import Dict, Optional, Tuple

from tools.models import LintResult

# Linters

LINTERS: Dict[str, str] = {
    ".py": "python -m py_compile {file} 2>&1",
    ".js": "node --check {file} 2>&1",
    ".ts": "npx tsc --noEmit {file} 2>&1",
    ".go": "go vet {file} 2>&1",
    ".rs": "rustfmt --check {file} 2>&1",
}


# 文件原子操作
def _count_occurrences(text: str, pattern: str) -> int:
    count = 0
    start = 0
    while True:
        pos = text.find(pattern, start)
        if pos == -1:
            break
        count += 1
        start = pos + 1
    return count


def _read_file(path: str) -> Tuple[str, Optional[str]]:
    """Read file content. Returns (content, error)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read(), None
    except Exception as e:
        return "", str(e)


def _write_file(path: str, content: str) -> Optional[str]:
    """Write file. Returns error or None."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return None
    except Exception as e:
        return str(e)


def _delete_file(path: str) -> Optional[str]:
    """Delete file. Returns error or None."""
    try:
        os.remove(path)
        return None
    except Exception as e:
        return str(e)


def _move_file(src: str, dst: str) -> Optional[str]:
    """Move file. Returns error or None."""
    try:
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        os.rename(src, dst)
        return None
    except Exception as e:
        return str(e)


def _unified_diff(old_content: str, new_content: str, filename: str) -> str:
    import re

    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
    )
    diff_lines = list(diff)

    # 解析 hunk header，给每行加上原始/新文件的行号
    hunk_header = re.compile(r'^@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@')
    result: list = []
    old_lineno = new_lineno = 0

    for line in diff_lines[2:]:
        m = hunk_header.match(line)
        if m:
            old_lineno = int(m.group(1))
            new_lineno = int(m.group(3))
            result.append(line)  # hunk header 原样保留
            continue
        if line.startswith('-'):
            result.append(f"-{old_lineno} {line[1:]}")
            old_lineno += 1
        elif line.startswith('+'):
            result.append(f"+{new_lineno} {line[1:]}")
            new_lineno += 1
        elif line.startswith('\\'):
            result.append(line)  # \ No newline at end of file
        else:
            result.append(f" {old_lineno} {line[1:]}")
            old_lineno += 1
            new_lineno += 1

    return "".join(result)


def _check_lint(path: str) -> LintResult:
    """Run syntax check on a file."""
    import subprocess

    ext = os.path.splitext(path)[1].lower()
    if ext not in LINTERS:
        return LintResult(skipped=True, message=f"No linter for {ext} files")

    linter_cmd = LINTERS[ext].format(file=shlex.quote(path))
    try:
        result = subprocess.run(
            linter_cmd, shell=True, capture_output=True, text=True, timeout=30
        )
        return LintResult(
            success=(result.returncode == 0),
            output=result.stdout + result.stderr,
        )
    except subprocess.TimeoutExpired:
        return LintResult(skipped=True, message="Linter timed out")
    except Exception as e:
        return LintResult(skipped=True, message=str(e))
