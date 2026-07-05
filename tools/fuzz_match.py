# -*- coding: utf-8 -*-
"""
容错搜索替换引擎 —— patch_file 的匹配核心，解决因为幻觉导致的无法匹配 patch 问题
部分代码可能是非标准格式，然而模型输出往往是标准格式，会导致匹配问题，故而要做容错处理
"""

import re
from difflib import SequenceMatcher
from typing import Callable, Dict, List, Optional, Tuple


# Unicode Normalisation
UNICODE_MAP: Dict[str, str] = {
    "\u201c": '"', "\u201d": '"',
    "\u2018": "'", "\u2019": "'",
    "\u2014": "--", "\u2013": "-",
    "\u2026": "...", "\u00a0": " ",
}


def _fold_unicode(text: str) -> str:
    for char, repl in UNICODE_MAP.items():
        text = text.replace(char, repl)
    return text


# Main API
'''
容错性的 find-and-replace 引擎，解决 LLM 生成的 old_string
与文件中实际内容存在微小差异（空白、缩进、Unicode、换行等）时无法匹配的问题
'''
def locate_and_substitute(
        content: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
) -> Tuple[str, int, Optional[str], Optional[str]]:

    if not old_string:
        return content, 0, None, "old_string cannot be empty"
    if old_string == new_string:
        return content, 0, None, "old_string and new_string are identical"

    matchers: List[Tuple[str, Callable]] = [
        ("literal",              _match_literal),
        ("stripped_lines",       _match_stripped_lines),
        ("joined_lines",         _match_joined_lines),
        ("collapsed_whitespace", _match_collapsed_whitespace),
        ("ignore_indent",        _match_ignore_indent),
        ("unescaped",            _match_unescaped),
        ("line_boundaries",      _match_line_boundaries),
        ("ascii_fallback",       _match_ascii_fallback),
        ("anchored_block",       _match_anchored_block),
        ("fuzzy_context",        _match_fuzzy_context),
    ]

    for matcher_name, matcher_fn in matchers:
        matches = matcher_fn(content, old_string)
        if matches:
            if len(matches) > 1 and not replace_all:
                return content, 0, None, (
                    f"Found {len(matches)} matches. "
                    f"Provide more context or use replace_all=True."
                )
            new_content = _execute_substitutions(content, matches, new_string)
            return new_content, len(matches), matcher_name, None

    return content, 0, None, "Could not find a match for old_string in the file"


def _execute_substitutions(
        content: str, matches: List[Tuple[int, int]], new_string: str
) -> str:
    sorted_matches = sorted(matches, key=lambda x: x[0], reverse=True)
    result = content
    for start, end in sorted_matches:
        result = result[:start] + new_string + result[end:]
    return result



def _match_literal(content: str, pattern: str) -> List[Tuple[int, int]]:
    """Exact substring search."""
    matches = []
    start = 0
    while True:
        pos = content.find(pattern, start)
        if pos == -1:
            break
        matches.append((pos, pos + len(pattern)))
        start = pos + 1
    return matches


def _match_stripped_lines(content: str, pattern: str) -> List[Tuple[int, int]]:
    """
     模型输出代码行尾多了不可见空白
    """
    pattern_lines = [line.strip() for line in pattern.split("\n")]
    pattern_normalized = "\n".join(pattern_lines)
    content_lines = content.split("\n")
    content_normalized_lines = [line.strip() for line in content_lines]
    return _search_normalized_lines(
        content, content_lines, content_normalized_lines,
        pattern, pattern_normalized
    )


def _collapse_spaces(s: str) -> str:
    return re.sub(r"[ \t]+", " ", s)


def _match_joined_lines(content: str, pattern: str) -> List[Tuple[int, int]]:
    """
    解决 LLM 错误地把本应在一行的代码拆成了多行的问题
    对 pattern 做处理
    """
    pattern_lines = pattern.split("\n")
    if len(pattern_lines) <= 1:
        return []

    joined = " ".join(
        _collapse_spaces(line) for line in pattern_lines
    )
    joined_normalized = _collapse_spaces(joined)
    if not joined_normalized.strip():
        return []

    content_lines = content.split("\n")
    normalized_lines = [_collapse_spaces(line) for line in content_lines]

    matches = []
    for i, norm_line in enumerate(normalized_lines):
        if norm_line.strip() == joined_normalized.strip():
            start_pos, end_pos = _compute_line_range(
                content_lines, i, i + 1, len(content)
            )
            matches.append((start_pos, end_pos))

    return matches


