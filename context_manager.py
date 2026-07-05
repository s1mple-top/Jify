# -*- coding: utf-8 -*-
"""
上下文管理器

维护跨轮次对话历史和会话摘要，供 gateway 和 agent_loop 统一使用。
- turn_history: 最近 N 轮完整记录（轮次级）
- session_summary: 超出窗口的旧轮次压缩为摘要文本
- 超出摘要长度上限时通过 summarizer（LLM）做二次压缩
"""

import threading
from dataclasses import dataclass, field
from typing import List, Optional, Callable


@dataclass
class TurnRecord:
    user_msg: str
    assistant_msg: str
    intent: str = ""  # 用户本轮意图快照（user_msg 前 100 字），供压缩时参考
    transcript: List[dict] = field(default_factory=list)

    def __post_init__(self):
        """自动从 user_msg 提取 intent（若未显式传入）。"""
        if not self.intent:
            self.intent = self.user_msg[:100].replace("\n", " ").strip()


class ContextManager:
    """跨轮次上下文管理。

    采用增量压缩策略：每 INCREMENTAL_COMPRESS_INTERVAL 轮将新轮次压缩
    追加到 session_summary，每次只处理「已有摘要 + 新轮次」，避免全量
    压缩带来的 token 开销。

    Attributes:
        turn_history: 最近 N 轮完整 TurnRecord
        session_summary: 旧轮次压缩后的文本摘要
        summarizer: LLM 摘要函数，签名 (prompt: str) -> str
    """

    # 常量
    MAX_RECENT_TURNS = 8               # 保留最近 N 轮完整记录
    INCREMENTAL_COMPRESS_INTERVAL = 4  # 每 N 轮触发一次增量压缩
    MAX_SESSION_SUMMARY_CHARS = 6000   # session_summary 最大字符数，超限触发 LLM 二次压缩

    def __init__(self, summarizer: Optional[Callable[[str], str]] = None):
        self.turn_history: List[TurnRecord] = []
        self.session_summary: str = ""
        self.summarizer = summarizer
        self._pending_compress: List[TurnRecord] = []  # 等待增量压缩的轮次
        self._summary_version: int = 0                  # 版本号，用于 CAS 写入防并发覆盖



    # 轮次生命周期
    def start_turn(self) -> None:
        """开始新轮次（当前为 no-op，保留扩展点）。"""


    def end_turn(self, user_msg: str, assistant_msg: str,
                 transcript: List[dict] = None) -> None:
        """结束一轮对话，追加到 turn_history。

        增量压缩策略：
        1. 新轮次加入 _pending_compress 缓冲区
        2. 每 INCREMENTAL_COMPRESS_INTERVAL 轮在后台线程触发增量压缩：
           将已有 session_summary + 缓冲区中的新轮次一并交给 summarizer，
           要求仅追加、不删除
        3. 若 session_summary 仍超长，在后台线程触发 LLM 二次压缩作为安全兜底

        所有 LLM 调用均为 fire-and-forget 后台线程，end_turn 立即返回。
        """
        intent = user_msg[:100].replace("\n", " ").strip() # 截取前 100 作为意图，人总是渴望预先表达情感意图
        record = TurnRecord(
            user_msg=user_msg,
            assistant_msg=assistant_msg,
            intent=intent,
            transcript=transcript or [],
        )
        self.turn_history.append(record)
        self._pending_compress.append(record)

        # turn_history 保留全部轮次，不截断（摘要仅作为超长替换的后备资源）

        # 增量压缩：每 N 轮在后台线程中将缓冲区的新轮次压缩追加到 session_summary
        if len(self._pending_compress) >= self.INCREMENTAL_COMPRESS_INTERVAL:
            if self.summarizer:
                pending_snapshot = list(self._pending_compress)
                current_summary = self.session_summary
                threading.Thread(
                    target=self._incremental_compress,
                    args=(current_summary, pending_snapshot),
                    daemon=True,
                ).start()
            else:
                # 未配置 summarizer 时退化为纯文本拼接（同步，开销极小）
                for t in self._pending_compress:
                    sep = "\n\n" if self.session_summary else ""
                    self.session_summary += sep + self._format_turn(t)
            self._pending_compress.clear()

        # 安全兜底：session_summary 超长 → 后台线程全量二次压缩
        if (self.summarizer
                and len(self.session_summary) > self.MAX_SESSION_SUMMARY_CHARS):
            overloaded_summary = self.session_summary
            threading.Thread(
                target=self._llm_compress_and_update,
                args=(overloaded_summary,),
                daemon=True,
            ).start()


    # 系统上下文构建
    def build_system_context(self, token_budget: int = 3000) -> str:
        """构建注入 system prompt 的会话上下文。

        截断到 token_budget * 4 字符（粗略估计），避免挤占 system prompt。

        Args:
            token_budget: 分配给该上下文的 token 上限

        Returns:
            截断后的会话摘要文本；无摘要则返回空字符串
        """
        summary = self.get_session_summary()
        if not summary:
            return ""
        max_chars = token_budget * 4
        if len(summary) <= max_chars:
            return summary
        return summary[:max_chars]

    def build_user_context(self, user_message: str) -> str:
        """构建用户消息（拼接全部对话历史）。

        摘要不注入日常交互，仅作为超长替换的后备资源。
        若无历史轮次，直接返回原始消息。

        Args:
            user_message: 用户当前输入

        Returns:
            包含全部历史轮次和当前输入的组合文本
        """
        if not self.turn_history:
            return user_message

        parts = []

        # 全部历史轮次，暂时将所有的tool执行结果全部纳入上下文
        parts.append("=== 对话历史 ===")
        for t in self.turn_history:
            parts.append(self._format_turn(t))
        parts.append("")

        # 当前消息
        parts.append("=== 当前消息 ===")
        parts.append(user_message)

        return "\n".join(parts)


    # 摘要输出
    def get_session_summary(self) -> str:
        """返回完整会话摘要：session_summary + turn_history 格式化文本。"""
        parts = []
        if self.session_summary:
            parts.append(self.session_summary)
        for t in self.turn_history:
            parts.append(self._format_turn(t))
        return "\n\n".join(parts) if parts else ""

    # 复用 build_compression_context 别名
    def build_compression_context(self) -> str:
        """构建压缩用的结构化上下文，保留用户意图、关键行为、关键结果。"""
        return self.get_session_summary()


    # 内部辅助
    @staticmethod
    def _format_turn(t: TurnRecord) -> str:
        """将 TurnRecord 格式化为结构化文本。"""
        if t.transcript:
            lines = []
            for entry in t.transcript:
                role = entry.get("role", "")
                if role == "user":
                    lines.append(f"用户: {entry.get('content', '')}")
                elif role == "tool":
                    name = entry.get("name", "unknown")
                    result = entry.get("result", "")
                    if result:
                        lines.append(f"工具结果 [{name}]:\n{result}")
                    else:
                        lines.append(f"工具调用: {name}")
                elif role == "assistant":
                    lines.append(f"Jify: {entry.get('content', '')}")
            return "\n".join(lines)
        return f"用户: {t.user_msg}\nJify: {t.assistant_msg}"

    MAX_COMPRESS_RETRIES = 3  # CAS 冲突最大重试次数

    def _incremental_compress(self, current_summary: str,
                             pending_turns: List[TurnRecord]) -> None:
        """增量压缩：在已有 summary 基础上追加新轮次信息（后台线程调用）。

        采用 CAS 语义写入 session_summary：写入前比对版本号，
        若已被其他线程更新则基于最新 summary 重试，防并发覆盖。

        Args:
            current_summary: 触发压缩时的 session_summary 快照
            pending_turns: 触发压缩时的 _pending_compress 快照
        """
        version_snapshot = self._summary_version

        new_turns_text = "\n\n".join(
            self._format_turn(t) for t in pending_turns
        )

        if current_summary:
            prompt = (
                "以下是之前对话的摘要，请严格保留其全部内容，"
                "只能追加、不能删除或修改已有摘要中的任何内容：\n\n"
                f"{current_summary}\n\n"
                "以下是新的对话轮次，请将其中的用户意图、关键结果、"
                "主要进展、正在进行还未完成的工作、以及上述摘要里未完成当前新轮次里已完成的工作，追加整合到摘要末尾：\n\n"
                f"{new_turns_text}"
            )
        else:
            prompt = (
                "请总结以下对话轮次中的用户意图、关键结果和主要进展，要求尽可能简洁但保留核心：\n\n"
                f"{new_turns_text}"
            )

        for _attempt in range(self.MAX_COMPRESS_RETRIES):
            try:
                result = self.summarizer(prompt)
            except Exception:
                return  # 后台压缩失败不影响主流程

            if not result:
                return

            # CAS 写入：只有版本号未变才允许覆盖
            if self._summary_version == version_snapshot:
                self.session_summary = result
                self._summary_version += 1
                return

            # 冲突：summary 已被其他线程更新，基于最新摘要重建 prompt
            latest_summary = self.session_summary
            version_snapshot = self._summary_version
            if latest_summary:
                prompt = (
                    "以下是之前对话的摘要，请严格保留其全部内容，"
                    "只能追加、不能删除或修改已有摘要中的任何内容：\n\n"
                    f"{latest_summary}\n\n"
                    "以下是新的对话轮次，请将其中的用户意图、关键结果、"
                    "主要进展、正在进行还未完成的工作、以及上述摘要里未完成当前新轮次里已完成的工作，追加整合到摘要末尾：\n\n"
                    f"{new_turns_text}"
                )
            # else: 仍为首次压缩，prompt 不变，直接重试

        # 重试耗尽：静默丢弃（极端并发下极小概率触发）

    def _llm_compress(self, prompt: str) -> str:
        """通过 summarizer 调用 LLM 压缩文本（全量二次压缩，安全兜底用）。

        Args:
            prompt: 待压缩文本

        Returns:
            压缩后文本；若 summarizer 不可用或失败则返回原文
        """
        if not self.summarizer:
            return prompt
        try:
            compress_prompt = (
                "请对以下对话摘要进行二次压缩，去除冗余但保留所有关键信息"
                "（用户意图、关键结果、主要进展、正在进行还未完成的工作）：\n\n"
                f"{prompt}"
            )
            result = self.summarizer(compress_prompt)
            return result if result else prompt
        except Exception:
            return prompt

    def _llm_compress_and_update(self, overloaded_summary: str) -> None:
        """后台线程入口：对超长的 session_summary 做全量压缩并 CAS 更新。"""
        version_snapshot = self._summary_version
        compressed = self._llm_compress(overloaded_summary)
        if compressed and compressed != overloaded_summary:
            if self._summary_version == version_snapshot:
                self.session_summary = compressed
                self._summary_version += 1
