# -*- coding: utf-8 -*-
"""
经验自动提取模块

从多轮对话中提取可复用的经验教训，持久化到 experiences.json。

提取维度：
  最佳实践（best_practices）、踩坑记录（pitfalls）

存储：~/.jify/self_evolution/experiences.json
"""

import json
import os
from typing import List, Dict


class ExperienceExtractor:
    """从对话历史中提取可复用经验，管理持久化存储"""

    STORAGE_FILE = os.path.join(os.path.expanduser("~"), ".jify", "self_evolution", "experiences.json")

    # JSON 顶级 key
    KEY_BEST_PRACTICES = "best_practices"
    KEY_PITFALLS = "pitfalls"

    def __init__(self, summarizer=None):
        """
        Args:
            summarizer: Callable(prompt: str) -> str，LLM 调用接口
            user_id: 用户唯一标识（保留兼容，当前不影响文件名）
        """
        self.summarizer = summarizer
        self._storage_path = self.STORAGE_FILE
        os.makedirs(os.path.dirname(self._storage_path), exist_ok=True)


    # 持久化
    def _load_json(self) -> Dict[str, List[str]]:
        """读取经验 JSON，返回 {best_practices: [...], pitfalls: [...]}"""
        try:
            with open(self._storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        return {
            self.KEY_BEST_PRACTICES: data.get(self.KEY_BEST_PRACTICES, []),
            self.KEY_PITFALLS: data.get(self.KEY_PITFALLS, []),
        }

    def _save_json(self, data: Dict[str, List[str]]) -> None:
        """落盘"""
        with open(self._storage_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _category_to_key(category: str) -> str:
        """中文 category → JSON key"""
        if "踩坑" in category:
            return ExperienceExtractor.KEY_PITFALLS
        return ExperienceExtractor.KEY_BEST_PRACTICES


    # 经验提取
    EXTRACTION_PROMPT = """你是一个经验总结助手。你的任务是从多轮对话中提取 Jify（AI 编程助手）的行为纠正经验。

**经验的定义：Jify 哪里做得不对，以后该怎么改。不是泛知识、不是用户画像、不是技术方案选型。**

== 上下文结构说明 ==
你会收到最近几轮的完整对话，格式为：
  --- 第 N 轮 ⚠ 用户纠正/不满 ---
  [用户] ...
  [本轮使用的工具] ...
  [Jify 回复] ...

轮次标记了 "⚠ 用户纠正/不满" 的，说明该轮用户消息命中纠错关键词（如「不对」「错了」「不是这样的」），但请注意：
- ⚠ 标记是辅助信号，不代表该轮一定有价值——用户可能在纠正自己而非 Jify
- 没有 ⚠ 标记的轮次也可能包含重要上下文——用户可能在之前就表达了不满
- 请综合多轮上下文判断：导致用户不满的根因可能在前几轮

== 分析方法（跨轮推理）==
1. 优先看 ⚠ 标记的轮次，定位用户的纠正意图
2. 向前回溯 1-3 轮，找到 Jify 当时做了什么导致用户不满
3. 结合用户当时在做什么任务（工具调用、项目上下文），理解情境
4. 提炼出「在这个情境下，Jify 应该怎么做」的可执行原则

**好的经验长这样：**
- 「用户说『开始改吧』→ 直接改，别先分析方案」  ← 行为纠正
- 「解释协议时必须主动说明传输层独立性」          ← 行为纠正
- 「用户引入新概念时要先确认含义」                ← 行为纠正
- 「用户连续两次纠正同一方向 → 立即调整，不要坚持原方案」 ← 跨轮洞察

**以下不是经验，不要提取：**
- 「用户偏好 Unix domain sockets」  ← 这是用户画像
- 「选择了长度前缀协议方案」        ← 这是技术决策，不是行为纠正
- 「今天修了 3 个 bug」             ← 这是流水账
- 用户纠正自己的错误（不是纠正 Jify）← 这不是 Jify 的行为问题

对话上下文：
{conversation}

已有经验（避免语义重复，但角度不同仍可提取）：
{existing}

请用 JSON 格式输出新发现的经验（只输出 JSON，不要任何其他文字）：
{{
  "lessons": [
    {{
      "category": "踩坑记录 或 最佳实践",
      "content": "当...时，应...（可执行为原则，一句话中文）"
    }}
  ]
}}

规则：
- 只提取 Jify 行为层面的可执行经验
- category 只用「踩坑记录」（用户纠错/不满时）或「最佳实践」（用户满意/认可时）
- content 必须可执行：未来的 Jify 读了就知道在什么情境下该怎么做
- 跨轮推理优先：不要只看单轮，要追溯导致 ⚠ 的根因
- 如果对话中没有新的行为纠正经验，输出 {{"lessons": []}}"""

    def extract(self, conversation: str) -> List[Dict[str, str]]:
        """从对话上下文中提取新经验

        Args:
            conversation: 多轮对话的文本摘要

        Returns:
            提取到的经验列表 [{"category": "...", "content": "..."}, ...]
        """
        if not self.summarizer:
            return []

        data = self._load_json()
        # 扁平化为文本，供 LLM 去重参考
        existing_text = json.dumps(data, ensure_ascii=False, indent=2)
        if len(existing_text) > 2000:
            existing_text = existing_text[-2000:]

        prompt = self.EXTRACTION_PROMPT.format(
            conversation=conversation[:3000],
            existing=existing_text,
        )

        try:
            result = self.summarizer(prompt)
            result = result.strip()
            if result.startswith("```"):
                lines = result.split("\n")
                result = "\n".join(lines[1:-1]) if len(lines) > 2 else result
            parsed = json.loads(result)
            lessons = parsed.get("lessons", [])
            return lessons if isinstance(lessons, list) else []
        except (json.JSONDecodeError, Exception):
            return []

    def apply(self, lessons: List[Dict[str, str]]) -> bool:
        """将提取到的经验去重后合并到 JSON

        Returns:
            True 表示有新增内容
        """
        if not lessons:
            return False

        data = self._load_json()
        changed = False

        for lesson in lessons:
            category = lesson.get("category", "")
            content = lesson.get("content", "")
            if not content:
                continue
            key = self._category_to_key(category)
            if content not in data[key]:
                data[key].append(content)
                changed = True

        if changed:
            self._save_json(data)
        return changed


    # experience 加载注入 System Prompt
    def build_prompt_section(self) -> str:
        """构建注入 system prompt 的经验段落"""
        data = self._load_json()
        parts = []

        if data.get(self.KEY_BEST_PRACTICES):
            parts.append("## 最佳实践")
            for item in data[self.KEY_BEST_PRACTICES]:
                parts.append(f"- {item}")

        if data.get(self.KEY_PITFALLS):
            parts.append("\n## 踩坑记录")
            for item in data[self.KEY_PITFALLS]:
                parts.append(f"- {item}")

        return "\n".join(parts) if parts else ""
