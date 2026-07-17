# -*- coding: utf-8 -*-
"""builtin tool — 技能加载"""


import os
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
)
def load_skill(skill_name: str) -> str:
    """
    加载并理解特定 skill 的内容。

    根据 skill_name 读取对应 skill 目录下的 SKILL.md 文件，
    首先在本地 skills 目录查找，如果找不到则在 ~/.jify/skills，
    最后在 OpenClaw 的 skill 目录查找。

    Args:
        skill_name: skill 的名称

    Returns:
        str: [SKILL.md 文件内容, skill 目录的绝对路径]
    """
    event_bus.put(UIEvent("TEXT", "* preparing load_skill ( " + skill_name + " )"))

    # tools/builtin/load_skill.py → tools/builtin → tools → project root
    project_dir = Path(__file__).parent.parent.parent
    skills_dir = project_dir / "skills"

    # 构建本地 skill 目录路径
    local_skill_md_path = skills_dir / skill_name / "SKILL.md"

    # 构建 OpenClaw skill 目录路径
    openclaw_skills_dir = Path(os.path.expanduser("~/.openclaw/workspace/skills"))
    openclaw_skill_md_path = openclaw_skills_dir / skill_name / "SKILL.md"

    # 构建 ~/.jify/skills 目录路径
    jify_skills_dir = Path(os.path.expanduser("~/.jify/skills"))
    jify_skill_md_path = jify_skills_dir / skill_name / "SKILL.md"

    # 确定使用哪个目录（优先级: local > ~/.jify/skills > OpenClaw）
    skill_dir = None
    skill_md_path = None

    if local_skill_md_path.exists():
        skill_dir = skills_dir / skill_name
        skill_md_path = local_skill_md_path
    elif jify_skill_md_path.exists():
        skill_dir = jify_skills_dir / skill_name
        skill_md_path = jify_skill_md_path
    elif openclaw_skill_md_path.exists():
        skill_dir = openclaw_skills_dir / skill_name
        skill_md_path = openclaw_skill_md_path
    else:
        return str(
            f"Skill '{skill_name}' not found. Available skills are in the local skills directory, ~/.jify/skills, or OpenClaw skills directory.")

    try:
        with open(skill_md_path, 'r', encoding='utf-8') as f:
            content = f.read()

        return str([str(skill_dir.resolve()) , content])
    except Exception as e:
        return str(f"Error reading skill '{skill_name}': {str(e)}")


if __name__ == "__main__":
    print(load_skill("jisu-baidu"))
