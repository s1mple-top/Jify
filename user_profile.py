# -*- coding: utf-8 -*-
"""
用户画像模块

从对话中自动检测用户偏好，结构化存储，注入 system prompt。

检测维度：
  命名风格、代码风格、简洁度偏好、技术栈偏好、交互节奏

存储：~/.jify/self_evolution/user_profile.json
"""

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from datetime import datetime



# 数据模型
@dataclass
class UserProfile:
    """用户画像数据结构"""
    naming_style: str = ""              # snake_case / camelCase / PascalCase
    code_style: str = ""                # 类型注解偏好、注释风格等
    verbosity: str = ""                 # concise / detailed / balanced
    tech_stack: List[str] = field(default_factory=list)   # 偏好技术栈
    interaction: str = ""               # batch / step_by_step / auto
    raw_preferences: List[str] = field(default_factory=list)  # 自由格式偏好列表

    def to_dict(self) -> dict:
        return {
            "naming_style": self.naming_style,
            "code_style": self.code_style,
            "verbosity": self.verbosity,
            "tech_stack": self.tech_stack,
            "interaction": self.interaction,
            "raw_preferences": self.raw_preferences,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "UserProfile":
        return cls(
            naming_style=d.get("naming_style", ""),
            code_style=d.get("code_style", ""),
            verbosity=d.get("verbosity", ""),
            tech_stack=d.get("tech_stack", []),
            interaction=d.get("interaction", ""),
            raw_preferences=d.get("raw_preferences", []),
        )



# 画像提取器
class UserProfileExtractor:
    """从对话中提取用户偏好，管理持久化存储"""

    STORAGE_DIR = os.path.join(os.path.expanduser("~"), ".jify", "self_evolution")
    STORAGE_FILENAME = "user_profile.json"

    def __init__(self, summarizer=None, user_id: str = "cli_user"):
        """
        Args:
            summarizer: Callable(prompt: str) -> str，LLM 调用接口
            user_id: 用户唯一标识（保留参数兼容性，不再影响文件名）
        """
        self.summarizer = summarizer
        self._storage_path = os.path.join(self.STORAGE_DIR, self.STORAGE_FILENAME)
        os.makedirs(self.STORAGE_DIR, exist_ok=True)
        self.profile = self._load()


    # 持久化
    def _ensure_dir(self):
        os.makedirs(os.path.dirname(self._storage_path), exist_ok=True)

    def _load(self) -> UserProfile:
        if not os.path.exists(self._storage_path):
            return UserProfile()
        try:
            with open(self._storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return UserProfile.from_dict(data)
        except (json.JSONDecodeError, IOError):
            return UserProfile()

    def save(self):
        self._ensure_dir()
        with open(self._storage_path, "w", encoding="utf-8") as f:
            json.dump(self.profile.to_dict(), f, ensure_ascii=False, indent=2)


    # 偏好提取
    EXTRACTION_PROMPT = """你是一个用户画像分析器。分析以下多轮对话历史，提取用户隐式偏好。

最近对话历史:
{conversation}

已有偏好（不要重复）:
{existing}

请用 JSON 格式输出新增的偏好（只输出 JSON，不要任何其他文字）:
{{
  "naming_style": "snake_case / camelCase / PascalCase / 空字符串(未检测到)",
  "code_style": "偏好描述 / 空字符串(未检测到)",
  "verbosity": "concise / detailed / balanced / 空字符串(未检测到)",
  "tech_stack": ["偏好技术1", "偏好技术2"],
  "interaction": "batch / step_by_step / auto / 空字符串(未检测到)",
  "raw_preferences": ["自由格式偏好1", "自由格式偏好2"]
  }}
  
规则:
- 只输出新增的、变化的偏好，未检测到的字段留空
- 技术栈只列出用户明确表达过偏好的
- raw_preferences 列出无法归类的显式偏好（如"我喜欢简短回复"）
- 如果对话中没有任何新的偏好信息，输出空 JSON: {{}}"""

    def extract(self, conversation: str) -> Optional[dict]:
        """从多轮对话上下文中提取用户偏好

        Args:
            conversation: 调用方预构建的多轮对话文本（已格式化）

        Returns:
            提取到的新偏好 dict，无新偏好返回 None
        """
        if not self.summarizer:
            return None

        existing = json.dumps(self.profile.to_dict(), ensure_ascii=False, indent=2)

        prompt = self.EXTRACTION_PROMPT.format(
            conversation=conversation,
            existing=existing,
        )

        try:
            result = self.summarizer(prompt)
            # 提取 JSON（LLM 可能包裹在 ```json ... ``` 中）
            result = result.strip()
            if result.startswith("```"):
                lines = result.split("\n")
                result = "\n".join(lines[1:-1]) if len(lines) > 2 else result
            parsed = json.loads(result)

            if not parsed or all(not v for v in parsed.values() if v != []):
                return None  # 空偏好，无新增

            return parsed
        except (json.JSONDecodeError, Exception):
            return None

    def apply(self, new_prefs: dict) -> bool:
        """将提取到的偏好合并到画像中

        Returns:
            True 表示画像有变化
        """
        changed = False

        for key in ["naming_style", "code_style", "verbosity", "interaction"]:
            if new_prefs.get(key) and new_prefs[key] != getattr(self.profile, key):
                setattr(self.profile, key, new_prefs[key])
                changed = True

        if new_prefs.get("tech_stack"):
            existing = set(self.profile.tech_stack)
            for tech in new_prefs["tech_stack"]:
                if tech and tech not in existing:
                    self.profile.tech_stack.append(tech)
                    changed = True

        if new_prefs.get("raw_preferences"):
            for pref in new_prefs["raw_preferences"]:
                if pref and pref not in self.profile.raw_preferences:
                    self.profile.raw_preferences.append(pref)
                    changed = True

        if changed:
            self.save() # 落盘
        return changed


    # System Prompt 注入
    def build_prompt_section(self) -> str:
        """构建注入 system prompt 的用户偏好段落"""
        p = self.profile
        parts = []

        if p.naming_style:
            parts.append(f"- 命名规范：统一使用 {p.naming_style}")
        if p.code_style:
            parts.append(f"- 代码风格：{p.code_style}")
        if p.verbosity:
            verb_map = {"concise": "偏好简短回复，不要过度解释", 
                       "detailed": "偏好详细解释，可以展开说明",
                       "balanced": "偏好适中详细度的回复"}
            parts.append(f"- 回复风格：{verb_map.get(p.verbosity, p.verbosity)}")
        if p.tech_stack:
            parts.append(f"- 技术栈：优先使用 {', '.join(p.tech_stack)}")
        if p.interaction:
            inter_map = {"batch": "偏好批量操作，一次确认多个",
                        "step_by_step": "偏好逐步确认，每次一个操作",
                        "auto": "偏好自动执行，不需确认"}
            parts.append(f"- 交互节奏：{inter_map.get(p.interaction, p.interaction)}")
        if p.raw_preferences:
            for pref in p.raw_preferences:
                parts.append(f"- {pref}")

        if not parts:
            return ""

        return "## 用户偏好\n" + "\n".join(parts)
