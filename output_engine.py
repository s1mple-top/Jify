# -*- coding: utf-8 -*-
"""
Jify Output Engine — 终端渲染、状态行、动画线程、输出缓冲。
"""

import json
import math
import random
import re
import threading
import termios
import time
from typing import Any, Dict, Optional

from rich.console import Console as RichConsole, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.table import Table as RichTable
from rich import box as rich_box
from rich.markup import escape
from rich.text import Text
from rich.cells import cell_len
from rich.theme import Theme
from pygments.styles.monokai import MonokaiStyle
from pygments.styles.dracula import DraculaStyle

# Team 状态栏框线：仅保留列间竖线（│），无顶部/底部横线、无左右边框，
# 用于把多个 worker 的实时细节在状态栏里竖线分割并列展示。
_TEAM_BOX = rich_box.Box(
    "    \n"  # top
    "  │ \n"  # head
    "    \n"  # head_row
    "  │ \n"  # mid
    "  │ \n"  # row
    "  │ \n"  # foot_row
    "  │ \n"  # foot
    "    \n"  # bottom
)

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
_TABLE_SEP_RE = re.compile(r'^\s*\|[-\s:]+\|')
# 代码围栏起始行：``` 或 ~~~（允许带语言标识和缩进）
_FENCE_RE = re.compile(r'^\s*(`{3,}|~{3,})')
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


# 全局终端模式锁：保护所有 tcgetattr/tcsetattr 操作。
# 审批流程(approval)与 prompt_toolkit、动画线程都可能切换终端模式，
# 统一用这把锁串行化，防止并发切换导致 termios 状态错乱（输入失效）。
termios_lock = threading.Lock()


