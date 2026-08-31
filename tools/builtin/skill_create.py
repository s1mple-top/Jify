# -*- coding: utf-8 -*-
"""builtin tool — 技能创建/编辑/删除

让 Agent 将成功经验固化为可复用技能，实现「用一次，记住一辈子」的自进化闭环。
"""

import json
import os
import re
import shutil
from pathlib import Path
from typing import Optional

import yaml

from tools.registry import register_tool
from event_bus import UIEvent, event_bus

# 常量
SKILLS_DIR = Path(os.path.expanduser("~/.jify/skills"))
VALID_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]*$")
MAX_NAME_LENGTH = 64
MAX_SKILL_CONTENT_CHARS = 50_000
MAX_DESCRIPTION_LENGTH = 500


# 校验函数
def _validate_name(name: str) -> Optional[str]:
    """校验技能名，返回错误信息或 None"""
    if not name:
        return "Skill name is required."
    if len(name) > MAX_NAME_LENGTH:
        return f"Skill name exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(name):
        return (
            f"Invalid skill name '{name}'. "
            f"Use lowercase letters, numbers, hyphens, dots, underscores. "
            f"Must start with a letter or digit."
        )
    return None


def _validate_frontmatter(content: str) -> Optional[str]:
    """校验 SKILL.md 的 YAML frontmatter，返回错误信息或 None"""
    if not content.strip():
        return "Content cannot be empty."
    if not content.startswith("---"):
        return "SKILL.md must start with YAML frontmatter (---). See existing skills for format."

    # 查找结束的 ---
    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return "SKILL.md frontmatter is not closed. Ensure you have a closing '---' line."

    yaml_str = content[3 : end_match.start() + 3]
    try:
        parsed = yaml.safe_load(yaml_str)
    except yaml.YAMLError as e:
        return f"YAML frontmatter parse error: {e}"

    if not isinstance(parsed, dict):
        return "Frontmatter must be a YAML mapping (key: value pairs)."

    if "name" not in parsed:
        return "Frontmatter must include 'name' field."
    if "description" not in parsed:
        return "Frontmatter must include 'description' field."
    if len(str(parsed["description"])) > MAX_DESCRIPTION_LENGTH:
        return f"Description exceeds {MAX_DESCRIPTION_LENGTH} characters."

    body = content[end_match.end() + 3 :].strip()
    if not body:
        return "SKILL.md must have body content after the frontmatter."

    return None


def _extract_frontmatter(content: str) -> dict:
    """从 SKILL.md 提取 YAML frontmatter 为字典"""
    end_match = re.search(r"\n---\s*\n", content[3:])
    yaml_str = content[3 : end_match.start() + 3]
    try:
        return yaml.safe_load(yaml_str) or {}
    except yaml.YAMLError:
        return {}


def _validate_content_size(content: str) -> Optional[str]:
    """校验内容不超过上限"""
    if len(content) > MAX_SKILL_CONTENT_CHARS:
        return (
            f"Content is {len(content):,} characters "
            f"(limit: {MAX_SKILL_CONTENT_CHARS:,}). "
            f"Consider splitting into smaller files (references/, templates/)."
        )
    return None


def _write_meta_json(name: str, description: str) -> None:
    """写出 skill.json（_get_skills() 用它发现技能）"""
    skill_json = SKILLS_DIR / name / "skill.json"
    skill_json.write_text(
        json.dumps(
            {"name": name, "description": description},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# 工具入口
@register_tool(
    name="skill_create",
    description=(
        "Create, edit, or delete a skill. Skills are reusable behaviors / instructions "
        "stored as SKILL.md files under ~/.jify/skills/. "
        "Use this tool to capture successful techniques so you can reuse them later. "
        "After creating or editing, call load_skill('skill-name') to activate it immediately. "
        "New skills appear in the system prompt on the next restart."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "edit", "delete"],
                "description": "Action to perform: 'create' a new skill, 'edit' an existing one, or 'delete' one.",
            },
            "name": {
                "type": "string",
                "description": (
                    "Skill name (lowercase slug, e.g. 'my-skill'). "
                    "Must start with letter/digit, only [a-zA-Z0-9_.-] allowed."
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    "Full SKILL.md content with YAML frontmatter. Required for 'create' and 'edit', "
                    "ignored for 'delete'. The frontmatter must include 'name' and 'description' fields."
                ),
            },
        },
        "required": ["action", "name"],
    },
    parallel_safe=False,
    requires_approval=True,
)
def skill_create(action: str, name: str, content: str = "") -> str:
    """创建、编辑或删除技能"""

    # 校验名称
    err = _validate_name(name)
    if err:
        return json.dumps({"success": False, "error": err})

    skill_dir = SKILLS_DIR / name
    skill_md = skill_dir / "SKILL.md"

    # create
    if action == "create":
        if skill_dir.exists():
            return json.dumps(
                {
                    "success": False,
                    "error": f"Skill '{name}' already exists. Use action='edit' to update, or choose a different name.",
                }
            )

        if not content:
            return json.dumps({"success": False, "error": "Parameter 'content' is required for create."})

        err = _validate_frontmatter(content)
        if err:
            return json.dumps({"success": False, "error": err})

        err = _validate_content_size(content)
        if err:
            return json.dumps({"success": False, "error": err})

        # 提取 frontmatter 用于 skill.json
        fm = _extract_frontmatter(content)

        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md.write_text(content, encoding="utf-8")
        _write_meta_json(name, fm.get("description", ""))

        event_bus.put(UIEvent("TEXT", f"* skill_create: created '{name}'"))

        return json.dumps(
            {
                "success": True,
                "message": f"Skill '{name}' created successfully.",
                "path": str(skill_md),
                "hint": f"Use load_skill('{name}') to activate it in the current session. It will appear in the system prompt after restart.",
            }
        )

    # edit
    elif action == "edit":
        if not skill_dir.exists() or not skill_md.exists():
            return json.dumps(
                {
                    "success": False,
                    "error": f"Skill '{name}' does not exist. Use action='create' to make a new one.",
                }
            )

        if not content:
            return json.dumps({"success": False, "error": "Parameter 'content' is required for edit."})

        err = _validate_frontmatter(content)
        if err:
            return json.dumps({"success": False, "error": err})
        err = _validate_content_size(content)
        if err:
            return json.dumps({"success": False, "error": err})

        fm = _extract_frontmatter(content)

        skill_md.write_text(content, encoding="utf-8")
        _write_meta_json(name, fm.get("description", ""))

        event_bus.put(UIEvent("TEXT", f"* skill_create: edited '{name}'"))

        return json.dumps(
            {
                "success": True,
                "message": f"Skill '{name}' updated successfully.",
                "path": str(skill_md),
            }
        )

    # delete
    elif action == "delete":
        if not skill_dir.exists():
            return json.dumps({"success": False, "error": f"Skill '{name}' does not exist."})

        shutil.rmtree(skill_dir)

        event_bus.put(UIEvent("TEXT", f"* skill_create: deleted '{name}'"))

        return json.dumps(
            {
                "success": True,
                "message": f"Skill '{name}' deleted successfully.",
            }
        )

    return json.dumps({"success": False, "error": f"Unknown action: '{action}'. Use 'create', 'edit', or 'delete'."})
