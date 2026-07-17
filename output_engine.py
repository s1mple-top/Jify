# -*- coding: utf-8 -*-
"""
Jify Output Engine — 终端渲染、状态行、动画线程、输出缓冲。
"""

import json
import math
import random
import re
import threading
import time
from typing import Dict, Optional

from rich.console import Console as RichConsole
from rich.live import Live
from rich.markdown import Markdown
from rich.table import Table as RichTable
from rich import box as rich_box
from rich.text import Text
from rich.theme import Theme
from pygments.styles.monokai import MonokaiStyle
from pygments.styles.dracula import DraculaStyle

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
_TABLE_SEP_RE = re.compile(r'^\s*\|[-\s:]+\|')
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F700-\U0001F77F"  # alchemical symbols
    "\U0001F780-\U0001F7FF"  # geometric shapes extended
    "\U0001F800-\U0001F8FF"  # supplemental arrows-C
    "\U0001F900-\U0001F9FF"  # supplemental symbols
    "\U0001FA00-\U0001FA6F"  # chess symbols
    "\U0001FA70-\U0001FAFF"  # symbols extended-A
    "\U00002600-\U000027BF"  # misc symbols / dingbats
    "\U00002B50"             # star
    "\U00002300-\U000023FF"  # misc technical
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0000200D"             # ZWJ
    "\U000020E3"             # variation selector
    "]+",
    re.UNICODE,
)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)


class JifyTheme:
    ACCENT = "#d4a373"
    SUBTLE = "#585b70"
    GREEN = "#a6e3a1"
    YELLOW = "#f9e2af"
    RED = "#f38ba8"

    class CodeMonokai(MonokaiStyle):
        background_color = None

    class CodeDracula(DraculaStyle):
        background_color = None

    code = CodeMonokai

    MARKDOWN = Theme({
        "markdown.h1": "bold italic",
        "markdown.h2": "bold italic",
        "markdown.h3": "bold italic",
        "markdown.block_quote": "italic default",
        "markdown.code": "italic default",
        "markdown.strong": "bold default",
        "markdown.emphasis": "italic default",
        "markdown.item.bullet": "default",
        "markdown.item.number": "default",
        "markdown.list": "default",
        "markdown.paragraph": "default",
        "markdown.text": "default",
        "markdown.table": "default",
        "markdown.table.header": "bold default",
        "markdown.table.border": "default",
    })

    @classmethod
    def create_console(cls) -> RichConsole:
        return RichConsole(theme=cls.MARKDOWN)

    @classmethod
    def markdown(cls, text: str, code_theme=None) -> Markdown:
        return Markdown(text, code_theme=code_theme or cls.code)