def restore_terminal_sane() -> None:
    """把 stdin 恢复到规范的 canonical + echo 状态，作为 prompt_toolkit 进入前的兜底。

    exec 子进程或审批流程若遗留 raw / -echo 等异常终端状态，会导致下一次
    read_input() 无法回显或无法输入；此函数在每次进入输入前强制纠正。
    非 tty 环境（如管道、重定向）直接跳过，避免无谓报错。
    """
    import sys
    try:
        fd = sys.stdin.fileno()
        if not sys.stdin.isatty():
            return
        with termios_lock:
            attr = termios.tcgetattr(fd)
            attr[0] |= termios.ICRNL                    # iflag: CR -> NL
            attr[3] |= termios.ECHO | termios.ICANON    # lflag: 规范模式 + 回显
            termios.tcsetattr(fd, termios.TCSADRAIN, attr)
    except (termios.error, OSError, ValueError):
        pass


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
    # 最终回答前的固定长度分隔线（长度固定，不随终端宽度变化）
    SEPARATOR = "---"

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

        # subagent token 动画：按实例 key 各存一组 (cur_sent, target_sent, cur_recv, target_recv)
        self._anim_sa: Dict[str, Dict[str, int]] = {}

        self._model_phase: str = "idle"
        self._workflow_status: Optional[Dict] = None
        self._team_workers: Dict[str, Dict] = {}
        self._todos: Optional[list] = None
        # 并发 subagent 可能同时运行，按 key（线程 ident）分槽位，避免互相覆盖
        self._subagents: Dict[str, Dict] = {}

        self._tip_index: int = 0
        self._tip_last_tick: float = 0.0

        self._think_buffer: str = ""
        self._think_header_emitted: bool = False
        self._show_thinking: bool = True

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

    def clear_team_worker(self, worker_id: str) -> None:
        """清理单个 Worker 的状态槽位。

        并行多个 team_delegate 时，各自结束只应清掉自己；全清会误删
        仍在跑的其他 Worker 状态行（在跑者要等下一个进度事件才被写回，
        表现为状态行数量反复跳变）。
        """
        with self._anim_lock:
            self._team_workers.pop(worker_id, None)

    def set_subagent(self, key: str, info: Dict) -> None:
        """状态栏里更新某个 subagent 的实时状态（key 用线程 ident 区分并发实例）"""
        with self._anim_lock:
            self._subagents[key] = info
            # 同步更新该实例的 token 动画目标值
            anim = self._anim_sa.get(key)
            if anim is None:
                anim = {"cur_sent": 0, "target_sent": 0, "cur_recv": 0, "target_recv": 0}
                self._anim_sa[key] = anim
            if "sent_est" in info:
                anim["target_sent"] = info["sent_est"]
            if "recv_est" in info:
                anim["target_recv"] = info["recv_est"]

    def clear_subagent(self, key: Optional[str] = None) -> None:
        """清理 subagent 槽位：传 key 只清该实例，不传则清全部"""
        with self._anim_lock:
            if key is None:
                self._subagents.clear()
                self._anim_sa.clear()
            else:
                self._subagents.pop(key, None)
                self._anim_sa.pop(key, None)

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
    def show_thinking(self) -> bool:
        return self._show_thinking

    @show_thinking.setter
    def show_thinking(self, value: bool):
        self._show_thinking = value
        if not value:
            self._think_buffer = ""

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
                    subagents: Optional[Dict[str, Dict]] = None,
                    tip: str = "") -> Any:
        t = Text("")
        # 状态栏所有行禁止折行：帧高必须只由条目数决定。超宽行一旦折行，
        # 帧高可能超过终端高度，Live 重绘时光标上移越界 → 整屏滚动爆闪。
        t.no_wrap = True
        t.overflow = "crop"
        team_table = None
        # team worker 统一活动流展示行数上限：think / 正文 / tool 共用同一预算
        _MAX_STREAM_LINES = 10
        # 行数预算：帧高封顶在终端高度内（留 1 行余量，块高恰好 == 终端高时
        # 光标落在末行，边界上仍可能触发一次滚动）
        _max_lines = max(6, self.console.height - 1)
        _wf_lines = 0

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
            "compressing": "compressing...",
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
                _wf_lines = 1
                t.append("\n")
                t.append(f"  ⚙ {wf['name']} › {' · '.join(parts)}", style=JifyTheme.SUBTLE)

        if team_workers:
            now = time.time()
            team_table = RichTable(box=_TEAM_BOX, show_header=False, padding=(0, 1), expand=False)
            cells = []
            # 多 worker 并排时每列只有终端宽度的 1/N，think 行必须按列宽裁剪，
            # 否则按整行宽度裁出来的内容在列内会被 fold 成多行，状态栏随之抖动。
            _n_cols = max(1, len(team_workers))
            _col_avail = max(16, (self.console.width - 3 * _n_cols) // _n_cols - 2)
            for wid, info in sorted(team_workers.items()):
                task = info.get("task", "")
                # 头部行 "⏳ wid  task" 也要按列宽截断，否则同样会被折行
                task_avail = max(6, _col_avail - 6 - cell_len(wid))
                if cell_len(task) > task_avail:
                    kept, used = [], 0
                    for ch in task:
                        cw = cell_len(ch)
                        if used + cw > task_avail - 1:
                            break
                        used += cw
                        kept.append(ch)
                    task = "".join(kept) + "…"
                _start = info.get("_start")
                if _start is not None:
                    w_elapsed = now - _start
                else:
                    w_elapsed = info.get("elapsed", 0)
                tool_uses = info.get("tool_uses", 0)
                status = info.get("status", "running")
                symbol = {"pending": "○", "running": "⏳", "completed": "✓", "failed": "✗"}.get(status, "⏳")
                last_tool = info.get("last_tool", "")
                sent_est = info.get("sent_est", 0)
                recv_est = info.get("recv_est", 0)

                cell = Text()
                cell.append(f"{symbol} {wid}", style="bold yellow")
                if task:
                    cell.append(f"  {task}", style=JifyTheme.SUBTLE)
                cell.append("\n", style=JifyTheme.SUBTLE)
                cell.append(f"  {self.fmt_elapsed(w_elapsed)} · {tool_uses} tools", style=JifyTheme.SUBTLE)
                if last_tool:
                    cell.append(f" · {last_tool}", style=JifyTheme.GREEN)

                # think 跑马灯：固定占一行，采集侧已压成单行并只保留尾部。
                # 这里按「本列」可用宽度裁一次（前缀 "  ✻ " 占 4 列），保证不折行
                # （折行会让状态栏抖动）。必须按显示宽度裁（中文占 2 列）。
                # 没有 think 时也保留这一行（填空白），否则状态栏高度会随
                # think 的有无上下跳。
                think_line = (info.get("think") or "").strip()
                if think_line:
                    avail = max(12, _col_avail - 4)
                    if cell_len(think_line) > avail:
                        tail, used = [], 0
                        for ch in reversed(think_line):
                            cw = cell_len(ch)
                            if used + cw > avail - 1:
                                break
                            used += cw
                            tail.append(ch)
                        think_line = "\u2026" + "".join(reversed(tail))
                    cell.append("\n", style=JifyTheme.SUBTLE)
                    cell.append(f"  \u273b {think_line}", style=JifyTheme.SUBTLE)
                else:
                    cell.append("\n", style=JifyTheme.SUBTLE)
                    cell.append(" ", style=JifyTheme.SUBTLE)

                # worker 活动流：只展示工具调用，正文由 worker 结果统一回给主 Agent
                flat = []
                for entry in info.get("stream") or []:
                    if entry.get("kind") != "tool":
                        continue
                    for ln in (entry.get("text") or "").splitlines():
                        if ln.strip():
                            flat.append(ln)
                # tool 行同样要按列宽裁剪：列宽是 auto 且未设 no_wrap，
                # 只靠 overflow="ellipsis" 仍会 wrap，超长内容折到下一行后
                # 缩进丢失，状态栏高度跟着涨。前缀 "  • " 占 4 列。
                for ln in flat[-_MAX_STREAM_LINES:]:
                    avail = max(12, _col_avail - 4)
                    if cell_len(ln) > avail:
                        kept, used = [], 0
                        for ch in ln:
                            cw = cell_len(ch)
                            if used + cw > avail - 1:
                                break
                            used += cw
                            kept.append(ch)
                        ln = "".join(kept) + "…"
                    cell.append("\n", style=JifyTheme.SUBTLE)
                    cell.append(f"  • {ln}", style=JifyTheme.GREEN)
                if sent_est or recv_est:
                    cell.append("\n", style=JifyTheme.SUBTLE)
                    cell.append(f"  ↑{self.fmt_tokens(sent_est // 2)} ↓{self.fmt_tokens(recv_est // 2)}", style=JifyTheme.SUBTLE)
                cells.append(cell)
                # ellipsis 兜底：即使个别内容算宽仍有偏差，也只截断不折行
                team_table.add_column(overflow="ellipsis")
            team_table.add_row(*cells)

        # 行数预算分配：header(1) + workflow 行先扣除，subagent 优先占用剩余
        # 预算（实时活动主体），todos 存在时保底留 1 行；subagent 超出部分
        # 折叠成 1 行摘要，todos 超出截掉低优先级尾部。
        _remaining = _max_lines - 1 - _wf_lines
        _n_sa = len(subagents) if subagents else 0
        _n_todo = len(todos) if todos else 0
        if _n_sa + _n_todo > _remaining:
            _sa_slot = _remaining - 1 if _n_todo else _remaining
            _sa_visible = min(_n_sa, max(0, _sa_slot))
            if _n_sa > _sa_visible:
                # 折叠摘要行本身占 1 行
                _sa_visible = max(0, _sa_slot - 1)
            _todo_visible = min(
                _n_todo,
                max(0, _remaining - _sa_visible - (1 if _n_sa > _sa_visible else 0)),
            )
        else:
            _sa_visible = _n_sa
            _todo_visible = _n_todo

        if subagents:
            now_sa = time.time()
            # 按启动时间排序，先起的排前面
            ordered = sorted(
                subagents.items(),
                key=lambda kv: (kv[1].get("_start") or 0, str(kv[0])),
            )
            for idx, (key, sa) in enumerate(ordered[:_sa_visible]):
                task = sa.get("task", "")
                _start = sa.get("_start")
                if _start is not None:
                    sa_elapsed = now_sa - _start
                else:
                    sa_elapsed = sa.get("elapsed", 0)
                tool_uses = sa.get("tool_uses", 0)
                status = sa.get("status", "running")
                sent_est = sa.get("sent_est", 0)
                recv_est = sa.get("recv_est", 0)
                symbol = {"pending": "○", "running": "⏳", "completed": "✓", "failed": "✗"}.get(status, "⏳")
                seq = sa.get("seq", idx + 1)
                token_info = ""
                if sent_est:
                    # 初期的架构设计缺陷，暂时使用预估token计数
                    token_info += f" · ↑ {self.fmt_tokens(sent_est // 2)} tokens"
                if recv_est:
                    token_info += f" · ↓ {self.fmt_tokens(recv_est // 2)} tokens"
                # 行整体是 no_wrap+crop，超宽会从行尾裁掉统计信息，因此先拼
                # 后缀量出宽度，再用剩余宽度按「显示宽度」截 task（中文占 2 列），
                # 保证时长/工具数/token 统计不被裁。80 列窄终端下 head+tail
                # 本身可能就占满整行，此时逐级降级：先丢 token 统计再丢工具数。
                _head = f'  🤖 subagent#{seq}(task="'
                _seg = f"{symbol} {self.fmt_elapsed(sa_elapsed)}"
                if tool_uses:
                    _seg += f" · {tool_uses} tools"
                if token_info:
                    _seg += token_info
                _task_avail = self.console.width - cell_len(_head) - cell_len(f'")  ({_seg})')
                if _task_avail < 8 and token_info:
                    _seg = f"{symbol} {self.fmt_elapsed(sa_elapsed)}"
                    if tool_uses:
                        _seg += f" · {tool_uses} tools"
                    _task_avail = self.console.width - cell_len(_head) - cell_len(f'")  ({_seg})')
                if _task_avail < 6 and tool_uses:
                    _seg = f"{symbol} {self.fmt_elapsed(sa_elapsed)}"
                    _task_avail = self.console.width - cell_len(_head) - cell_len(f'")  ({_seg})')
                _tail = f'")  ({_seg})'
                if cell_len(task) > _task_avail:
                    kept, used = [], 0
                    for ch in task:
                        cw = cell_len(ch)
                        if used + cw > _task_avail - 1:
                            break
                        used += cw
                        kept.append(ch)
                    task = "".join(kept) + "…" if _task_avail >= 1 else ""
                t.append("\n")
                t.append(f"{_head}{task}{_tail}", style=JifyTheme.SUBTLE)
            if _n_sa > _sa_visible:
                t.append("\n")
                t.append(f"  🤖 … +{_n_sa - _sa_visible} more", style=JifyTheme.SUBTLE)

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
            # 超出预算截掉低优先级尾部（排序后 low 在尾部）
            for td in sorted_todos[:_todo_visible]:
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

        if team_table is not None:
            return Group(t, team_table)
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
        # 与 stop_live / 动画线程共用 _anim_lock，串行化对 _live 的替换，
        # 防止审批恢复动画与中断清理并发时出现两个 Live 并存导致终端状态错乱。
        with self._anim_lock:
            if self._live and self._live_active:
                self._live.update("")
                self._live.stop()
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
        try:
            termios.tcdrain(sys.stdout.fileno())
        except termios.error:
            pass
        # 中断/结束统一恢复终端到 sane 状态，防止 exec 子进程或审批遗留 raw/-echo
        restore_terminal_sane()
        # _input_active 由 main_loop 唯一控制，finalize 不再越权重置
        self._stream_buffer = ""
        self._session_start_time = 0.0
        self._anim_sent = 0
        self._anim_target = 0
        self._anim_recv = 0
        self._anim_recv_target = 0
        self._anim_sa.clear()
        self._workflow_status = None
        self._team_workers.clear()
        self._subagents.clear()
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

                    # subagent token 平滑动画，每个实例各自指数逼近
                    for _sa_anim in self._anim_sa.values():
                        alpha = 1.0 - math.exp(-dt / 0.2)
                        if _sa_anim["cur_sent"] < _sa_anim["target_sent"]:
                            gap = _sa_anim["target_sent"] - _sa_anim["cur_sent"]
                            step = max(1, int(gap * alpha + 0.5))
                            _sa_anim["cur_sent"] = min(_sa_anim["cur_sent"] + step, _sa_anim["target_sent"])
                        if _sa_anim["cur_recv"] < _sa_anim["target_recv"]:
                            gap = _sa_anim["target_recv"] - _sa_anim["cur_recv"]
                            step = max(1, int(gap * alpha + 0.5))
                            _sa_anim["cur_recv"] = min(_sa_anim["cur_recv"] + step, _sa_anim["target_recv"])

                    elapsed = time.time() - self._session_start_time
                    phrase = self._phrase
                    sent = self._anim_sent
                    recv = self._anim_recv
                    phase = self._model_phase
                    wf_status = self._workflow_status
                    team_snapshot = dict(self._team_workers)
                    todos_snapshot = list(self._todos) if self._todos else None
                    subagent_snapshot = {k: dict(v) for k, v in self._subagents.items()}
                    for _key, _snap in subagent_snapshot.items():
                        _anim = self._anim_sa.get(_key)
                        if _anim is not None:
                            _snap["sent_est"] = _anim["cur_sent"]
                            _snap["recv_est"] = _anim["cur_recv"]

                    tip_text = ""
                    now_tip = time.monotonic()
                    if now_tip - self._tip_last_tick > 5.0:
                        self._tip_index = (self._tip_index + 1) % len(self.TIPS)
                        self._tip_last_tick = now_tip
                    tip_text = self.TIPS[self._tip_index] if not team_snapshot and not todos_snapshot and not wf_status and not subagent_snapshot else ""

                    if self._live is not None and self._live_active:
                        wave = time.monotonic() * 3.0
                        self._live.update(
                            self.status_line(phrase, elapsed, sent, recv, wave, phase,
                                             wf_status, team_snapshot, todos_snapshot,
                                             subagents=subagent_snapshot or None,
                                             tip=tip_text)
                        )

                self._anim_stop.wait(1 / 60)

        self._anim_thread = threading.Thread(target=_loop, daemon=True)
        self._anim_thread.start()

    def _stop_animation(self) -> None:
        if self._anim_thread is None:
            return
        self._anim_stop.set()
        with self._anim_lock:
            pass
        self._anim_thread.join(timeout=1.0)
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

    def prepare_for_input(self) -> None:
        # prompt_toolkit 进入前先恢复终端到 sane 状态，兜底修复上次交互遗留的
        # raw / -echo 等异常模式，确保本次输入可正常回显与读取。
        restore_terminal_sane()
        with self._anim_lock:
            import sys
            sys.stdout.write("\033[?25h")
            sys.stdout.flush()

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
        # 结尾的 | 只有在前面是偶数个反斜杠时才是列分隔符（\| 是字面竖线）
        if line.endswith('|'):
            run = 0
            k = len(line) - 2
            while k >= 0 and line[k] == '\\':
                run += 1
                k -= 1
            if run % 2 == 0:
                line = line[:-1]
        # 按未转义的 | 切列：统计 | 前连续反斜杠的个数，奇数表示该竖线被转义，
        # 偶数表示它是分隔符；反斜杠本身按 Markdown 规则成对折半还原。
        cells = []
        buf = []
        i = 0
        n = len(line)
        while i < n:
            ch = line[i]
            if ch == '\\':
                j = i
                while j < n and line[j] == '\\':
                    j += 1
                run = j - i
                if j < n and line[j] == '|':
                    buf.append('\\' * (run // 2))
                    if run % 2 == 0:
                        cells.append(''.join(buf))
                        buf = []
                    else:
                        buf.append('|')
                    i = j + 1
                else:
                    buf.append('\\' * run)
                    i = j
            elif ch == '|':
                cells.append(''.join(buf))
                buf = []
                i += 1
            else:
                buf.append(ch)
                i += 1
        cells.append(''.join(buf))
        return cells

    @staticmethod
    def _markdown_cell_to_text(cell: str) -> Text:
        """将单元格内的 Markdown 内联格式转为 Rich Text 对象。"""
        # 剥离 emoji，避免 Rich Table 列宽计算偏差导致竖线错位
        text = _EMOJI_RE.sub('', cell).strip()
        raw = text
        # 先转义原文，避免单元格里的 [xxx] / [/xxx] 被当成 rich 标签解析
        # （如 [/Users/.../ctf-web] 会抛 MarkupError 打死整轮对话）；
        # escape 必须在下面的标签替换之前，否则自己插入的 [bold] 也会被转义。
        text = escape(text)
        # rich.escape() 会给奇数个结尾反斜杠补一个，防止它与后续拼接的标签组成
        # 转义序列。这里之后不再拼接 markup，补位会显示成多出来的反斜杠
        # （C:\path\ -> C:\path\\），所以把补的那一个去掉。
        if raw.endswith('\\') and not raw.endswith('\\\\') and text.endswith('\\\\'):
            text = text[:-1]
        text = re.sub(r'\*\*(.+?)\*\*', r'[bold]\1[/bold]', text)
        text = re.sub(r'\*(.+?)\*', r'[italic]\1[/italic]', text)
        text = re.sub(r'`(.+?)`', r'[italic]\1[/italic]', text)
        return Text.from_markup(text, emoji=False)

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
        # 代码围栏（``` / ~~~）内部的内容必须原样交给 Markdown 渲染，
        # 否则代码块里出现的表格示例会被当成真表格抽出来，围栏也被拆两半。
        fence = ''

        while i < len(lines):
            line = lines[i]
            fence_match = _FENCE_RE.match(line)
            if fence_match:
                marker = fence_match.group(1)
                if not fence:
                    # 记录完整标记长度：```` 包裹 ``` 时，内部的 ``` 不该结束外层围栏
                    fence = marker
                elif marker[0] == fence[0] and len(marker) >= len(fence):
                    fence = ''
                non_table_lines.append(line)
                i += 1
            elif fence or not _TABLE_SEP_RE.match(line):
                non_table_lines.append(line)
                i += 1
            else:
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
                # 固定长度分隔线：用 Text 直出，避免交给 Markdown 解析成整行 hr
                self.queue_output(Text(self.SEPARATOR, style="white"))
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
        if not self._show_thinking:
            self._think_buffer = ""
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

        if name == "code_sandbox":
            lang = parsed.get("language", "")
            code = parsed.get("code", "")
            mock = parsed.get("mock_params", None)
            if isinstance(code, str):
                code_escaped = code.replace("\n", "\\n")
                if len(code_escaped) > 120:
                    code_escaped = code_escaped[:117] + "…"
            else:
                code_escaped = ""
            parts = [str(lang), code_escaped]
            if mock:
                parts.append("mock=" + str(mock))
            return (f"• {name}", '    ⎿ "' + " ".join(parts) + '"')

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
