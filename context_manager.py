# -*- coding: utf-8 -*-
"""
上下文管理器

维护跨轮次对话历史的**旁路摘要**（session_summary）与待压缩轮次缓冲，供 agent_loop 使用。

设计要点（配合 append-only 消息序列）：
- 真正的对话历史就是 agent_loop 中的 messages 序列（user/assistant/tool 逐条 append，不再重格式化）
- 本模块只负责「旁路预算」：每 INCREMENTAL_COMPRESS_INTERVAL 轮把新轮次异步折叠进 session_summary
- 摘要产物**不进入请求序列**；只有当 agent_loop 检测到上下文超阈值时，才把 session_summary
  作为**一条合成消息**注入到序列前端（一次性前缀替换），其余轮次继续 append
- _pending_compress 始终表示「尚未折叠进 session_summary 的轮次」；
  只有后台压缩**成功后**才从缓冲移除，避免折叠瞬间出现内容缺口

超阈值折叠的两条路径（由 agent_loop._compact_prefix 实现）：
- 路径 A（有可直接替换的旧正文）：用 session_summary 替换前缀，【零同步 LLM 调用】
- 路径 B（无可替换内容，即旧正文已全部摘要化）：把「现有摘要 + 未压缩轮次原文」
  合并压缩成新摘要后整体替换（**同步一次 LLM**），立即收敛，不必等攒够 4 轮

发送前预检语义（agent_loop._maybe_compact_prefix(preserve_current=True)）：
- 检查点从「迭代末」提前到「发请求前」，防止单次 tool 结果暴涨一步跨过真实模型窗口
- 预检模式下无旁路摘要时【只压本轮之前的历史，保留本轮 user 原文】；第一轮无历史
  可压则不动作。原因：此轮尚未产出回复，若压「含本轮」会把用户真实问题摘要化后
  发给模型，语义错误
- 迭代末（preserve_current=False）触发时本轮已产出回复，才允许「含本轮」兜底全量
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
    start_idx: int = 0  # 本轮在 messages 中的起始下标（用于计算「已压缩边界」）

    def __post_init__(self):
        """自动从 user_msg 提取 intent（若未显式传入）。"""
        if not self.intent:
            self.intent = self.user_msg[:100].replace("\n", " ").strip()


class ContextManager:
    """跨轮次旁路上下文管理。

    采用增量压缩策略：每 INCREMENTAL_COMPRESS_INTERVAL 轮将新轮次异步折叠进
    session_summary，避免全量压缩带来的 token 开销。

    Attributes:
        session_summary: 旧轮次压缩后的文本摘要（旁路产物，不主动注入请求）
        summarizer: LLM 摘要函数，签名 (prompt: str) -> str
        _pending_compress: 尚未折叠进 session_summary 的轮次
    """

    # 常量
    INCREMENTAL_COMPRESS_INTERVAL = 4  # 每 N 轮触发一次增量压缩
    MAX_SESSION_SUMMARY_CHARS = 120000   # session_summary 最大字符数，超限触发 LLM 二次压缩

    def __init__(self, summarizer: Optional[Callable[[str], str]] = None):
        self.session_summary: str = ""
        self.summarizer = summarizer
        self._pending_compress: List[TurnRecord] = []  # 尚未折叠进摘要的轮次
        self._skip_pending_once: bool = False  # 兜底整体压缩已含当前轮，下一轮 end_turn 跳过入 pending
        self._summary_lock = threading.Lock()

        # 单一后台线程串行消费压缩任务，消除并发写入冲突
        self._compress_queue: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._compress_worker, daemon=True)
        self._worker.start()

    def shutdown(self) -> None:
        """优雅关闭后台压缩线程"""
        self._compress_queue.put(None)
        # 不 join：/clear 后会新建 ContextManager，旧摘要结果已被丢弃，
        # 等待积压的 LLM 摘要任务纯属浪费。daemon 线程跑完后因哨兵自然退出。

    def end_turn(self, user_msg: str, assistant_msg: str,
                 transcript: List[dict] = None, start_idx: int = 0) -> None:
        """结束一轮对话，将本轮加入待压缩缓冲，并按节奏触发后台增量压缩。

        增量压缩策略：
        1. 新轮次加入 _pending_compress 缓冲区
        2. 每 INCREMENTAL_COMPRESS_INTERVAL 轮将缓冲区快照入队，由后台线程
           串行消费（压缩成功后从缓冲区移除，避免折叠瞬间缺口）
        3. 若 session_summary 仍超长，也入队触发 LLM 二次压缩

        所有 LLM 调用均通过队列异步处理，end_turn 立即返回。
        """
        intent = user_msg[:100].replace("\n", " ").strip()
        record = TurnRecord(
            user_msg=user_msg,
            assistant_msg=assistant_msg,
            intent=intent,
            transcript=transcript or [],
            start_idx=start_idx,
        )

        # 兜底整体压缩（_compact_prefix 无摘要分支）已把本轮内容并入 session_summary，
        # 本轮不应重复入 pending，否则会被增量压缩二次概括，造成内容重复与 start_idx 冲突。
        if self._skip_pending_once:
            self._skip_pending_once = False
            return

        self._pending_compress.append(record)

        # 增量压缩：每 N 轮将缓冲区快照入队
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

    def get_session_summary(self) -> str:
        """返回旁路摘要文本（仅 session_summary 本身，不含未压缩轮次）。"""
        return self.session_summary

    def get_compress_boundary(self, default_idx: int) -> int:
        """返回 session_summary 已覆盖到的 messages 边界下标。

        即「最老未压缩轮次」的起始下标：该下标之前的全部内容都已折进
        session_summary，可安全用摘要替换；其后（pending 轮次）尚未压缩，
        须原样保留在正文，避免出现「既不在摘要、又被删除」的内容缺口。

        若无 pending（全部轮次均已压缩），返回 default_idx（当前轮起点）。
        """
        if self._pending_compress:
            return min(t.start_idx for t in self._pending_compress)
        return default_idx

    def rebase(self, shift: int) -> None:
        """替换前缀后，同步平移所有未压缩轮次记录的 start_idx。

        只在确实有 pending 时生效；shift 为 messages 前缀缩减量。
        """
        if shift == 0:
            return
        for t in self._pending_compress:
            t.start_idx += shift

    def flush_pending_sync(self) -> bool:
        """同步把待压缩轮次折叠进 session_summary。

        供 agent_loop 在**前缀折叠前**调用，确保 session_summary 覆盖到折叠边界，
        避免出现「既不在摘要、又被折叠掉」的内容缺口。仅在确实有 pending 时才调 LLM。

        Returns:
            True 表示 pending 已成功折叠（或本就为空），session_summary 已覆盖折叠边界；
            False 表示 LLM 摘要失败，pending 未折叠，调用方需重试或降级全量压缩。
        """
        if not self.summarizer:
            return True
        with self._summary_lock:
            turns = list(self._pending_compress)
            if not turns:
                return True
            new_turns_text = "\n\n".join(self._format_turn(t) for t in turns)
            current_summary = self.session_summary
            if current_summary:
                prompt = (
                    "以下是之前对话的摘要，请严格保留其全部内容，"
                    "只能追加、不能删除或修改已有摘要中的任何内容：\n\n"
                    f"{current_summary}\n\n"
                    "以下是新的对话轮次，请将其中的用户意图、关键结果、"
                    "主要进展、正在进行还未完成的工作追加整合到摘要末尾：\n\n"
                    f"{new_turns_text}"
                )
            else:
                prompt = (
                    "请总结以下对话轮次中的用户意图、关键结果和主要进展，"
                    "要求尽可能简洁但保留核心：\n\n"
                    f"{new_turns_text}"
                )
            try:
                result = self.summarizer(prompt)
            except Exception:
                return False
            if not result:
                return False
            self.session_summary = result
            self._pending_compress = [
                t for t in self._pending_compress if t not in turns
            ]
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
        """增量压缩：基于当前 session_summary 追加新轮次信息。成功后从缓冲移除。"""
        with self._summary_lock:
            # 已被 flush 处理过的轮次可能已不在 pending，过滤掉
            turns = [t for t in pending_turns if t in self._pending_compress]
            if not turns:
                return
            new_turns_text = "\n\n".join(self._format_turn(t) for t in turns)

            current_summary = self.session_summary
            if current_summary:
                prompt = (
                    "以下是之前对话的摘要，请严格保留其全部内容，"
                    "只能追加、不能删除或修改已有摘要中的任何内容：\n\n"
                    f"{current_summary}\n\n"
                    "以下是新的对话轮次，请将其中的用户意图、关键结果、"
                    "主要进展、正在进行还未完成的工作追加整合到摘要末尾：\n\n"
                    f"{new_turns_text}"
                )
            else:
                prompt = (
                    "请总结以下对话轮次中的用户意图、关键结果和主要进展，"
                    "要求尽可能简洁但保留核心：\n\n"
                    f"{new_turns_text}"
                )

            try:
                result = self.summarizer(prompt)
            except Exception:
                return  # 后台压缩失败不影响主流程，pending 保留待下次重试

            if result:
                self.session_summary = result
                self._pending_compress = [
                    t for t in self._pending_compress if t not in turns
                ]

    def _do_compact(self) -> None:
        """二次压缩：对超长的 session_summary 做全量 LLM 压缩。"""
        with self._summary_lock:
            current = self.session_summary
            if len(current) <= self.MAX_SESSION_SUMMARY_CHARS:
                return  # 已被之前的 compact 任务处理过

            compressed = self._llm_compress(current)
            if compressed and compressed != current:
                self.session_summary = compressed

    def _llm_compress(self, prompt: str) -> str:
        """通过 summarizer 调用 LLM 压缩文本（全量二次压缩，安全兜底用）。"""
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