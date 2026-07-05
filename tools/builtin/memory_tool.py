# -*- coding: utf-8 -*-
"""Memory retrieval tool — RAG-style 长期记忆搜索。

按日期分文件存储: Memory/{user_id}/YYYY_MM_DD.md
检索策略:
  - 默认搜索今天 + 昨天的记忆
  - date 参数指定具体某天
  - from_date / to_date 指定日期范围
  - 向后兼容旧格式 Memory/{user_id}.md

第一版本，暂时不启用。
"""

import json
import math
import os
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional

from jify_tool import registry, register_tool

_MEMORY_ROOT = Path(__file__).parent.parent.parent / "Memory"

_SENTENCE_PATTERN = re.compile(
    r'[^。！？\n.!?\n]+(?:[。！？.!?]|$)'
)


def _detect_user_id() -> str:
    """自动检测当前用户 ID（优先从 P2P 实例名获取）。"""
    try:
        from agent_p2p import get_p2p
        p2p = get_p2p()
        if p2p and p2p.my_name:
            return p2p.my_name
    except Exception:
        pass
    return "cli_user"


def _tokenize(text: str) -> List[str]:
    """中英混合分词。"""
    tokens = []
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff':
            tokens.append(ch)
    for word in re.findall(r'[a-zA-Z0-9_]{2,}', text.lower()):
        tokens.append(word)
    return tokens


def _split_sentences(text: str) -> List[str]:
    """按句子拆分，过滤过短片段。"""
    parts = _SENTENCE_PATTERN.findall(text)
    return [p.strip() for p in parts if len(p.strip()) > 5]


def _resolve_date_files(user_id: str, date: str = "",
                        from_date: str = "", to_date: str = "") -> List[Path]:
    """解析日期参数，返回要搜索的记忆文件列表。

    - 无日期: 今天 + 昨天
    - date: 指定某一天
    - from_date / to_date: 日期范围
    """
    user_dir = _MEMORY_ROOT / user_id
    today = datetime.now().strftime("%Y_%m_%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y_%m_%d")

    if date:
        candidates = {date}
    elif from_date or to_date:
        candidates = set()
        if user_dir.is_dir():
            for f in user_dir.glob("*.md"):
                stem = f.stem
                if re.match(r'\d{4}_\d{2}_\d{2}', stem):
                    if from_date and stem < from_date:
                        continue
                    if to_date and stem > to_date:
                        continue
                    candidates.add(stem)
    else:
        candidates = {today, yesterday}

    files = []
    if user_dir.is_dir():
        for f in sorted(user_dir.glob("*.md"), reverse=True):
            if f.stem in candidates:
                files.append(f)

    return files


def _parse_md_entries(path: Path, date_tag: str = "") -> List[Dict]:
    """解析 Markdown 记忆文件，按句子拆分为条目。"""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []

    entries = []
    current_section = ""
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("# "):
            continue
        if stripped.startswith("## "):
            current_section = stripped[3:].strip()
            continue
        if stripped.startswith("#"):
            continue

        content = stripped
        timestamp = ""
        match = re.match(r'^- (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) \| (.+)$', stripped)
        if match:
            timestamp = match.group(1)
            content = match.group(2)
        elif stripped.startswith("- "):
            content = stripped[2:]

        for sent in _split_sentences(content):
            entries.append({
                "content": sent,
                "section": current_section,
                "source": date_tag or path.stem,
                "timestamp": timestamp,
            })

    return entries


def _parse_profile(path: Path) -> List[Dict]:
    """解析 JSON 用户画像。"""
    if not path.exists():
        return []
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    entries = []
    field_map = {
        "naming_style": "命名风格",
        "code_style": "代码风格",
        "verbosity": "输出风格",
        "tech_stack": "技术栈",
        "interaction": "交互偏好",
        "raw_preferences": "用户偏好",
    }
    for key, label in field_map.items():
        val = profile.get(key, "")
        if val:
            content = f"{label}: {val}" if isinstance(val, str) else f"{label}: {json.dumps(val, ensure_ascii=False)}"
            for sent in _split_sentences(content):
                entries.append({
                    "content": sent,
                    "section": "用户画像",
                    "source": "profile",
                    "timestamp": "",
                })
    return entries


