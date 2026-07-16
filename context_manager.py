# -*- coding: utf-8 -*-
"""
上下文管理器

维护跨轮次对话历史和会话摘要，供 gateway 和 agent_loop 统一使用。
- turn_history: 最近 N 轮完整记录（轮次级）
- session_summary: 超出窗口的旧轮次压缩为摘要文本
- 超出摘要长度上限时通过 summarizer（LLM）做二次压缩
"""

import queue
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
    # MAX_RECENT_TURNS = 8               # 保留最近 N 轮完整记录
    INCREMENTAL_COMPRESS_INTERVAL = 4  # 每 N 轮触发一次增量压缩
    MAX_SESSION_SUMMARY_CHARS = 60000   # session_summary 最大字符数，超限触发 LLM 二次压缩
    KEEP_RECENT_TURNS = 2              # 截断时至少保留的未压缩轮数

    def __init__(self, summarizer: Optional[Callable[[str], str]] = None):
        self.turn_history: List[TurnRecord] = []
        self.session_summary: str = ""
        self.summarizer = summarizer
        self._pending_compress: List[TurnRecord] = []  # 等待增量压缩的轮次

        # 单一后台线程串行消费压缩任务，消除并发写入冲突
        self._compress_queue: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._compress_worker, daemon=True)
        self._worker.start()


    def shutdown(self) -> None:
        """优雅关闭后台压缩线程"""
        self._compress_queue.put(None)
        if self._worker.is_alive():
            self._worker.join(timeout=10)

    def end_turn(self, user_msg: str, assistant_msg: str,
                 transcript: List[dict] = None) -> None:
        """结束一轮对话，添加到 turn_history。

        增量压缩策略：
        1. 新轮次加入 _pending_compress 缓冲区
        2. 每 INCREMENTAL_COMPRESS_INTERVAL 轮将缓冲区放入压缩队列，
           由后台线程串行消费，读取当前 session_summary 做增量压缩
        3. 若 session_summary 仍超长，也入队触发 LLM 二次压缩

        所有 LLM 调用均通过队列异步处理，end_turn 立即返回。
        """
        intent = user_msg[:100].replace("\n", " ").strip()
        record = TurnRecord(
            user_msg=user_msg,
            assistant_msg=assistant_msg,
            intent=intent,
            transcript=transcript or [],
        )
        self.turn_history.append(record)
        self._pending_compress.append(record)

        # 增量压缩：每 N 轮将缓冲区入队
        if len(self._pending_compress) >= self.INCREMENTAL_COMPRESS_INTERVAL:
            if self.summarizer:
                pending_snapshot = list(self._pending_compress)
                self._compress_queue.put(("inc", pending_snapshot))
            else:
                # 未配置 summarizer 时退化为纯文本拼接（同步，开销极小）
                for t in self._pending_compress:
                    sep = "\n\n" if self.session_summary else ""
                    self.session_summary += sep + self._format_turn(t)
            self._pending_compress.clear()

        # 安全兜底：session_summary 超长 → 入队触发二次压缩
        if (self.summarizer
                and len(self.session_summary) > self.MAX_SESSION_SUMMARY_CHARS):
            self._compress_queue.put(("compact",))

    # # 系统上下文构建
    # def build_system_context(self, token_budget: int = 3000) -> str:
    #     """构建注入 system prompt 的会话上下文。
    #
    #     截断到 token_budget * 4 字符（粗略估计），避免挤占 system prompt。
    #
    #     Args:
    #         token_budget: 分配给该上下文的 token 上限
    #
    #     Returns:
    #         截断后的会话摘要文本；无摘要则返回空字符串
    #     """
    #     summary = self.get_session_summary()
    #     if not summary:
    #         return ""
    #     max_chars = token_budget * 4
    #     if len(summary) <= max_chars:
    #         return summary
    #     return summary[:max_chars]

    def build_user_context(self, user_message: str) -> str:
        """构建用户消息（拼接全部对话历史）。

        不主动前置摘要——摘要仅在超阈值压缩时由 _rebuild_messages 注入。
        正常流程保留完整上下文，确保压缩前模型能看到全部细节。

        Args:
            user_message: 用户当前输入
        Returns:
            全部历史轮次 + 当前输入的组合文本；首轮直接返回原始消息
        """
        if not self.turn_history:
            return user_message

        parts = ["=== 对话历史 ==="]
        for t in self.turn_history:
            parts.append(self._format_turn(t))
        parts.append("")
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

    def build_compression_context(self) -> str:
        """构建压缩用的结构化上下文，保留用户意图、关键行为、关键结果。"""
        return self.get_session_summary()

    def truncate_compressed_turns(self) -> bool:
        """移除已被增量压缩过的旧轮次，只保留未被压缩的最近轮次。

        当 session_summary 存在时，turn_history 中已被压缩进摘要的轮次可以安全移除。
        保留策略：_pending_compress 中的轮次精确代表"尚未被旁路压缩"的轮次，
        保留它们即可。若 pending 为空（极端情况），用 KEEP_RECENT_TURNS 兜底。

        Returns:
            True 如果发生了截断
        """
        if not self.session_summary:
            return False

        pending_count = len(self._pending_compress)
        keep_count = pending_count if pending_count > 0 else self.KEEP_RECENT_TURNS
        if len(self.turn_history) <= keep_count:
            return False

        self.turn_history = self.turn_history[-keep_count:]
        return True

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


    def _compress_worker(self) -> None:
        """后台线程：串行消费压缩队列，消除并发写入冲突。"""
        while True:
            item = self._compress_queue.get()
            if item is None:  # 哨兵：优雅退出
                break
            task_type = item[0]
            if task_type == "inc":
                self._do_incremental_compress(item[1])
            elif task_type == "compact":
                self._do_compact()

    def _do_incremental_compress(self, pending_turns: List[TurnRecord]) -> None:
        """增量压缩：基于当前 session_summary 追加新轮次信息。

        读取当前 session_summary（而非入队时快照），确保包含之前批次
        已完成的压缩结果。由于只有本线程写 session_summary，无需 CAS。

        Args:
            pending_turns: 入队时的 _pending_compress 快照
        """
        new_turns_text = "\n\n".join(
            self._format_turn(t) for t in pending_turns
        )

        current_summary = self.session_summary
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

        try:
            result = self.summarizer(prompt)
        except Exception:
            return  # 后台压缩失败不影响主流程

        if result:
            self.session_summary = result

    def _do_compact(self) -> None:
        """二次压缩：对超长的 session_summary 做全量 LLM 压缩。

        读取当前 session_summary，压缩后写回。由于只有本线程写，
        无需 CAS。
        """
        current = self.session_summary
        if len(current) <= self.MAX_SESSION_SUMMARY_CHARS:
            return  # 已被之前的 compact 任务处理过，无需重复压缩

        compressed = self._llm_compress(current)
        if compressed and compressed != current:
            self.session_summary = compressed

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
