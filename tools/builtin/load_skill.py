# -*- coding: utf-8 -*-
"""builtin tool — 技能加载"""


import os
import json
import re
from pathlib import Path

from tools.registry import register_tool
from event_bus import UIEvent, event_bus


@register_tool(
    name="load_skill",
    description="Load specific skills and understand their content",
    parameters={
        "type": "object",
        "properties": {
            "skill_name": {"type": "string", "description": "The name of skill"}
        },
        "required": ["skill_name"],
    },
    parallel_safe=False,
    # requires_approval=False,
)
def load_skill(skill_name: str) -> str:
    """
    加载并理解特定 skill 的内容。

    根据 skill_name 读取对应 skill 目录下的 SKILL.md 文件，
    理解该 skill 的使用方法和理念。

    首先在本地 skills 目录查找，如果找不到则在 OpenClaw 的 skill 目录查找。

    Args:
        skill_name: skill 的名称

    Returns:
        list: [SKILL.md 文件的完整内容, skill目录的绝对路径, 元数据字典]
    """
    event_bus.put(UIEvent("TEXT", "* preparing load_skill ( " + skill_name + " )"))

    # tools/builtin/load_skill.py → tools/builtin → tools → project root
    project_dir = Path(__file__).parent.parent.parent
    skills_dir = project_dir / "skills"

    # 构建本地 skill 目录路径
    local_skill_dir = skills_dir / skill_name
    local_skill_md_path = local_skill_dir / "SKILL.md"
    local_skill_json_path = local_skill_dir / "skill.json"
    local_meta_json_path = local_skill_dir / "_meta.json"

    # 构建 OpenClaw skill 目录路径
    openclaw_skills_dir = Path("/Users/s1mple/.openclaw/workspace/skills")
    openclaw_skill_dir = openclaw_skills_dir / skill_name
    openclaw_skill_md_path = openclaw_skill_dir / "SKILL.md"

    # 构建 ~/.jify/skills 目录路径
    jify_skills_dir = Path(os.path.expanduser("~/.jify/skills"))
    jify_skill_dir = jify_skills_dir / skill_name
    jify_skill_md_path = jify_skill_dir / "SKILL.md"
    jify_skill_json_path = jify_skill_dir / "skill.json"
    jify_meta_json_path = jify_skill_dir / "_meta.json"

    # 确定使用哪个目录（优先级: local > ~/.jify/skills > OpenClaw）
    skill_dir = None
    skill_md_path = None
    skill_json_path = None
    meta_json_path = None
    source = ""

    if local_skill_md_path.exists():
        skill_dir = local_skill_dir
        skill_md_path = local_skill_md_path
        skill_json_path = local_skill_json_path
        meta_json_path = local_meta_json_path
        source = "local"
    elif jify_skill_md_path.exists():
        skill_dir = jify_skill_dir
        skill_md_path = jify_skill_md_path
        skill_json_path = jify_skill_json_path
        meta_json_path = jify_meta_json_path
        source = "jify"
    elif openclaw_skill_md_path.exists():
        skill_dir = openclaw_skill_dir
        skill_md_path = openclaw_skill_md_path
        source = "openclaw"
    else:
        return str(
            f"Skill '{skill_name}' not found. Available skills are in the local skills directory, ~/.jify/skills, or OpenClaw skills directory.")

    try:
        with open(skill_md_path, 'r', encoding='utf-8') as f:
            content = f.read()

        skill_info = {
            "content": content,
            "path": str(skill_dir.resolve()),
            "metadata": {},
            "source": source
        }

        # 如果是本地或 jify 技能，尝试读取 skill.json 或 _meta.json
        if source in ("local", "jify"):
            # 尝试读取 skill.json（主要元数据源）
            if skill_json_path and skill_json_path.exists():
                try:
                    with open(skill_json_path, 'r', encoding='utf-8') as f:
                        skill_data = json.load(f)
                        skill_info["metadata"] = {
                            "name": skill_data.get("name", ""),
                            "description": skill_data.get("description", ""),
                            "version": skill_data.get("version", ""),
                            "author": skill_data.get("author", ""),
                            "tags": skill_data.get("tags", []),
                            "parallel_safe": skill_data.get("parallel_safe", False),
                            "timeout": skill_data.get("timeout", 5),
                            "source": "skill.json"
                        }
                except json.JSONDecodeError:
                    pass

            # 如果没有找到 skill.json 或解析失败，尝试读取 _meta.json
            if not skill_info["metadata"] and meta_json_path and meta_json_path.exists():
                try:
                    with open(meta_json_path, 'r', encoding='utf-8') as f:
                        meta_data = json.load(f)
                        slug = meta_data.get("slug", "")
                        description = meta_data.get("description", "")
                        skill_info["metadata"] = {
                            "name": slug,
                            "description": description,
                            "version": meta_data.get("version", ""),
                            "source": "_meta.json"
                        }
                except json.JSONDecodeError:
                    pass

        # 如果是 OpenClaw 技能，从 SKILL.md 的 YAML frontmatter 解析元数据
        if source == "openclaw":
            try:
                with open(skill_md_path, 'r', encoding='utf-8') as f:
                    lines = []
                    for i, line in enumerate(f):
                        if i >= 20:
                            break
                        lines.append(line)

                    content_preview = ''.join(lines)
                    name = ""
                    description = ""

                    name_match = re.search(r'name:\s*(\S+)', content_preview)
                    if name_match:
                        name = name_match.group(1)

                    description_match = re.search(r'description:\s*(.+)', content_preview)
                    if description_match:
                        description = description_match.group(1).strip()

                    skill_info["metadata"] = {
                        "name": name,
                        "description": description,
                        "source": "SKILL.md"
                    }
            except Exception:
                pass

        return str([skill_info["content"], skill_info["path"], skill_info["metadata"]])
    except Exception as e:
        return str(f"Error reading skill '{skill_name}': {str(e)}")
