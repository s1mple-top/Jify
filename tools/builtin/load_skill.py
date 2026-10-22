# -*- coding: utf-8 -*-
"""builtin tool — 技能加载"""


import json
import os
from datetime import datetime
from pathlib import Path

from tools.registry import register_tool
from tools.skill_paths import SKILL_MD, canonical_skill_name, resolve_skill
from tools.builtin.skill_experience import render_for_prompt
from event_bus import UIEvent, event_bus

# 使用量追踪文件
USAGE_FILE = Path(os.path.expanduser("~/.jify/skills/usage.json"))


def _record_usage(skill_name: str) -> None:
    """记录 skill 加载次数和时间戳到 usage.json"""
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat()

    data = {}
    if USAGE_FILE.exists():
        try:
            data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, IOError):
            pass

    if skill_name not in data:
        data[skill_name] = {
            "first_loaded": now,
            "last_loaded": now,
            "load_count": 1,
        }
    else:
        entry = data[skill_name]
        if not entry.get("first_loaded"):
            entry["first_loaded"] = now
        entry["last_loaded"] = now
        entry["load_count"] = entry.get("load_count", 0) + 1

    USAGE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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

    skill_dir = resolve_skill(skill_name)
    if skill_dir is None:
        # JSON 对象而非纯文本：dispatch 透传对象，error 落在顶层，模型能识别失败
        return json.dumps({
            "error": f"Skill '{skill_name}' not found. Available skills are in the local skills directory, ~/.jify/skills, or OpenClaw skills directory."
        }, ensure_ascii=False)
    skill_md_path = skill_dir / SKILL_MD
    # 统计与经验库统一使用规范名，避免 Weather / weather 被记成两条
    canonical = canonical_skill_name(skill_dir)

    try:
        with open(skill_md_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # 附加实战经验库（自增长沉淀，随方法论一起加载）
        experience_text = render_for_prompt(canonical)
        if experience_text:
            content = content + "\n" + experience_text

        # 记录使用量
        _record_usage(canonical)

        return str([str(skill_dir.resolve()) , content])
    except Exception as e:
        return json.dumps({"error": f"Error reading skill '{skill_name}': {str(e)}"}, ensure_ascii=False)


if __name__ == "__main__":
    print(load_skill("jisu-baidu"))
