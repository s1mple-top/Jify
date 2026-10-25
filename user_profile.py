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
    EXTRACTION_PROMPT = """你是一个用户使用喜好分析器。分析以下多轮对话历史，提取用户与助手协作过程中的「使用喜好」——即用户反复表现出的、跨任务稳定的交互习惯与工作风格，而非针对某个具体任务或问题的临时要求。

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

== 四条硬判据（不满足即丢弃，宁缺勿滥）==

【判据 1】去任务化测试
操作：把该条里所有具体实体（人名、公司名、病名、症状、文件名、函数名、库名、字段名、项目名、话题名等）替换成「X」，句子必须仍然成立且有意义。

  通过的例子：
    「用户要求错误消息使用中文」
    「用户偏好先给结论再分层拆解」
    「用户追问细节时要求先直接回答核心问题，再展开机制」
    → 把句中任意词替换成 X，句子依然在描述「用户想要什么」，跨任务成立。

  不通过的例子：
    「用户追问机制时，先直接回答核心问题（如'吸入为主还是皮肤接触为主'），再分层解释两种路径的区别」
    → 把「吸入为主还是皮肤接触为主」替换成 X，得到「先回答（如'X'）」——句子失去意义，说明它依赖某次健康问答的具体话题，是任务残留。
    → 修正后：「用户追问细节时要求先直接回答核心问题，再展开机制」

  不通过的例子：
    「偏好考虑实际数据规模（如TB级）的内存限制，避免一次性加载全量数据导致内存溢出」
    → 「TB级」「内存溢出」是某次任务的技术细节，替换成 X 后约束力归零。
    → 修正后：「用户要求方案考虑真实数据规模，避免一次性加载」

【判据 2】主语必须是用户
操作：偏好描述「用户的习惯/要求/反感」，不描述「助手某次答得怎么样」。禁止「偏好助手……」句式。

  通过的例子：
    「用户偏好先给结论再分层拆解」
    「用户反感过度确认，明确要求时希望直接执行」
    「用户被指出错误时要求先承认再修正」
    → 主语都是用户，说的是用户的稳定期待。

  不通过的例子：
    「偏好助手在分析岗位职责时，先给出直接结论，再逐层拆解，并明确区分核心职责与边缘能力」
    → 主语是「助手」，描述的是某次回答的形态，属于经验（experience），不属于画像；「岗位职责」还绑定了一个具体场景。
    → 修正后：「用户偏好先给结论再分层拆解」

  不通过的例子：
    「认为在多个调用方重复触发 hook 的方案'很挫'，追求最准确、覆盖所有路径的实现」
    → 这是用户的技术立场/认知，不是协作偏好，去掉项目语境后无法转化成「助手该怎么配合我」。
    → 处理：不提取。只有当它直接决定了做事方式，才写成行为要求，如「用户要求改动覆盖所有调用路径，不接受单点补丁」。

【判据 3】禁止举例、禁止限定场景
操作：raw_preferences 每条不超过 40 字；不得含括号补充；不得含「如'……'」「例如……」；不得绑定具体领域（岗位、健康、offer、JD、CVE、Docker 等）。

  通过的例子：
    「技术术语要求精确，会指出模糊表述并要求澄清」（21 字，无括号、无领域词）
    「验证结论时优先实际运行，而非仅静态推理」（19 字）
    → 不依赖任何具体场景，任何任务下都能直接约束助手行为。

  不通过的例子：
    「偏好助手在用户追问细节时，先直接回答核心问题（如'不需要临时下载'），再分层解释机制」
    → 三重违规：带括号举例、超 40 字、主语是助手。
    → 修正后：「用户追问细节时要求先答核心问题，再展开机制」

  不通过的例子：
    「偏好助手在分析JD匹配度时，先给出直接结论，再逐层拆解，并明确区分硬性门槛与核心职责」
    → 「JD」「硬性门槛」把偏好锁死在某次求职分析任务上，换个任务这条就悬空了。
    → 修正后：「用户偏好先给结论再分层拆解」

【判据 4】证据门槛
操作：只提取用户在对话中「明确表达」过（我偏好/我喜欢/不要/以后都这样/每次都要）或「明确纠正」过的偏好。

  通过的例子：
    用户说「不要写注释」「以后都用 Docker」「用表格呈现」
    → 用户亲自表态，可直接落库。

  不通过的例子：
    Jify 主动用表格回答了问题，用户没夸也没反对。
    → 没有表态就是没有证据，只能说明助手这次表现不错，不能反推用户偏好。宁可漏提，也不要凭猜测写进画像。

== 其他规则 ==
- 忽略一次性、针对某个具体问题的要求，不要沉淀具体任务内容（如"把 xx 文件改名"这类单次指令）
- 忽略基础设施/协调类工具的使用（如 team 多智能体协作、subagent 子任务、任务跟踪 update_todos、mcp 管理等），不要把"使用 team 模式"这类框架机制当作用户使用喜好沉淀
- 忽略纯粹的领域观点/技术立场（如"认为某方案很挫"、"embedding 有局限"），这些是认知，不是协作偏好；只有当它直接决定用户希望助手怎么做事时才保留，且必须写成行为要求
- 只输出新增的、变化的偏好，未检测到的字段留空
- tech_stack 只列出用户明确表达过"偏好使用"的技术，而非本次任务碰巧用到的技术
- raw_preferences 输出前自检：逐条问「换成另一个完全不同的任务，这条还成立吗？」不成立就删
- 上面判据中的例子仅用于说明标准，不要把示例原样当作本次提取结果；但有实质重合时可以参照其抽象程度
- 如果对话中没有新的使用喜好信息，输出空 JSON: {{}}"""

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

        return "\n".join(parts)
