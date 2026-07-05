# -*- coding: utf-8 -*-
"""
自进化引擎 — 独立后台线程，不阻塞主对话流

架构:
  AgentLoop.run() 返回后
    └─ SelfEvolutionEngine.submit(task)  ← 非阻塞入队，立刻返回
         │
         ▼
    独立 daemon 线程 + ThreadPoolExecutor(max_workers=1)
         │
         ├─ 1: 提取用户偏好 → user_profile.json
         ├─ 2: 提取可复用经验
         └─ 3: 检测技能模式

拥有独立的 model_client，线程安全。
"""

import threading
import concurrent.futures
import time
from dataclasses import dataclass, field
from typing import Optional, Callable, Dict, Any, List
from enum import Enum


class TaskPhase(Enum):
    PROFILE = "profile"           # 用户画像提取
    EXPERIENCE = "experience"     # 经验自动提取
    SKILL = "skill"               # 技能模式检测


@dataclass
class EvolutionTask:
    phase: TaskPhase
    user_msg: str = ""
    assistant_msg: str = ""
    tools_used: List[str] = field(default_factory=list)  # 本轮用到的工具
    outcome: str = ""  # 本轮实际产出摘要
    user_feedback: str = ""
    timestamp: float = field(default_factory=time.time)


class SelfEvolutionEngine:
    """
    自进化引擎。
    """

    def __init__(self, summarizer: Callable[[str], str],
                 profile_interval: int = 8,
                 user_id: str = "cli_user"):
        """
        Args:
            summarizer: LLM 调用接口 callable(prompt: str) -> str
            profile_interval: 每 N 轮做一次画像提取（默认 5）
            user_id: 用户唯一标识，CLI 默认 "cli_user"，网关模式传实际用户ID
        """
        self.summarizer = summarizer
        self.user_id = user_id
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="evolution"
        )
        self._profile_extractor = None   # 延迟初始化，避免循环导入
        self._experience_extractor = None
        self._skill_detector = None

        # 轮次计数
        self._turn_count: int = 0
        self._profile_interval: int = profile_interval  # 每 N 轮提取画像
        self._experience_interval: int = 5  # 每 5 轮尝试提取经验

        # 对话累积缓冲（结构化存储，供经验提取和用户画像使用）
        self._conversation_buffer: List[Dict[str, Any]] = []
        self._BUFFER_MAX_LEN: int = 30         # 缓冲区最大轮次
        self._EXPERIENCE_CONTEXT_TURNS: int = 5  # 经验提取触发时取最近 N 轮
        self._PROFILE_CONTEXT_TURNS: int = 5     # 用户画像提取触发时取最近 N 轮

        # 信号打分触发（替代纯轮次触发）
        self._last_experience_turn: int = 0  # 上次触发经验提取的轮次
        self._experience_min_gap: int = 3   # 关键词触发的最小间隔（防止连续重复提取）
        self._experience_max_gap: int = 10   # 最多 N 轮强制触发一次（上限保护）

        # 触发间隔（每 M 轮检测一次技能模式）
        self._skill_interval: int = 10  # 每 10 轮检测一次


    # 公开接口
    def submit(self, task: EvolutionTask) -> None:
        """非阻塞提交进化任务，立刻返回"""
        self._executor.submit(self._process, task)

    def shutdown(self, wait: bool = False) -> None:
        """优雅关闭（进程退出时调用）"""
        self._executor.shutdown(wait=wait)

    # 内部处理
    def _process(self, task: EvolutionTask) -> None:
        """在后台线程中处理任务"""
        try:
            self._turn_count += 1

            # 每 N 轮做一次画像提取
            if self._turn_count % self._profile_interval == 0:
                self._do_profile_safe()

            # 追加到对话缓冲 — 结构化存储（提升截断上限以保留完整上下文）
            is_correction = self._is_negative_feedback(task.user_msg)
            self._conversation_buffer.append({
                "turn": self._turn_count,
                "user": task.user_msg,
                "assistant": task.assistant_msg[:8000],  # 提升上限，保留更完整的模型回复
                "tools": task.tools_used,
                "outcome": task.outcome[:8000] if task.outcome else "",
                "is_correction": is_correction,
            })

            # 滑动窗口
            if len(self._conversation_buffer) > self._BUFFER_MAX_LEN:
                self._conversation_buffer = self._conversation_buffer[-self._BUFFER_MAX_LEN:]

            # 累积任务给技能检测器，单独处理，后续将非toolcall的轮次剔除掉
            self._accumulate_for_skill(task)

            # 每 M 轮触发技能检测
            if self._turn_count % self._skill_interval == 0:
                self._do_skill_safe()

            # 信号打分触发经验提取（替代纯轮次触发）
            if self._should_extract_experience(task):
                self._do_experience_safe(task)

        except Exception:
            pass  # 顶层兜底，防止污染主流程

    def _accumulate_for_skill(self, task: EvolutionTask) -> None:
        """累积任务数据供沉淀 skill 使用"""
        if self._skill_detector is None:
            from skill_detector import SkillDetector
            self._skill_detector = SkillDetector(summarizer=self.summarizer, user_id=self.user_id)
        self._skill_detector.add_task(
            user_msg=task.user_msg,
            assistant_msg=task.assistant_msg,
            tools_used=task.tools_used,
            outcome=task.outcome,
        )


    # 异常日志与安全包装
    @staticmethod
    def _log_exception(phase: str, exc: Exception) -> None:
        """记录异常到 log.txt，包含完整 traceback"""
        import datetime
        import traceback
        tb = traceback.format_exc()
        from pathlib import Path
        log_dir = Path.home() / ".jify" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "log.txt", "a", encoding="utf-8") as f:
            f.write(
                f"[{datetime.datetime.now().isoformat()}] "
                f"Phase={phase} ERROR: {exc}\n{tb}\n"
            )

    def _do_profile_safe(self) -> None:
        try:
            self._do_profile()
        except Exception as e:
            self._log_exception("profile", e)

    def _do_experience_safe(self, task: EvolutionTask) -> None:
        try:
            self._do_experience(task)
        except Exception as e:
            self._log_exception("experience", e)

    def _do_skill_safe(self) -> None:
        try:
            self._do_skill()
        except Exception as e:
            self._log_exception("skill", e)

    # ── 用户负反馈检测 ──
    _NEGATIVE_PATTERNS = [
        "不对", "错了", "不是", "有问题", "不满意", "不行", "不要",
        "重新", "改回", "恢复", "不是这样的", "你搞错了",
        "这不是我想要的", "别", "别这样", "搞什么",
    ]

    @classmethod
    def _is_negative_feedback(cls, user_msg: str) -> bool:
        """检测用户消息是否包含纠错/不满信号"""
        msg = user_msg.lower()
        return any(p in msg for p in cls._NEGATIVE_PATTERNS)


    # 用户画像
    def _do_profile(self) -> None:
        """从多轮对话中提取用户偏好

        与经验提取共用 _conversation_buffer，取最近 N 轮完整上下文发给模型，
        使模型能跨轮推断用户偏好（如连续多轮的交互节奏偏好、技术栈偏好等）。
        """
        if not self._conversation_buffer:
            return

        # 延迟初始化
        if self._profile_extractor is None:
            from user_profile import UserProfileExtractor
            self._profile_extractor = UserProfileExtractor(
                summarizer=self.summarizer, user_id=self.user_id
            )

        # 取最近 N 轮构建多轮对话上下文
        recent_turns = self._conversation_buffer[-self._PROFILE_CONTEXT_TURNS:]

        lines = []
        lines.append(f"=== 最近 {len(recent_turns)} 轮对话（供用户画像提取） ===")
        lines.append("")

        for entry in recent_turns:
            turn_num = entry.get("turn", "?")
            lines.append(f"--- 第 {turn_num} 轮 ---")

            user_msg = entry.get("user", "")
            lines.append(f"[用户] {user_msg}")

            tools = entry.get("tools", [])
            if tools:
                lines.append(f"[本轮使用的工具] {', '.join(tools)}")

            assistant_msg = entry.get("assistant", "")
            if assistant_msg:
                lines.append(f"[Jify 回复] {assistant_msg}")

            lines.append("")

        conversation = "\n".join(lines)

        new_prefs = self._profile_extractor.extract(conversation) # 提取用户偏好

        # 记录模型返回日志
        import datetime
        from pathlib import Path
        log_dir = Path.home() / ".jify" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "log.txt", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now().isoformat()}] _do_profile result: {new_prefs}\n")

        if new_prefs:
            self._profile_extractor.apply(new_prefs)


    # 经验自动提取
    # 信号打分（轻量，不走 LLM）
    def _score_experience_signal(self, task: EvolutionTask) -> int:
        """对本轮对话做轻量信号打分，超过阈值才触发 LLM 提取。

        打分规则: 用户纠错/不满 → +3（唯一信号源）

        工具调用数和代码变更已被移除——前者无法区分「复杂任务」和「反复重试」，
        后者纯机械操作不应等价于经验积累。

        阈值: >= 3 分触发（即仅用户纠错/不满触发）。

        后续还需要加入其他规则
        """
        if self._is_negative_feedback(task.user_msg):
            return 3
        return 0

    def _should_extract_experience(self, task: EvolutionTask) -> bool:
        """判断是否应触发经验提取。

        触发条件:
          1. 上限保护: gap >= _experience_max_gap → 强制触发（防止长期不提取）
          2. 关键词触发: gap >= _experience_min_gap 且 score >= 3 → 触发
          若 gap < _experience_min_gap，即使关键词命中也不触发（防止连续重复提取）
        """
        gap = self._turn_count - self._last_experience_turn

        # 上限保护：太久没提取，强制触发
        if gap >= self._experience_max_gap:
            return True

        # 关键词触发需要满足最小间隔
        if gap < self._experience_min_gap:
            return False

        score = self._score_experience_signal(task)
        return score >= 3


    def _do_experience(self, task: EvolutionTask) -> None:
        """从多轮对话中提取可复用经验

        关键词匹配触发后，将最近 N 轮完整对话上下文发送给模型进行精准总结。
        注意：此处不再次做关键词判定——触发已由 _should_extract_experience 完成，
        模型拿到完整上下文后自行判断是否存在可提取的行为纠正经验。
        """
        if not self._conversation_buffer:
            return

        if self._experience_extractor is None:
            from experience_extractor import ExperienceExtractor
            self._experience_extractor = ExperienceExtractor(
                summarizer=self.summarizer
            )

        # 取最近 N 轮构建多轮对话上下文
        recent_turns = self._conversation_buffer[-self._EXPERIENCE_CONTEXT_TURNS:]

        # 构建带时间线的结构化上下文文本
        lines = []
        lines.append("=== 最近多轮对话上下文（供经验提取） ===")
        lines.append(f"共 {len(recent_turns)} 轮，其中关键词命中的轮次已标注 ⚠ 纠错信号")
        lines.append("")

        for i, entry in enumerate(recent_turns):
            turn_num = entry.get("turn", "?")
            is_correction = entry.get("is_correction", False)

            # 轮次分隔与标记
            correction_flag = " ⚠ 用户纠正/不满" if is_correction else ""
            lines.append(f"--- 第 {turn_num} 轮{correction_flag} ---")

            # 用户消息
            user_msg = entry.get("user", "")
            lines.append(f"[用户] {user_msg}")

            # 工具调用
            tools = entry.get("tools", [])
            if tools:
                lines.append(f"[本轮使用的工具] {', '.join(tools)}")

            # 模型回复
            assistant_msg = entry.get("assistant", "")
            if assistant_msg:
                lines.append(f"[Jify 回复] {assistant_msg}")

            # 产出摘要（如有）
            outcome = entry.get("outcome", "")
            if outcome and outcome != assistant_msg:
                lines.append(f"[产出摘要] {outcome}")

            lines.append("")

        conversation = "\n".join(lines)

        lessons = self._experience_extractor.extract(conversation)
        if lessons:
            self._experience_extractor.apply(lessons)

        self._last_experience_turn = self._turn_count


    # 技能模式检测
    def _do_skill(self) -> None:
        """检测反复执行的任务模式，生成 skill 建议"""
        if self._skill_detector is None:
            from skill_detector import SkillDetector
            self._skill_detector = SkillDetector(summarizer=self.summarizer, user_id=self.user_id)

        patterns = self._skill_detector.detect()
        if patterns:
            self._skill_detector.apply(patterns)


    # 对外查询接口
    def get_profile_section(self) -> str:
        """获取注入 system prompt 的用户偏好段落"""
        if self._profile_extractor is None:
            from user_profile import UserProfileExtractor
            self._profile_extractor = UserProfileExtractor(
                summarizer=self.summarizer, user_id=self.user_id
            )
        return self._profile_extractor.build_prompt_section()

    # Skill 审批接口（供 CLI 轮询）
    def get_pending_skills(self):
        """返回待审批的 skill 建议列表"""
        if self._skill_detector is None:
            return []
        return self._skill_detector.get_pending_approval()

    def approve_skill(self, name: str) -> bool:
        """审批通过一个 skill，落盘 SKILL.md"""
        if self._skill_detector is None:
            return False
        return self._skill_detector.approve(name)

    def reject_skill(self, name: str) -> bool:
        """拒绝一个 skill，加入黑名单不再弹窗"""
        if self._skill_detector is None:
            return False
        return self._skill_detector.reject(name)