def _parse_experiences_json() -> List[Dict]:
    """解析 experiences.json，返回结构化条目列表。"""
    exp_path = Path(os.path.expanduser("~"), ".jify", "self_evolution", "experiences.json")
    if not exp_path.exists():
        return []
    try:
        data = json.loads(exp_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    entries = []
    for key, label in [("best_practices", "最佳实践"), ("pitfalls", "踩坑记录")]:
        for item in data.get(key, []):
            if isinstance(item, str):
                entries.append({
                    "content": item,
                    "section": label,
                    "source": "experience",
                    "timestamp": "",
                })
    return entries


def _load_memories(user_id: str, date: str = "",
                   from_date: str = "", to_date: str = "") -> List[Dict]:
    """加载指定用户和日期范围的记忆条目。"""
    entries = []

    # 1. 日期分文件
    date_files = _resolve_date_files(user_id, date, from_date, to_date)
    for f in date_files:
        entries.extend(_parse_md_entries(f, date_tag=f.stem))

    # 2. 经验文件（experiences.json）
    entries.extend(_parse_experiences_json())

    # 3. 用户画像
    entries.extend(_parse_profile(_MEMORY_ROOT / "profiles" / f"{user_id}.json"))

    # 4. 向后兼容: 旧格式兜底
    legacy_path = _MEMORY_ROOT / f"{user_id}.md"
    if legacy_path.exists() and not entries:
        entries.extend(_parse_md_entries(legacy_path, date_tag="legacy"))

    return entries


def _tfidf_score(query: str, entries: List[Dict]) -> List[Dict]:
    """TF-IDF 相似度排序。"""
    if not entries:
        return []

    query_tokens = _tokenize(query)
    if not query_tokens:
        return entries[:5]

    all_texts = [e["content"] for e in entries]
    doc_tokens_list = [_tokenize(t) for t in all_texts]
    N = len(all_texts)

    query_tf = Counter(query_tokens)
    results = []

    for i, entry in enumerate(entries):
        doc_tokens = doc_tokens_list[i]
        if not doc_tokens:
            continue
        doc_tf = Counter(doc_tokens)
        score = 0.0
        for token in set(query_tokens):
            if token in doc_tf:
                df = sum(1 for dt in doc_tokens_list if token in dt) or 1
                q_tf = query_tf[token] / len(query_tokens)
                d_tf = doc_tf[token] / len(doc_tokens)
                idf = math.log((N + 1) / (df + 0.5))
                score += q_tf * d_tf * idf
        if score > 0:
            results.append((score, entry))

    results.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in results]


@register_tool(
    name="memory_search",
    description=(
        "搜索长期记忆库。默认搜索今天和昨天的记忆；"
        "当用户提到具体日期时（如「前天」「上周三」「6月5号」），"
        "通过 date 参数指定具体日期。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "自然语言搜索查询",
            },
            "date": {
                "type": "string",
                "description": "指定搜索日期，格式 YYYY_MM_DD，如 2026_06_05。用户提到具体日期时传入",
            },
            "from_date": {
                "type": "string",
                "description": "日期范围起始，格式 YYYY_MM_DD",
            },
            "to_date": {
                "type": "string",
                "description": "日期范围结束，格式 YYYY_MM_DD",
            },
            "user_id": {
                "type": "string",
                "description": "用户 ID，留空自动检测",
            },
            "top_k": {
                "type": "integer",
                "description": "返回条目数，默认 5",
            },
        },
        "required": ["query"],
    },
    # requires_approval=False,
)
def memory_search(query: str, date: str = "", from_date: str = "",
                  to_date: str = "", user_id: str = "", top_k: int = 5) -> str:
    uid = user_id or _detect_user_id()
    entries = _load_memories(uid, date, from_date, to_date)

    if not entries:
        return json.dumps({
            "results": [],
            "count": 0,
            "query": query,
            "message": f"未找到匹配的记忆",
        }, ensure_ascii=False)

    scored = _tfidf_score(query, entries)[:top_k]
    formatted = []
    for e in scored:
        formatted.append({
            "content": e["content"],
            "section": e.get("section", ""),
            "source": e.get("source", ""),
            "timestamp": e.get("timestamp", ""),
        })

    return json.dumps({
        "results": formatted,
        "count": len(formatted),
        "query": query,
    }, ensure_ascii=False)


@register_tool(
    name="memory_save",
    description=(
        "将一条重要信息保存到今天的长期记忆。"
        "适用于用户表达的个人偏好、项目约定、重要决策或经验教训。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "要保存的记忆内容",
            },
            "section": {
                "type": "string",
                "description": "记忆分类，如「关键事实」「项目约定」「偏好」「经验教训」",
            },
            "user_id": {
                "type": "string",
                "description": "用户 ID，留空自动检测",
            },
        },
        "required": ["content", "section"],
    },
    # requires_approval=False,
)
def memory_save(content: str, section: str, user_id: str = "") -> str:
    uid = user_id or _detect_user_id()
    user_dir = _MEMORY_ROOT / uid
    user_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y_%m_%d")
    mem_path = user_dir / f"{today}.md"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    try:
        if mem_path.exists():
            existing = mem_path.read_text(encoding="utf-8")
        else:
            existing = f"# 记忆 - {today}\n\n"

        section_header = f"## {section}"
        new_entry = f"- {ts} | {content}\n"

        if section_header in existing:
            parts = existing.split(section_header, 1)
            before = parts[0] + section_header + "\n"
            after = parts[1].lstrip("\n") if len(parts) > 1 else ""
            existing = before + new_entry + after
        else:
            existing = existing.rstrip() + f"\n\n{section_header}\n{new_entry}"

        mem_path.write_text(existing, encoding="utf-8")
        return json.dumps({"saved": True, "file": f"{uid}/{today}.md", "section": section}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"saved": False, "message": str(e)}, ensure_ascii=False)