def _match_collapsed_whitespace(content: str, pattern: str) -> List[Tuple[int, int]]:
    """Collapse whitespace in both content and pattern, then exact-match."""
    pattern_normalized = _collapse_spaces(pattern)
    content_normalized = _collapse_spaces(content)
    hits = _match_literal(content_normalized, pattern_normalized)
    if not hits:
        return []
    return _resolve_whitespace_positions(content, content_normalized, hits)


def _match_ignore_indent(content: str, pattern: str) -> List[Tuple[int, int]]:
    """Strip leading whitespace from every line before comparing."""
    content_lines = content.split("\n")
    content_stripped = [line.lstrip() for line in content_lines]
    pattern_lines = [line.lstrip() for line in pattern.split("\n")]
    return _search_normalized_lines(
        content, content_lines, content_stripped,
        pattern, "\n".join(pattern_lines)
    )


def _literal_to_actual(s: str) -> str:
    """Convert literal backslash sequences to actual characters."""
    return s.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")


def _match_unescaped(content: str, pattern: str) -> List[Tuple[int, int]]:
    """Match after unescaping literal \\n / \\t / \\r in the pattern."""
    resolved = _literal_to_actual(pattern)
    if resolved == pattern:
        return []
    return _match_literal(content, resolved)


def _match_line_boundaries(content: str, pattern: str) -> List[Tuple[int, int]]:
    """Strip each line before line-level comparison."""
    pattern_lines = [line.strip() for line in pattern.split("\n")]
    if not pattern_lines:
        return []

    condensed = "\n".join(pattern_lines)
    content_lines = content.split("\n")
    n = len(pattern_lines)

    matches = []
    for i in range(len(content_lines) - n + 1):
        check_lines = [line.strip() for line in content_lines[i:i + n]]
        if "\n".join(check_lines) == condensed:
            start_pos, end_pos = _compute_line_range(
                content_lines, i, i + n, len(content)
            )
            matches.append((start_pos, end_pos))

    return matches


def _match_ascii_fallback(content: str, pattern: str) -> List[Tuple[int, int]]:
    """Normalize Unicode smart-quotes/dashes, then try exact and line-trimmed match."""
    norm_pattern = _fold_unicode(pattern)
    norm_content = _fold_unicode(content)
    if norm_content == content and norm_pattern == pattern:
        return []

    hits = _match_literal(norm_content, norm_pattern)
    if not hits:
        hits = _match_stripped_lines(norm_content, norm_pattern)
    if not hits:
        return []

    origin_offsets = _compute_origin_to_norm(content)
    return _resolve_norm_to_origin(origin_offsets, hits)


def _match_anchored_block(content: str, pattern: str) -> List[Tuple[int, int]]:
    """Anchor on first and last lines, verify middle via similarity threshold."""
    norm_pattern = _fold_unicode(pattern)
    norm_content = _fold_unicode(content)

    pattern_lines = norm_pattern.split("\n")
    if len(pattern_lines) < 2:
        return []

    anchor_first = pattern_lines[0].strip()
    anchor_last = pattern_lines[-1].strip()

    norm_lines = norm_content.split("\n")
    orig_lines = content.split("\n")
    n = len(pattern_lines)

    candidates = []
    for i in range(len(norm_lines) - n + 1):
        if (norm_lines[i].strip() == anchor_first and
                norm_lines[i + n - 1].strip() == anchor_last):
            candidates.append(i)

    matches = []
    threshold = 0.50 if len(candidates) == 1 else 0.70

    for i in candidates:
        if n <= 2:
            similarity = 1.0
        else:
            content_middle = "\n".join(norm_lines[i + 1:i + n - 1])
            pattern_middle = "\n".join(pattern_lines[1:-1])
            similarity = SequenceMatcher(None, content_middle, pattern_middle).ratio()

        if similarity >= threshold:
            start_pos, end_pos = _compute_line_range(
                orig_lines, i, i + n, len(content)
            )
            matches.append((start_pos, end_pos))

    return matches


def _match_fuzzy_context(content: str, pattern: str) -> List[Tuple[int, int]]:
    """Sliding window: at least 50% of lines must achieve 80% similarity."""
    pattern_lines = pattern.split("\n")
    content_lines = content.split("\n")
    if not pattern_lines:
        return []

    matches = []
    n = len(pattern_lines)

    for i in range(len(content_lines) - n + 1):
        block = content_lines[i:i + n]
        good = 0
        for p_line, c_line in zip(pattern_lines, block):
            sim = SequenceMatcher(None, p_line.strip(), c_line.strip()).ratio()
            if sim >= 0.80:
                good += 1

        if good >= n * 0.5:
            start_pos, end_pos = _compute_line_range(
                content_lines, i, i + n, len(content)
            )
            matches.append((start_pos, end_pos))

    return matches