class OutputEngine:
    THINK_PHRASES = [
        "Happy EveryDay", "Do a Nice Job", "Keep It Real",
        "Stay Curious", "Think Deeply", "Code with Joy",
        "Make It Happen", "Dream Big", "Stay Hungry",
        "Shine Bright", "Level Up", "Build Something Great",
        "One Step at a Time", "Enjoy the Process", "Focus & Flow",
    ]

    _WAVE_COLORS: tuple = tuple(
        f"italic #{int(0xf5 + (0xff - 0xf5) * i / 15):02x}"
                 f"{int(0xc2 + (0xff - 0xc2) * i / 15):02x}"
                 f"{int(0xe7 + (0xff - 0xe7) * i / 15):02x}"
        for i in range(16)
    )

    TIPS = [
        "Use /help to see all available commands",
        "Use /skill to check and install skills",
        "Use /model <name> to switch language models",
        "Use /sessions to list recent conversations",
        "Use /resume <id> to continue a past session",
        "Use /clear to reset conversation history",
        "Use /jify to analyze the current project",
        "Use /exit to quit Jify",
    ]

    _TOOL_DISPLAY_KEY = {
        "read_file": "path",
        "write_file": "path",
        "patch_file": "path",
        "exec": "command",
        "static_analysis": "path",
        "load_skill": "skill_name",
        "p2p_send": "peer_name",
    }

    _MD_CACHE_MAX = 50

    def __init__(self):
        self._console = JifyTheme.create_console()
        self._live: Optional[Live] = None
        self._live_active: bool = False
        self._stream_buffer: str = ""
        self._phrase: str = ""
        self._session_start_time: float = 0.0

        self._anim_thread: Optional[threading.Thread] = None
        self._anim_stop = threading.Event()
        self._anim_lock = threading.Lock()
        self._anim_sent: int = 0
        self._anim_target: int = 0
        self._anim_recv: int = 0
        self._anim_recv_target: int = 0

        # subagent 独立的 token 动画变量，复用主 Agent 同款指数平滑
        self._anim_subagent_sent: int = 0
        self._anim_subagent_sent_target: int = 0
        self._anim_subagent_recv: int = 0
        self._anim_subagent_recv_target: int = 0

        self._model_phase: str = "idle"
        self._workflow_status: Optional[Dict] = None
        self._team_workers: Dict[str, Dict] = {}
        self._todos: Optional[list] = None
        self._subagent: Optional[Dict] = None

        self._tip_index: int = 0
        self._tip_last_tick: float = 0.0

        self._think_buffer: str = ""
        self._think_header_emitted: bool = False

        self._md_cache: Dict[str, Markdown] = {}

        self._input_active = threading.Event()  # 初始 cleared = 非活跃
        self._pending_output: list = []

        # self._tool_running: bool = False
        # self._tool_done: bool = False

    # ── Properties ──
    @property
    def stream_buffer(self) -> str:
        return self._stream_buffer

    @stream_buffer.setter
    def stream_buffer(self, value: str):
        self._stream_buffer = value

    @property
    def model_phase(self) -> str:
        return self._model_phase

    @model_phase.setter
    def model_phase(self, value: str):
        self._model_phase = value

    def set_team_worker(self, worker_id: str, info: Dict) -> None:
        """更新单个 Worker 的实时状态 (供 Worker 线程调用)"""
        with self._anim_lock:
            self._team_workers[worker_id] = info

    def clear_team_workers(self) -> None:
        with self._anim_lock:
            self._team_workers.clear()

    def set_subagent(self, info: Dict) -> None:
        """状态栏里更新 subagent 实时状态"""
        with self._anim_lock:
            self._subagent = info
            # 同步更新 token 动画目标值
            if "sent_est" in info:
                self._anim_subagent_sent_target = info["sent_est"]
            if "recv_est" in info:
                self._anim_subagent_recv_target = info["recv_est"]

    def clear_subagent(self) -> None:
        with self._anim_lock:
            self._subagent = None

    def set_todos(self, todos: list) -> None:
        """更新 todos 列表，由动画线程下一帧渲染到状态栏。"""
        with self._anim_lock:
            self._todos = todos

    @property
    def think_buffer(self) -> str:
        return self._think_buffer

    @think_buffer.setter
    def think_buffer(self, value: str):
        self._think_buffer = value

    @property
    def phrase(self) -> str:
        return self._phrase

    @phrase.setter
    def phrase(self, value: str):
        self._phrase = value

    @property
    def session_start_time(self) -> float:
        return self._session_start_time

    @session_start_time.setter
    def session_start_time(self, value: float):
        self._session_start_time = value

    @property
    def console(self) -> RichConsole:
        return self._console

    # ── Formatting ──
    @staticmethod
    def fmt_tokens(n: int) -> str:
        if n < 1000:
            return str(n)
        if n % 1000 == 0:
            return f"{n // 1000}k"
        return f"{n / 1000:.1f}k"

    @staticmethod
    def fmt_elapsed(elapsed: float) -> str:
        secs = int(elapsed)
        if secs < 60:
            return f"{secs}s"
        mins = secs // 60
        remain = secs % 60
        return f"{mins}min {remain}s"

    @classmethod
    def wave_text(cls, text: str, offset: float) -> Text:
        result = Text()
        n = len(text)
        colors = cls._WAVE_COLORS
        for i, ch in enumerate(text):
            factor = (math.sin(i / n * 2 * math.pi - offset) + 1) / 2
            idx = min(int(factor * 15), 15)
            result.append(ch, style=colors[idx])
        return result

    def status_line(self, phrase: str, elapsed: float, sent: int, recv: int,
                    wave_offset: float = 0.0, model_phase: str = "idle",
                    workflow_status: Optional[Dict] = None,
                    team_workers: Optional[Dict] = None,
                    todos: Optional[list] = None,
                    subagent: Optional[Dict] = None,
                    tip: str = "") -> Text:
        t = Text("")

        t.append("✦ ", style=f"italic {JifyTheme.SUBTLE}")
        if wave_offset:
            t.append(self.wave_text(phrase, wave_offset))
        else:
            t.append(phrase, style=f"italic {JifyTheme.SUBTLE}")
        t.append(f"  ({self.fmt_elapsed(elapsed)}", style=JifyTheme.SUBTLE)

        # 早期设计缺陷，暂时预估 token, subagent 的 token 消耗也暂时预估
        if sent > 0:
            t.append(f" · ↑ {self.fmt_tokens(sent // 2)} tokens", style=JifyTheme.YELLOW)
        if recv > 0:
            t.append(f" · ↓ {self.fmt_tokens(recv // 2)} tokens", style=JifyTheme.GREEN)
        t.append(" · ctrl+c to interrupt", style=JifyTheme.SUBTLE)

        phase_text = {
            "thinking": "think...",
            "replying": "generate answers...",
            "compressing": "⏳ 正在整合记忆...",
        }.get(model_phase, "offline")

        t.append(f" · {phase_text}", style=JifyTheme.SUBTLE)
        # if tool_running:
        #     show_dot = int(wave_offset * 2) % 2 == 0
        # elif tool_done:
        #     show_dot = True
        # else:
        #     show_dot = False
        # if show_dot:
        #     t.append(" ●", style="bold yellow")
        t.append(")", style=JifyTheme.SUBTLE)

        if workflow_status:
            wf = workflow_status
            parts = []
            for s in wf.get("steps", []):
                symbol = {"running": "⏳", "done": "✓", "error": "✗"}.get(s.get("state", ""), "○")
                parts.append(f"{s['id']} {symbol}")
            if parts:
                t.append("\n")
                t.append(f"  ⚙ {wf['name']} › {' · '.join(parts)}", style=JifyTheme.SUBTLE)

        if team_workers:
            now = time.time()
            for wid, info in sorted(team_workers.items()):
                task = info.get("task", "")
                if len(task) > 42:
                    task = task[:39] + "…"
                _start = info.get("_start")
                if _start is not None:
                    elapsed = now - _start
                else:
                    elapsed = info.get("elapsed", 0)
                tool_uses = info.get("tool_uses", 0)
                status = info.get("status", "running")
                symbol = {"pending": "○", "running": "⏳", "completed": "✓", "failed": "✗"}.get(status, "⏳")
                t.append("\n")
                t.append(f'  ⚙ {wid}(task="{task}")  ({symbol} {self.fmt_elapsed(elapsed)} · {tool_uses} tools)', style=JifyTheme.SUBTLE)

        if subagent:
            task = subagent.get("task", "")
            if len(task) > 42:
                task = task[:39] + "…"
            _start = subagent.get("_start")
            if _start is not None:
                sa_elapsed = time.time() - _start
            else:
                sa_elapsed = subagent.get("elapsed", 0)
            tool_uses = subagent.get("tool_uses", 0)
            status = subagent.get("status", "running")
            sent_est = subagent.get("sent_est", 0)
            recv_est = subagent.get("recv_est", 0)
            symbol = {"pending": "○", "running": "⏳", "completed": "✓", "failed": "✗"}.get(status, "⏳")
            token_info = ""
            if sent_est:
                # 初期的架构设计缺陷，暂时使用预估token计数
                token_info += f" · ↑ {self.fmt_tokens(sent_est // 2)} tokens"
            if recv_est:
                token_info += f" · ↓ {self.fmt_tokens(recv_est // 2)} tokens"
            t.append("\n")
            t.append(f'  🤖 subagent(task="{task}")  ({symbol} {self.fmt_elapsed(sa_elapsed)} · {tool_uses} tools{token_info})', style=JifyTheme.SUBTLE)

        if todos:
            status_icon = {
                "completed": "☒",
                "in_progress": "◐",
                "pending": "☐",
                "cancelled": "✗",
            }
            status_color = {
                "completed": JifyTheme.SUBTLE,
                "in_progress": JifyTheme.GREEN,
                "pending": JifyTheme.SUBTLE,
                "cancelled": JifyTheme.RED,
            }
            priority_order = {"high": 0, "medium": 1, "low": 2}
            sorted_todos = sorted(todos, key=lambda td: priority_order.get(td.get("priority", "low"), 2))
            for td in sorted_todos:
                s = td.get("status", "pending")
                icon = status_icon.get(s, "☐")
                color = status_color.get(s, JifyTheme.SUBTLE)
                content = td.get("content", "")
                t.append("\n")
                t.append(f"  {icon}  {content}", style=color)

        if tip:
            t.append("\n")
            t.append(f"   ⎿  tips: {tip}", style=JifyTheme.SUBTLE)
            t.append(f"\n")
            t.append(f"\n")

        return t

    # Lifecycle
    def start_round(self) -> None:
        if self._session_start_time == 0.0:
            self._session_start_time = time.time()

        self._phrase = random.choice(self.THINK_PHRASES)
        self._model_phase = "idle"

        if self._live is None or not self._live_active:
            self._live = Live(
                self.status_line(self._phrase, 0, 0, 0),
                console=self._console,
                refresh_per_second=60,
                transient=True,
                vertical_overflow="visible",
            )
            self._live.start()
            self._live_active = True

        self._start_animation()

    def stop_live(self) -> None:
        self._stop_animation()
        with self._anim_lock:
            if self._live and self._live_active:
                self._live.update("")
                self._live.stop()
                self._live = None
                self._live_active = False

    def restart_live(self) -> None:
        self._live = Live(
            self.status_line(self._phrase, 0, 0, 0),
            console=self._console,
            refresh_per_second=60,
            transient=True,
            vertical_overflow="visible",
        )
        self._live.start()
        self._live_active = True
        self._start_animation()

    def finalize(self) -> None:
        self.stop_live()
        self._flush_pending()
        import sys
        self._console.file.flush()
        sys.stdout.flush()
        # _input_active 由 main_loop 唯一控制，finalize 不再越权重置
        self._stream_buffer = ""
        self._session_start_time = 0.0
        self._anim_sent = 0
        self._anim_target = 0
        self._anim_recv = 0
        self._anim_recv_target = 0
        self._anim_subagent_sent = 0
        self._anim_subagent_sent_target = 0
        self._anim_subagent_recv = 0
        self._anim_subagent_recv_target = 0
        self._workflow_status = None
        self._team_workers.clear()
        self._todos = None
        self._tip_index = 0
        self._tip_last_tick = 0.0
        self._think_buffer = ""
        self._think_header_emitted = False
        # self._tool_running = False
        # self._tool_done = False

    # def stream_end(self) -> None:
    #     pass
    #
    # def Nanswer(self) -> None:
    #     pass

    # Animation
    def _start_animation(self) -> None:
        if self._anim_thread is not None and self._anim_thread.is_alive():
            return
        self._anim_stop.clear()

        def _loop() -> None:
            last_tick = time.monotonic()
            while not self._anim_stop.is_set():
                now = time.monotonic()
                dt = max(now - last_tick, 0.001)
                last_tick = now

                with self._anim_lock:
                    if self._anim_sent < self._anim_target:
                        gap = self._anim_target - self._anim_sent
                        alpha = 1.0 - math.exp(-dt / 0.2)
                        step = max(1, int(gap * alpha + 0.5))
                        self._anim_sent = min(self._anim_sent + step, self._anim_target)
                    if self._anim_recv < self._anim_recv_target:
                        gap = self._anim_recv_target - self._anim_recv
                        alpha = 1.0 - math.exp(-dt / 0.2)
                        step = max(1, int(gap * alpha + 0.5))
                        self._anim_recv = min(self._anim_recv + step, self._anim_recv_target)

                    # subagent token 平滑动画，复用同款指数逼近
                    if self._anim_subagent_sent < self._anim_subagent_sent_target:
                        gap = self._anim_subagent_sent_target - self._anim_subagent_sent
                        alpha = 1.0 - math.exp(-dt / 0.2)
                        step = max(1, int(gap * alpha + 0.5))
                        self._anim_subagent_sent = min(self._anim_subagent_sent + step, self._anim_subagent_sent_target)
                    if self._anim_subagent_recv < self._anim_subagent_recv_target:
                        gap = self._anim_subagent_recv_target - self._anim_subagent_recv
                        alpha = 1.0 - math.exp(-dt / 0.2)
                        step = max(1, int(gap * alpha + 0.5))
                        self._anim_subagent_recv = min(self._anim_subagent_recv + step, self._anim_subagent_recv_target)

                    elapsed = time.time() - self._session_start_time
                    phrase = self._phrase
                    sent = self._anim_sent
                    recv = self._anim_recv
                    phase = self._model_phase
                    wf_status = self._workflow_status
                    team_snapshot = dict(self._team_workers)
                    todos_snapshot = list(self._todos) if self._todos else None
                    subagent_snapshot = dict(self._subagent) if self._subagent else None
                    if subagent_snapshot:
                        subagent_snapshot["sent_est"] = self._anim_subagent_sent
                        subagent_snapshot["recv_est"] = self._anim_subagent_recv

                    tip_text = ""
                    now_tip = time.monotonic()
                    if now_tip - self._tip_last_tick > 5.0:
                        self._tip_index = (self._tip_index + 1) % len(self.TIPS)
                        self._tip_last_tick = now_tip
                    tip_text = self.TIPS[self._tip_index] if not team_snapshot and not todos_snapshot and not wf_status else ""

                    if self._live is not None and self._live_active:
                        wave = time.monotonic() * 3.0
                        self._live.update(
                            self.status_line(phrase, elapsed, sent, recv, wave, phase,
                                             wf_status, team_snapshot, todos_snapshot,
                                             subagent=subagent_snapshot,
                                             tip=tip_text)
                        )

                self._anim_stop.wait(1 / 60)

        self._anim_thread = threading.Thread(target=_loop, daemon=True)
        self._anim_thread.start()

    def _stop_animation(self) -> None:
        self._anim_stop.set()
        if self._anim_thread is not None:
            self._anim_thread.join(timeout=0.5)
            self._anim_thread = None

    def init_anim_state(self, sent: int, recv: int, sent_target_delta: int) -> None:
        with self._anim_lock:
            self._anim_sent = sent
            self._anim_target = sent + sent_target_delta
            self._anim_recv = recv
            self._anim_recv_target = recv

    def update_anim_target(self, sent: int, recv: int) -> None:
        with self._anim_lock:
            if sent >= 0:
                self._anim_target = sent
            if recv >= 0:
                self._anim_recv_target = recv

    def update_status(self, phrase: str, elapsed: float, sent: int, recv: int) -> None:
        if self._live:
            self._live.update(self.status_line(phrase, elapsed, sent, recv))

    # Output
    def queue_output(self, renderable) -> None:
        if self._input_active.is_set():
            self._pending_output.append(renderable)
            return
        with self._anim_lock:
            if self._live_active and self._live is not None:
                self._live.console.print(renderable)
            else:
                self._console.print(renderable)

    def set_input_active(self, active: bool) -> None:
        self._input_active.set() if active else self._input_active.clear()

    def _flush_pending(self) -> None:
        with self._anim_lock:
            for item in self._pending_output:
                if self._live_active and self._live is not None:
                    self._live.console.print(item)
                else:
                    self._console.print(item)
            self._pending_output.clear()

    # Markdown
    def cached_markdown(self, text: str) -> Markdown:
        if text not in self._md_cache:
            if len(self._md_cache) >= self._MD_CACHE_MAX:
                self._md_cache.pop(next(iter(self._md_cache)))
            self._md_cache[text] = Markdown(text, code_theme=JifyTheme.CodeMonokai)
        return self._md_cache[text]

    def output_markdown(self, text: str) -> None:
        text = _strip_ansi(text)
        if self._has_table(text):
            for renderable in self._render_table_segments(text):
                self.queue_output(renderable)
        else:
            md = self.cached_markdown(text)
            self.queue_output(md)

    # Table rendering
    @staticmethod
    def _has_table(text: str) -> bool:
        for line in text.split('\n'):
            if _TABLE_SEP_RE.match(line):
                return True
        return False

    @staticmethod
    def _parse_table_row(line: str) -> list:
        line = line.strip()
        if line.startswith('|'):
            line = line[1:]
        if line.endswith('|'):
            line = line[:-1]
        return [c for c in line.split('|')]

    @staticmethod
    def _markdown_cell_to_text(cell: str) -> Text:
        """将单元格内的 Markdown 内联格式转为 Rich Text 对象。"""
        # 剥离 emoji，避免 Rich Table 列宽计算偏差导致竖线错位
        text = _EMOJI_RE.sub('', cell).strip()
        text = re.sub(r'\*\*(.+?)\*\*', r'[bold]\1[/bold]', text)
        text = re.sub(r'\*(.+?)\*', r'[italic]\1[/italic]', text)
        text = re.sub(r'`(.+?)`', r'[italic]\1[/italic]', text)
        return Text.from_markup(text)

    def _build_rich_table(self, table_text: str) -> RichTable:
        lines = table_text.strip().split('\n')
        if len(lines) < 2:
            return RichTable()

        header_cells = self._parse_table_row(lines[0])
        sep_cells = self._parse_table_row(lines[1])

        alignments = []
        for cell in sep_cells:
            cell = cell.strip()
            if cell.startswith(':') and cell.endswith(':'):
                alignments.append('center')
            elif cell.endswith(':'):
                alignments.append('right')
            else:
                alignments.append('left')

        data_rows = []
        for line in lines[2:]:
            row = self._parse_table_row(line)
            if row:
                data_rows.append(row)

        table = RichTable(box=rich_box.HEAVY)
        for header, align in zip(header_cells, alignments):
            table.add_column(
                Text(header.strip(), justify=align),
                overflow="fold",
                no_wrap=False,
                min_width=4,
                justify=align,
            )

        n_cols = len(header_cells)
        for row in data_rows:
            while len(row) < n_cols:
                row.append('')
            table.add_row(*[self._markdown_cell_to_text(cell.strip()) for cell in row[:n_cols]])

        return table

    def _render_table_segments(self, text: str) -> list:
        lines = text.split('\n')
        i = 0
        segments = []
        non_table_lines = []

        while i < len(lines):
            if _TABLE_SEP_RE.match(lines[i]):
                # _has_table 的 header 行必须包含 | 才算有效表格头，
                # 否则它只是一个恰好出现在分隔行上方的普通行（如 Markdown 标题）
                header_line = ''
                if non_table_lines:
                    candidate = non_table_lines[-1].strip()
                    if candidate.startswith('|'):
                        header_line = non_table_lines.pop()
                if non_table_lines:
                    seg_text = '\n'.join(non_table_lines).strip()
                    if seg_text:
                        segments.append(('md', seg_text))
                    non_table_lines = []

                sep_line = lines[i]
                data_lines = []
                j = i + 1
                while j < len(lines) and lines[j].strip().startswith('|'):
                    data_lines.append(lines[j])
                    j += 1

                if header_line:
                    table_text = '\n'.join([header_line, sep_line] + data_lines)
                    segments.append(('table', table_text))
                else:
                    # 无有效表头则不视为表格，全部归入 Markdown
                    non_table_lines.append(sep_line)
                    non_table_lines.extend(data_lines)
                i = j
            else:
                non_table_lines.append(lines[i])
                i += 1

        if non_table_lines:
            seg_text = '\n'.join(non_table_lines).strip()
            if seg_text:
                segments.append(('md', seg_text))

        renderables = []
        for seg_type, content in segments:
            if seg_type == 'md':
                renderables.append(self.cached_markdown(content))
            else:
                table = self._build_rich_table(content)
                if table:
                    renderables.append(table)

        return renderables

    def flush_stream_buffer(self, is_final: bool = False) -> None:
        if self._stream_buffer.strip():
            if is_final:
                self.queue_output(Text(""))
            self.output_markdown(self._stream_buffer)
            self._stream_buffer = ""

    # Diff
    def output_diff(self, content: str) -> None:
        lines = content.splitlines()
        rendered = Text()
        for i, line in enumerate(lines):
            if i > 0:
                rendered.append("\n")
            if line.startswith('--- a/') or line.startswith('+++ b/'):
                rendered.append(line, style="bold cyan")
            elif line.startswith('@@'):
                rendered.append(line, style="cyan")
            elif line.startswith('-'):
                rendered.append(line, style="on #3d1515")
            elif line.startswith('+'):
                rendered.append(line, style="on #1b4f1b")
            else:
                rendered.append(line, style="dim")
        self.queue_output(rendered)

    # # Todos 走状态栏
    # def output_todos(self, todos: list) -> None:
    #     status_icon = {
    #         "completed": ("☒", JifyTheme.SUBTLE),
    #         "in_progress": ("◐", JifyTheme.GREEN),
    #         "pending": ("☐", JifyTheme.SUBTLE),
    #         "cancelled": ("✗", JifyTheme.RED),
    #     }
    #     text_color = {
    #         "completed": JifyTheme.SUBTLE,
    #         "in_progress": JifyTheme.GREEN,
    #         "pending": "white",
    #         "cancelled": JifyTheme.RED,
    #     }
    #     priority_order = {"high": 0, "medium": 1, "low": 2}
    #     sorted_todos = sorted(todos, key=lambda t: priority_order.get(t.get("priority", "low"), 2))
    #
    #     lines = Text()
    #     for i, t in enumerate(sorted_todos):
    #         if i > 0:
    #             lines.append("\n")
    #         status = t.get("status", "pending")
    #         icon, color = status_icon.get(status, ("☐", JifyTheme.SUBTLE))
    #         lines.append(f"  {icon}  ", style=color)
    #         lines.append(t.get("content", ""), style=text_color.get(status, "white"))
    #     self.queue_output(lines)

    # Thinking
    def reset_thinking(self) -> None:
        self._think_buffer = ""
        self._think_header_emitted = False

    def output_thinking(self, flush: bool = False) -> None:
        if not self._think_buffer:
            return
        if not self._think_header_emitted:
            self.queue_output(Text(""))
            self.queue_output(Text("✻ Thinking…", style=f"bold italic {JifyTheme.SUBTLE}"))
            self._think_header_emitted = True
        if flush:
            self.queue_output(Text(self._think_buffer, style=JifyTheme.SUBTLE))
            self._think_buffer = ""

    # Workflow
    def process_workflow_event(self, data: Dict) -> None:
        event_type = data.get("event", "")
        if event_type == "init":
            self._workflow_status = {
                "name": data["workflow_name"],
                "steps": [
                    {"id": s["id"], "state": s.get("state", "pending"), "tool": s.get("tool", "")}
                    for s in data.get("steps", [])
                ],
            }
        elif event_type == "step":
            if self._workflow_status:
                for s in self._workflow_status["steps"]:
                    if s["id"] == data.get("step_id"):
                        s["state"] = data.get("state", "pending")
                        break
        elif event_type == "done":
            self._workflow_status = None

    # Tool call formatting
    @classmethod
    def format_tool_call(cls, name: str, args: str) -> tuple:
        try:
            parsed = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            return (f"• {name}", None)

        if name == "read_file":
            path = parsed.get("path", "")
            if isinstance(path, str):
                display_path = path.rsplit("/", 1)[-1] if path else "…"
                return (f"• Read({display_path})", None)

        if name == "exec":
            command = parsed.get("command", "")
            if isinstance(command, str):
                escaped = command.replace("\n", "\\n")
                if len(escaped) > 120:
                    escaped = escaped[:117] + "…"
                return (f"• {name}", f'    ⎿ "{escaped}"')

        name_line = f"• {name}"

        key = cls._TOOL_DISPLAY_KEY.get(name)
        if key and key in parsed:
            value = parsed[key]
            if isinstance(value, str):
                if len(value) > 80:
                    value = "…" + value[-77:]
                detail = f'    ⎿ "{value}"'
            else:
                detail = f"    ⎿ {value}"
            return (name_line, detail)

        for v in parsed.values():
            if isinstance(v, str) and v:
                preview = v[:80] + ("…" if len(v) > 80 else "")
                return (name_line, f'    ⎿ "{preview}"')
        return (name_line, None)
    #
    # # Tool line output
    # def output_line(self, text: str, style: str = "") -> None:
    #     self.queue_output(Text(text, style=style if style else None))
