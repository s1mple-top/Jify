# -*- coding: utf-8 -*-
"""符号索引提取 —— 为 read_file 返回值追加结构化符号概览。

四层策略:
  Tier 1 — .py      → AST 精准解析 (class/def + 行号)
  Tier 2 — 主流语言  → 正则近似匹配，标注 [regex]
  Tier 3 — .md/.rst  → 标题提取
  Tier 4 — 其他      → 仅显示总行数
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════
# 扩展名 → 策略映射
# ═══════════════════════════════════════════════════════════════════

_AST_EXTS = frozenset({".py"})

_REGEX_LANG: dict[str, str] = {
    ".java":  "java",
    ".go":    "go",
    ".rs":    "rust",
    ".js":    "javascript",
    ".mjs":   "javascript",
    ".cjs":   "javascript",
    ".ts":    "typescript",
    ".tsx":   "typescript",
    ".jsx":   "javascript",
    ".c":     "c",
    ".cpp":   "cpp",
    ".cc":    "cpp",
    ".cxx":   "cpp",
    ".h":     "c",
    ".hpp":   "cpp",
    ".cs":    "csharp",
    ".swift": "swift",
    ".kt":    "kotlin",
    ".kts":   "kotlin",
}

_HEADING_EXTS = frozenset({".md", ".markdown", ".rst", ".org"})

# ═══════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════


def build_symbol_index(path: str, total_lines: int) -> str:
    """入口：根据扩展名分发到对应策略，返回要追加的索引文本。"""
    ext = Path(path).suffix.lower()

    if ext in _AST_EXTS:
        return _py_index(path, total_lines)
    if ext in _REGEX_LANG:
        return _regex_index(path, _REGEX_LANG[ext], total_lines)
    if ext in _HEADING_EXTS:
        return _heading_index(path, ext, total_lines)
    return _tier4_index(ext, total_lines)


# Tier 1 — Python AST

def _py_index(path: str, total_lines: int) -> str:
    """使用 AST 提取 Python 文件中的 class / def 符号及其行号。"""
    try:
        with open(path, encoding="utf-8") as f:
            source_lines = f.readlines()
        source = "".join(source_lines)
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError, Exception):
        return _no_symbols(total_lines, "ast parse failed")

    entries: list[tuple[int, int | None, str, str]] = []
    # (start_line, end_line | None, indent_prefix, signature)

    def get_sig(node: ast.AST) -> str:
        line = source_lines[node.lineno - 1].strip()
        if len(line) > 80:
            line = line[:77] + "..."
        return line

    def collect_class(node: ast.ClassDef, prefix: str) -> None:
        sig = get_sig(node)
        end = getattr(node, "end_lineno", None)
        entries.append((node.lineno, end, prefix, sig))
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                collect_func(child, prefix + "│  ")

    def collect_func(node: ast.FunctionDef | ast.AsyncFunctionDef, prefix: str) -> None:
        sig = get_sig(node)
        end = getattr(node, "end_lineno", None)
        entries.append((node.lineno, end, prefix, sig))

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            collect_class(node, "")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            collect_func(node, "")

    if not entries:
        return _no_symbols(total_lines, "no top-level symbols")

    return _format_entries(entries, total_lines, "ast")


# Tier 2 — 正则近似匹配
# 每个语言: [(编译后的正则, 符号类型标签), ...]
_PATTERNS: dict[str, list[tuple[re.Pattern, str]]] = {}

# Java
_PATTERNS["java"] = [
    (
        re.compile(
            r"^\s*(public|private|protected)?\s*(abstract|final)?\s*"
            r"(class|interface|@interface|enum)\s+(\w+)",
            re.MULTILINE,
        ),
        "class/interface/enum",
    ),
    (
        re.compile(
            r"^\s*(public|private|protected)?\s*(static|abstract|final|native|synchronized)?\s*"
            r"(<[\w\s,<>?]+>\s+)?[\w<>\[\],\s]+\s+(\w+)\s*\([^)]*\)",
            re.MULTILINE,
        ),
        "method",
    ),
]

# Go
_PATTERNS["go"] = [
    (
        re.compile(r"^func\s+(\(\w+\s+\*?\w+\)\s+)?(\w+)\(", re.MULTILINE),
        "func/method",
    ),
    (re.compile(r"^type\s+(\w+)\s+(struct|interface)", re.MULTILINE), "type"),
]

# Rust
_PATTERNS["rust"] = [
    (
        re.compile(r"^\s*(pub(?:\s*\(\w+\))?\s+)?(fn|struct|enum|trait|impl)\s+(\w+)", re.MULTILINE),
        "fn/struct/enum/trait/impl",
    ),
    (re.compile(r"^\s*(pub\s+)?mod\s+(\w+)", re.MULTILINE), "mod"),
]

# JavaScript / TypeScript
_PATTERNS["javascript"] = [
    (
        re.compile(r"^\s*(export\s+(default\s+)?)?(async\s+)?function\s+(\w+)", re.MULTILINE),
        "function",
    ),
    (re.compile(r"^\s*(export\s+(default\s+)?)?class\s+(\w+)", re.MULTILINE), "class"),
    (
        re.compile(r"^\s*(export\s+)?(const|let|var)\s+(\w+)\s*=\s*(async\s+)?\(", re.MULTILINE),
        "arrow/function-expr",
    ),
]

_PATTERNS["typescript"] = [
    (
        re.compile(r"^\s*(export\s+(default\s+)?)?(async\s+)?function\s+(\w+)", re.MULTILINE),
        "function",
    ),
    (re.compile(r"^\s*(export\s+(default\s+)?)?(abstract\s+)?class\s+(\w+)", re.MULTILINE), "class"),
    (re.compile(r"^\s*(export\s+)?interface\s+(\w+)", re.MULTILINE), "interface"),
    (re.compile(r"^\s*(export\s+)?type\s+(\w+)", re.MULTILINE), "type alias"),
    (re.compile(r"^\s*(export\s+)?enum\s+(\w+)", re.MULTILINE), "enum"),
]

# C
_PATTERNS["c"] = [
    (
        re.compile(
            r"^\s*(static|inline|extern|virtual)?\s*"
            r"[\w\s*]+?\s+(\w+)\s*\([^)]*\)\s*\{",
            re.MULTILINE,
        ),
        "function",
    ),
    (re.compile(r"^\s*(struct|union|enum)\s+(\w+)", re.MULTILINE), "struct/union/enum"),
]

# C++
_PATTERNS["cpp"] = [
    (
        re.compile(
            r"^\s*(virtual|static|inline|explicit|constexpr)?\s*"
            r"[\w\s*&<>:]+?\s+(\w+)\s*\([^)]*\)\s*(const\s*)?(override\s*)?(noexcept\s*)?\{",
            re.MULTILINE,
        ),
        "function/method",
    ),
    (re.compile(r"^\s*(class|struct|enum\s+class)\s+(\w+)", re.MULTILINE), "class/struct/enum"),
]

# C#
_PATTERNS["csharp"] = [
    (
        re.compile(
            r"^\s*(public|private|protected|internal)?\s*(static|abstract|sealed|partial)?\s*"
            r"(class|struct|interface|enum|record)\s+(\w+)",
            re.MULTILINE,
        ),
        "class/struct/interface/enum/record",
    ),
    (
        re.compile(
            r"^\s*(public|private|protected|internal)?\s*(static|async|override|virtual|abstract)?\s*"
            r"[\w<>\[\],\s]+\s+(\w+)\s*\([^)]*\)",
            re.MULTILINE,
        ),
        "method",
    ),
]

# Swift
_PATTERNS["swift"] = [
    (
        re.compile(r"^\s*(public|private|internal|fileprivate|open)?\s*(class|struct|enum|protocol|extension|actor)\s+(\w+)", re.MULTILINE),
        "class/struct/enum/protocol/extension/actor",
    ),
    (
        re.compile(r"^\s*(public|private|internal)?\s*(override\s+)?(class\s+)?func\s+(\w+)", re.MULTILINE),
        "func",
    ),
]

# Kotlin
_PATTERNS["kotlin"] = [
    (
        re.compile(
            r"^\s*(public|private|protected|internal)?\s*(abstract|open|data|sealed)?\s*"
            r"(class|object|interface|enum\s+class)\s+(\w+)",
            re.MULTILINE,
        ),
        "class/object/interface/enum",
    ),
    (
        re.compile(r"^\s*(suspend\s+)?(private\s+)?fun\s+(\w+)", re.MULTILINE),
        "fun",
    ),
]


def _regex_index(path: str, lang: str, total_lines: int) -> str:
    """使用正则表达式近似提取符号（best-effort）。"""
    try:
        with open(path, encoding="utf-8") as f:
            source = f.read()
    except (UnicodeDecodeError, Exception):
        return _no_symbols(total_lines, "read failed")

    patterns = _PATTERNS.get(lang, [])
    entries: list[tuple[int, int | None, str, str]] = []

    for pattern, label in patterns:
        for m in pattern.finditer(source):
            # 取捕获组里最后一个有意义的名称（通常是符号名）
            groups = [g for g in m.groups() if g is not None]
            name = groups[-1] if groups else "?"
            if name in ("if", "else", "for", "while", "switch", "return", "new", "delete",
                        "try", "catch", "throw", "case", "default", "do", "goto", "break",
                        "continue", "using", "namespace", "typedef"):
                continue  # 排除关键字误匹配
            line_no = source[: m.start()].count("\n") + 1
            all_lines = source.splitlines()
            line_text = all_lines[line_no - 1].strip()
            # 跨行误匹配修正: \s 吞掉了换行, match.start 在对齐到上一行末尾
            if not line_text:
                for i in range(line_no, len(all_lines)):
                    if all_lines[i].strip():
                        line_no = i + 1
                        line_text = all_lines[i].strip()
                        break
                else:
                    continue
            if len(line_text) > 80:
                line_text = line_text[:77] + "..."
            entries.append((line_no, None, "", f"{line_text}  [{label}]"))

    if not entries:
        return _no_symbols(total_lines, "no regex matches")

    # 按行号排序并去重（同一行可能被多个 pattern 匹配）
    entries.sort(key=lambda e: e[0])
    deduped: list[tuple[int, int | None, str, str]] = []
    seen = set()
    for e in entries:
        if e[0] not in seen:
            deduped.append(e)
            seen.add(e[0])

    return _format_entries(deduped, total_lines, "regex")


# Tier 3 — Markdown / RST 标题
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

# RST 下划线字符集合
_RST_UNDERLINE = set("=-~^'\"*+#:.")


def _heading_index(path: str, ext: str, total_lines: int) -> str:
    """提取 Markdown / RST 的标题结构。"""
    try:
        with open(path, encoding="utf-8") as f:
            source_lines = f.readlines()
    except (UnicodeDecodeError, Exception):
        return _no_symbols(total_lines, "read failed")

    entries: list[tuple[int, int | None, str, str]] = []

    if ext in (".md", ".markdown", ".org"):
        source = "".join(source_lines)
        for m in _MD_HEADING.finditer(source):
            hashes = m.group(1)
            title = m.group(2).strip()
            level = len(hashes)
            indent = "  " * (level - 1)
            line_no = source[: m.start()].count("\n") + 1
            entries.append((line_no, None, indent, f"{hashes} {title}"))

    elif ext == ".rst":
        # RST: 标题是上一行 + 下一行下划线（全部由相同标点组成）
        for i in range(1, len(source_lines)):
            prev = source_lines[i - 1].strip()
            curr = source_lines[i].strip()
            if (
                prev
                and curr
                and len(curr) >= 3
                and all(ch in _RST_UNDERLINE for ch in curr)
                and len(set(curr)) == 1  # 全部相同字符
            ):
                if len(prev) > 80:
                    prev = prev[:77] + "..."
                entries.append((i, None, "", prev))  # line_no 是下划线所在行（1-based）

    if not entries:
        return _no_symbols(total_lines, "no headings found")

    return _format_entries(entries, total_lines, "headings")


# Tier 4 — 无符号提取
def _tier4_index(ext: str, total_lines: int) -> str:
    return _no_symbols(total_lines, f"no symbols extracted for {ext} files")


# 共享 helper
def _no_symbols(total_lines: int, reason: str) -> str:
    return (
        f"\n\n--- Symbol Index ({total_lines} lines total) ---\n"
        f"  [{reason}]\n"
    )


def _format_entries(
    entries: list[tuple[int, int | None, str, str]],
    total_lines: int,
    method: str,
) -> str:
    lines: list[str] = [f"\n\n--- Symbol Index ({total_lines} lines total) ---"]
    for start, end, prefix, sig in entries:
        if end and end > start:
            lines.append(f"  L{start:<5}-L{end:<5} {prefix}{sig}")
        else:
            lines.append(f"  L{start:<5}        {prefix}{sig}")
    lines.append(f"  [extracted by {method}]")
    return "\n".join(lines)
