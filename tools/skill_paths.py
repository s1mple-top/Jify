# -*- coding: utf-8 -*-
"""Skill 路径解析 —— 发现侧与加载侧共用同一套目录与命名规则。

此前 system_prompt._discover_skills 与 load_skill 各自硬编码目录列表，
导致「提示里列出但加载不到」。所有 skill 目录/名称解析统一走这里。

优先级：项目 skills > ~/.jify/skills > ~/.openclaw/workspace/skills > ~/.openclaw/skills
"""

import json
import re
from pathlib import Path
from typing import List, NamedTuple, Optional

import yaml

SKILL_MD = "SKILL.md"
META_JSON = "_meta.json"
SKILL_JSON = "skill.json"

_NAME_SAFE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]*$")


class SkillEntry(NamedTuple):
    name: str
    description: str
    dir: Path


def skill_roots() -> List[Path]:
    """按优先级返回 skill 根目录（不含具体 skill 名）。"""
    project_dir = Path(__file__).parent.parent
    return [
        project_dir / "skills",
        Path.home() / ".jify" / "skills",
        Path.home() / ".openclaw" / "workspace" / "skills",
        Path.home() / ".openclaw" / "skills",
    ]


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _frontmatter(path: Path) -> dict:
    """读取 SKILL.md 的 YAML frontmatter；失败返回空字典。"""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not content.startswith("---"):
        return {}
    m = re.search(r"\n---\s*\n", content[3:])
    if not m:
        return {}
    try:
        parsed = yaml.safe_load(content[3:m.start() + 3])
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _entry_name(skill_dir: Path, frontmatter: dict) -> str:
    """skill 的唯一名字：_meta.slug > skill.json.name > frontmatter name > 目录名。"""
    name = _read_json(skill_dir / META_JSON).get("slug") or ""
    if not name:
        name = _read_json(skill_dir / SKILL_JSON).get("name") or ""
    if not name:
        name = frontmatter.get("name") or ""
    return str(name) or skill_dir.name


def _entry_description(skill_dir: Path, frontmatter: dict) -> str:
    """描述：_meta > skill.json > frontmatter。"""
    for src in (_read_json(skill_dir / META_JSON), _read_json(skill_dir / SKILL_JSON), frontmatter):
        desc = src.get("description")
        if desc:
            return str(desc)
    return ""


def discover_skills() -> List[SkillEntry]:
    """遍历所有根目录，返回可加载（SKILL.md 存在）的 skill 列表。

    同名 skill 只保留优先级最高的那个。
    """
    seen = set()
    result: List[SkillEntry] = []
    for root in skill_roots():
        if not root.is_dir():
            continue
        for skill_dir in sorted(root.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / SKILL_MD
            if not skill_md.is_file():
                # 无 SKILL.md 的目录无法加载，不对外暴露
                continue
            frontmatter = _frontmatter(skill_md)
            name = _entry_name(skill_dir, frontmatter)
            if name in seen:
                continue
            seen.add(name)
            result.append(SkillEntry(name, _entry_description(skill_dir, frontmatter), skill_dir))
    return result


def canonical_skill_name(skill_dir: Path) -> str:
    """返回 skill 的规范名（与 discover_skills() 一致），用于统计与经验库索引。"""
    return _entry_name(skill_dir, _frontmatter(skill_dir / SKILL_MD))


def resolve_skill(skill_name: str) -> Optional[Path]:
    """按名字解析出含 SKILL.md 的 skill 目录，找不到返回 None。

    先按目录名精确命中，再回退到 slug / frontmatter name 匹配，
    使「提示里给出的名字」在任何文件系统（区分大小写与否）下都能加载。
    """
    if not skill_name or not _NAME_SAFE_RE.match(skill_name):
        return None

    for root in skill_roots():
        if not root.is_dir():
            continue
        for skill_dir in root.iterdir():
            # 逐一比较目录名而非直接拼接：避免大小写不敏感文件系统返回不存在的路径
            if skill_dir.name == skill_name and (skill_dir / SKILL_MD).is_file():
                return skill_dir

    for entry in discover_skills():
        if entry.name == skill_name:
            return entry.dir
    return None