# Position mapping helpers
def _compute_origin_to_norm(original: str) -> List[int]:
    result: List[int] = []
    norm_pos = 0
    for char in original:
        result.append(norm_pos)
        repl = UNICODE_MAP.get(char)
        norm_pos += len(repl) if repl is not None else 1
    result.append(norm_pos)
    return result


def _resolve_norm_to_origin(
        origin_offsets: List[int],
        norm_matches: List[Tuple[int, int]],
) -> List[Tuple[int, int]]:
    norm_to_orig: Dict[int, int] = {}
    for orig_pos, norm_pos in enumerate(origin_offsets[:-1]):
        if norm_pos not in norm_to_orig:
            norm_to_orig[norm_pos] = orig_pos

    results: List[Tuple[int, int]] = []
    orig_len = len(origin_offsets) - 1

    for norm_start, norm_end in norm_matches:
        if norm_start not in norm_to_orig:
            continue
        orig_start = norm_to_orig[norm_start]
        orig_end = orig_start
        while orig_end < orig_len and origin_offsets[orig_end] < norm_end:
            orig_end += 1
        results.append((orig_start, orig_end))

    return results


def _compute_line_range(
        lines: List[str], start_line: int, end_line: int, total_len: int
) -> Tuple[int, int]:
    start_pos = sum(len(ln) + 1 for ln in lines[:start_line])
    end_pos = sum(len(ln) + 1 for ln in lines[:end_line]) - 1
    if end_pos >= total_len:
        end_pos = total_len
    return start_pos, end_pos


def _search_normalized_lines(
        content: str,
        content_lines: List[str],
        content_norm: List[str],
        pattern: str,
        pattern_norm: str,
) -> List[Tuple[int, int]]:
    pattern_lines = pattern_norm.split("\n")
    n = len(pattern_lines)
    matches = []

    for i in range(len(content_norm) - n + 1):
        block = "\n".join(content_norm[i:i + n])
        if block == pattern_norm:
            start_pos, end_pos = _compute_line_range(
                content_lines, i, i + n, len(content)
            )
            matches.append((start_pos, end_pos))

    return matches


def _resolve_whitespace_positions(
        original: str,
        collapsed: str,
        hits: List[Tuple[int, int]],
) -> List[Tuple[int, int]]:
    if not hits:
        return []

    origin_to_collapsed: List[int] = []
    orig_idx = 0
    col_idx = 0

    while orig_idx < len(original) and col_idx < len(collapsed):
        if original[orig_idx] == collapsed[col_idx]:
            origin_to_collapsed.append(col_idx)
            orig_idx += 1
            col_idx += 1
        elif original[orig_idx] in " \t" and collapsed[col_idx] == " ":
            origin_to_collapsed.append(col_idx)
            orig_idx += 1
            if orig_idx < len(original) and original[orig_idx] not in " \t":
                col_idx += 1
        elif original[orig_idx] in " \t":
            origin_to_collapsed.append(col_idx)
            orig_idx += 1
        else:
            origin_to_collapsed.append(col_idx)
            orig_idx += 1

    while orig_idx < len(original):
        origin_to_collapsed.append(len(collapsed))
        orig_idx += 1

    collapsed_to_orig_start: Dict[int, int] = {}
    collapsed_to_orig_end: Dict[int, int] = {}
    for orig_pos, col_pos in enumerate(origin_to_collapsed):
        if col_pos not in collapsed_to_orig_start:
            collapsed_to_orig_start[col_pos] = orig_pos
        collapsed_to_orig_end[col_pos] = orig_pos

    results: List[Tuple[int, int]] = []
    for col_start, col_end in hits:
        if col_start in collapsed_to_orig_start:
            orig_start = collapsed_to_orig_start[col_start]
        else:
            orig_start = min(
                i for i, n in enumerate(origin_to_collapsed) if n >= col_start
            )

        if col_end - 1 in collapsed_to_orig_end:
            orig_end = collapsed_to_orig_end[col_end - 1] + 1
        else:
            orig_end = orig_start + (col_end - col_start)

        while orig_end < len(original) and original[orig_end] in " \t":
            orig_end += 1

        results.append((orig_start, min(orig_end, len(original))))

    return results
