# -*- coding: utf-8 -*-
"""
Jify Output Engine — 终端渲染、状态行、动画线程、输出缓冲。
"""

import json
import io
import math
import random
import re
import threading
import termios
import time
import unicodedata
from typing import Any, Dict, Optional

from rich.console import Console as RichConsole, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.table import Table as RichTable
from rich import box as rich_box
from rich.markup import escape, MarkupError
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
_TABLE_SEP_RE = re.compile(r'^\s*\|(?:\s*:?-+:?\s*\|)+\s*$|^\s*\|(?:\s*:?-+:?\s*\|)*\s*:?-+:?\s*$')
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


# 降级句子的句末标点判定。命中则不再补句号，避免出现「内容..」。
# 除半角 .!? 与省略号外也认中文的 。！？——中文内容很少以半角句号收尾，
# 只判半角会让每条中文段落都被追加一个英文句点。
_SENTENCE_END_RE = re.compile(r'[.!?。！？\u2026]["\')\]]*$')


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)


# 零宽字符与变体选择符不占显示位
_ZERO_WIDTH = frozenset('\u200b\u200c\u200d\ufe0f')


def _cell_display_width(cell: str) -> int:
    """估算单元格在终端里的显示宽度：宽字符/全角计 2，其余计 1。"""
    return sum(
        2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
        for ch in cell
        if ch not in _ZERO_WIDTH and not unicodedata.combining(ch)
    )


def _cell_est_lines(cell: str, budget: int) -> float:
    """估算单元格折行后占几行，用于判断是否该把表格降级成键值列表。

    含宽字符（中日韩、全角标点、emoji）的单元格沿用「字符数×2」口径：中文表格的
    降级分界点因此与改造前完全一致，避免波及已调好的中英混排效果。纯半角单元格
    改用精确显示宽度，否则纯 ASCII 内容会被高估一倍而提前降级成键值列表。
    """
    width = _cell_display_width(cell)
    n_chars = len(cell)
    return (n_chars * 2 if width != n_chars else width) / max(1, budget)


def _max_unbreakable_width(cells) -> int:
    """一组单元格里最长「不可断片段」的显示宽度。

    中文与全角标点可逐字折行，视觉上不会破坏语义；而英文标识符、路径、模型名
    这类连续半角串被 fold 拦腰切断（context_manager -> context_mana / ger）后
    基本不可读。因此只统计连续半角串的长度，宽字符按断点处理。
    """
    best = 0
    for cell in cells:
        for token in str(cell).split():
            # 按非 ASCII 段切开：中文/全角标点天然是断点
            for seg in re.split(r"[^\x00-\x7f]+", token):
                best = max(best, len(seg))
    return best


# 工具执行期的进度帧：由细到粗的一组星形符号，逐帧推进到末帧后按原路回退，
# 首尾不重复，形成 12 帧的往返序列。单帧停留 120ms，比原先按秒翻转的方波
# 更连贯，视觉上是「呼吸」而不是「开关」。
_EXEC_FRAMES = ("·", "✢", "✳", "✶", "✻", "✽")
_EXEC_YOYO = _EXEC_FRAMES + tuple(reversed(_EXEC_FRAMES))
_EXEC_FRAME_SEC = 0.12


def _exec_frame() -> str:
    """取当前时刻对应的执行帧，前后补空格凑满 3 cells。

    所有帧字符的半角宽度均为 1，补齐后与任意帧宽度一致，
    状态行整行宽度恒定，不会因换帧触发折行。
    """
    idx = int(time.monotonic() / _EXEC_FRAME_SEC) % len(_EXEC_YOYO)
    return f" {_EXEC_YOYO[idx]} "


# subagent 轨道：状态栏右緣单独一列，每个在跑的 subagent 占一行。状态栏与输入态
# （prompt_toolkit）共用同一套帧与配色，两处显示的节奏完全一致。
_SA_DOT = "●"
_SA_DOT_STYLE = "#f5d6a8"
_SA_BLINK_HALF = 1.0                         # 亮/灭各持续秒数，整体是 2s 方波
_SA_LABEL_MAX = len("subagent") + 2          # subagent + 两位序号
_SA_HEAD_W = 1 + 1 + _SA_LABEL_MAX           # 点 + 空格 + 标签
SA_RAIL_W = 2 + _SA_HEAD_W                   # │ + 空格 + 头部（最窄轨道）
_SA_RAIL_MIN_LINES = 3                       # 轨道最低视觉高度：只有 1 个实例且无 todos 时
                                             # 块高仅 1 行，竖线只剩一个字符；撑到 3 行更完整。
                                             # 只是下限，todos/实例变多时块高继续跟着涨
_SA_DESC_MIN = 12                            # 任务片段最小可用宽度，低于则不再展示片段
_SA_DESC_MAX = 30                            # 任务片段宽度上限


def sa_frame() -> str:
    """取当前时刻的指示点：亮/灭方波（有无有无），不做明暗渐变。

    亮帧 ``"● "``、灭帧 ``"  "`` 均为 2 cells，换帧不改变行宽。
    """
    on = int(time.monotonic() / _SA_BLINK_HALF) % 2 == 0
    return _SA_DOT + " " if on else "  "


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
        "markdown.code": "bold default",
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

        # subagent 只保留序号：右侧轨道每行一个「● subagentN」，其余信息不展示
        self._subagents: Dict[str, int] = {}

        self._model_phase: str = "idle"
        self._workflow_status: Optional[Dict] = None
        self._team_workers: Dict[str, Dict] = {}
        self._todos: Optional[list] = None

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

    def set_subagent(self, key: str, task: str = "") -> None:
        """登记一个正在跑的 subagent（key 用线程 ident 区分并发实例）。

        序号取当前最小的空闲槽位，同一时刻在跑的实例显示为连续的
        subagent1 / subagent2 …，finish 后槽位回收给下一个复用。
        task 为该实例的任务片段，供轨道第二档展示；已登记过的实例重复调用
        只更新片段，不重排序号（序号随生命周期稳定）。
        """
        snippet = " ".join(task.split())[:80]
        with self._anim_lock:
            if key in self._subagents:
                self._subagents[key]["task"] = snippet
                return
            used = {v["seq"] for v in self._subagents.values()}
            seq = 1
            while seq in used:
                seq += 1
            self._subagents[key] = {"seq": seq, "task": snippet, "last_tool": "", "tools": 0}

    def update_subagent(self, key: str, last_tool: str = "") -> None:
        """更新某个 subagent 的最近一次工具活动（轨道第二档的次级行）。"""
        with self._anim_lock:
            info = self._subagents.get(key)
            if info is None:
                return
            info["tools"] += 1
            if last_tool:
                info["last_tool"] = last_tool

    def clear_subagent(self, key: Optional[str] = None) -> None:
        """清理 subagent 槽位：传 key 只清该实例，不传则清全部"""
        with self._anim_lock:
            if key is None:
                self._subagents.clear()
            else:
                self._subagents.pop(key, None)

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
                    subagents: Optional[Dict[str, int]] = None,
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
        # subagent 轨道占右侧一列（分隔线 + 点 + 标签，宽裕时再带任务片段）。
        # 轨道与左侧并排、不占左侧行预算。宽度按最窄轨道保守扣除，给左侧
        # 正文和 team 表先留足空间；片段宽度在 compose_rail 里按左侧实际
        # 渲染宽度二次分配，避免把状态行挤到截断。
        _rail_items = []
        if subagents:
            _rail_items = sorted(subagents.items(), key=lambda kv: (kv[1].get("seq", 0), str(kv[0])))
        _wf_lines = 0

        t.append("✦ ", style=f"italic {JifyTheme.SUBTLE}")
        if wave_offset:
            t.append(self.wave_text(phrase, wave_offset))
        else:
            t.append(phrase, style=f"italic {JifyTheme.SUBTLE}")
        t.append(f"  ({self.fmt_elapsed(elapsed)}", style=JifyTheme.SUBTLE)

        # 工具执行期（executing）：首个 token 计数前的 "·" 换成往复推进的星形帧。
        # 每个帧字符都是 1 cell，前后各补一个空格后恒为 3 cells，整行宽度不随
        # 换帧变化，不会触发折行。执行结束 phase 切回 idle，前缀恢复 " · "。
        _exec_running = model_phase == "executing" and (sent > 0 or recv > 0)
        _exec_glyph = _exec_frame() if _exec_running else " · "

        # 早期设计缺陷，暂时预估 token, subagent 的 token 消耗也暂时预估
        if sent > 0:
            if _exec_running:
                t.append(_exec_glyph, style=JifyTheme.SUBTLE)
            else:
                t.append(" · ", style=JifyTheme.SUBTLE)
            _exec_running = False  # 只替换第一个箭头前的点
            t.append(f"↑ {self.fmt_tokens(sent // 2)} tokens", style=JifyTheme.YELLOW)
        if recv > 0:
            if _exec_running:
                t.append(_exec_glyph, style=JifyTheme.SUBTLE)
            else:
                t.append(" · ", style=JifyTheme.SUBTLE)
            _exec_running = False
            t.append(f"↓ {self.fmt_tokens(recv // 2)} tokens", style=JifyTheme.GREEN)
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

        # 行数预算分配：状态行(1) + workflow 行先扣除，再扣 team 表固定开销，
        # 剩下的按 stream / subagent / todos 的顺序分配。
        # team 表固定开销 = 空行 + 标题 + 统计 + think + token + 表尾 = 6 行，
        # 不含活动流行；活动流条数按剩余高度动态收缩（原先固定 10 条，小窗口
        # 会顶穿终端高度 → Live 光标上移越界 → 整屏滚动爆闪）。
        _n_todo = len(todos) if todos else 0
        _team_fixed = 6 if team_workers else 0
        _tip_lines = 3 if tip else 0          # tips 正文 1 行 + 前后空行 2 行
        _remaining = _max_lines - 1 - _wf_lines - _team_fixed - _tip_lines
        if team_workers:
            # subagent / todos 每条占 1 行，先给它们留位，剩余才给活动流；
            # 至少留 1 条（worker 在跑就必须能看到它最后干了什么）
            _stream_room = _remaining - _n_todo
            _MAX_STREAM_LINES = max(1, min(_MAX_STREAM_LINES, _stream_room))
            _remaining -= _MAX_STREAM_LINES

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
                # 任务描述常为多行文本，Text 对 \n 强制断行且 no_wrap 挡不住，
                # 先折叠空白压成单行，否则帧高随描述行数增长导致 Live 抖动
                task = " ".join(task.split())
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
                # ellipsis 兜底：即使个别内容算宽仍有偏差，也只截断不折行。
                # 必须带 no_wrap：rich 的 overflow="ellipsis" 只在 no_wrap 生效，
                # 缺了它列被压窄时每行会 fold 成 2-3 行，帧高随 worker 数膨胀
                # （实测 6 worker × 10 条活动流：42 行，终端仅 24 行）。
                team_table.add_column(overflow="ellipsis", no_wrap=True)
            team_table.add_row(*cells)

        # 行数预算：todos 超出则截掉低优先级尾部（subagent 在右侧轨道，不占左侧行）。
        # 有 subagent 在跑时给它至少留 1 行：否则 todos 排满时轨道整个消失，
        # 用户看不到「还有子任务在跑」这一事实。
        _rail_room = 1 if _rail_items else 0
        _todo_visible = min(_n_todo, max(0, _remaining - _rail_room))

        if todos:
            status_icon = {
                "completed": "☑",
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

        left = Group(t, team_table) if team_table is not None else t
        if not _rail_items:
            return left
        return self.compose_rail(left, _rail_items, self.console.width, _max_lines)

    @staticmethod
    def _sa_fit(text: str, width: int) -> str:
        """按显示宽度截断（中文占 2 列），超宽补省略号。"""
        if width <= 0:
            return ""
        if cell_len(text) <= width:
            return text
        kept, used = [], 0
        for ch in text:
            cw = cell_len(ch)
            if used + cw > width - 1:
                break
            used += cw
            kept.append(ch)
        return "".join(kept) + "…"

    @classmethod
    def compose_rail(cls, left, items: list, width: int, max_lines: int) -> Any:
        """把左侧状态块与右侧 subagent 轨道拼成一行两列。

        轨道按可用高度分三档降级（与左侧正文抢同一份行预算，故必须先算行数）：

        - full    ：每实例 2 行，``● subagent1 任务片段`` + ``  ⎿ 最近工具``；
                    还没调过工具的实例只有头部 1 行，不画空的工具行
        - compact ：每实例 1 行，只剩 ``● subagent1 任务片段``
        - minimal ：整体 1 行摘要，``● 3 agents running…``

        左列宽度自适应，右列宽度按「终端宽度 - 左侧实际最长行」二次分配：
        片段只在左侧确实用不到那么宽时才会占用，避免把状态行挤到截断。
        两列之间用竖向分隔线连接，行数取两者较大值——轨道比左侧长时左列
        补空行，反之右列补空行，保证每行都有分隔线，视觉上是一条竖线。

        grid 的 vertical 对齐在 expand 模式下不生效，只能靠两侧行数相等来
        对齐；行数需求要预渲染左块才能知道，硬算容易和实际渲染差一行。
        """
        # 量左侧实际行数（决定轨道能要几行）与自然宽度（硬约束用）
        console = RichConsole(width=width, file=io.StringIO(), legacy_windows=False,
                              color_system=None, force_terminal=False)
        n_left = len(console.render_lines(left, console.options, pad=False))

        wide = RichConsole(width=400, file=io.StringIO(), legacy_windows=False,
                           color_system=None, force_terminal=False)
        natural = wide.render_lines(left, wide.options, pad=False)
        left_w = max((sum(seg.cell_length for seg in ln) for ln in natural), default=0)

        # 轨道宽度是硬约束推导的：左侧自然宽度 + 最窄轨道必须放得下，
        # 否则一出现 subagent 就把状态行尾巴裁掉（既有体验倒退）。
        # 有余量时才给任务片段，且不超过实际最长片段。
        spare = width - min(left_w, width) - SA_RAIL_W - 1
        task_w = max((cell_len(v.get("task") or "") for _, v in items), default=0)
        desc_w = min(task_w, _SA_DESC_MAX, spare) if spare >= _SA_DESC_MIN else 0
        rail_w = SA_RAIL_W + (1 + desc_w if desc_w else 0)

        # 行预算：左侧正文优先（它是主信息），轨道只能用剩余行数。
        # full 档下只有调过工具的实例才占 2 行（头部 + 工具行），刚启动还没
        # 调工具的只占 1 行，所以行数需求要逐实例统计，不能一律按 2*n 算。
        room = max(0, max_lines - n_left)
        n = len(items)
        _full_lines = sum(2 if (v.get("last_tool") or "") else 1 for _, v in items)
        if room >= _full_lines:
            mode = "full"
        elif room >= n:
            mode = "compact"
        elif room >= 1:
            mode = "minimal"
        else:
            mode = "none"

        rail = Text()
        rail.no_wrap = True
        rail.overflow = "crop"

        def _head(i: int, info: Dict) -> None:
            rail.append("│ ", style=JifyTheme.SUBTLE)
            rail.append(sa_frame(), style=f"bold {_SA_DOT_STYLE}")
            rail.append(f"subagent{info.get('seq', i + 1)}", style=JifyTheme.SUBTLE)

        if mode == "full":
            for i, (_, info) in enumerate(items):
                if i:
                    rail.append("\n")
                _head(i, info)
                task = info.get("task") or ""
                if task and desc_w:
                    rail.append(" " + cls._sa_fit(task, desc_w), style=JifyTheme.SUBTLE)
                # 没有工具活动就不画工具行：刚启动的实例只留一行头部，
                # 不出现空的 ⎿ 占位
                last = info.get("last_tool") or ""
                if not last:
                    continue
                rail.append("\n")
                rail.append("│ " + "    ⎿ ", style=JifyTheme.SUBTLE)
                rail.append(cls._sa_fit(last, rail_w - 8), style=JifyTheme.SUBTLE)
        elif mode == "compact":
            for i, (_, info) in enumerate(items):
                if i:
                    rail.append("\n")
                _head(i, info)
                task = info.get("task") or ""
                if task and desc_w:
                    rail.append(" " + cls._sa_fit(task, desc_w), style=JifyTheme.SUBTLE)
        elif mode == "minimal":
            # 最窄轨道只有 SA_RAIL_W，放不下长句；用 n×空格 的紧凑写法
            rail.append("│ ", style=JifyTheme.SUBTLE)
            rail.append(sa_frame(), style=f"bold {_SA_DOT_STYLE}")
            rail.append(f"×{n}", style=JifyTheme.SUBTLE)
        else:
            return left

        # 竖线要贯穿整个状态块：块高取三者最大——左侧实际行数、轨道自身行数、
        # 轨道最小视觉高度。只有 1 个实例且没有 todos 时块高仅 1 行，竖线只剩
        # 一个字符看着像悬空，撑到下限更稳；有 todos / 多实例时块高自然变高，
        # 竖线跟着铺满。上限受帧高预算约束，不会顶穿终端。
        rail_lines = rail.plain.count("\n") + 1
        block_lines = max(n_left, rail_lines, min(_SA_RAIL_MIN_LINES, max_lines))
        for _ in range(block_lines - rail_lines):
            rail.append("\n│", style=JifyTheme.SUBTLE)

        grid = RichTable.grid(padding=0, expand=True)
        grid.add_column(ratio=1, no_wrap=True, overflow="crop")
        grid.add_column(width=rail_w, no_wrap=True, overflow="crop")
        grid.add_row(left, rail)
        return grid

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
                # 兜底：status_line 已按终端高度分配行数预算，理论上不会超；
                # 但 tip / 估算偏差仍可能溢出，此时裁掉而不是让光标上移越界
                vertical_overflow="ellipsis",
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
                vertical_overflow="ellipsis",
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

                    elapsed = time.time() - self._session_start_time
                    phrase = self._phrase
                    sent = self._anim_sent
                    recv = self._anim_recv
                    phase = self._model_phase
                    wf_status = self._workflow_status
                    team_snapshot = dict(self._team_workers)
                    todos_snapshot = list(self._todos) if self._todos else None
                    subagent_snapshot = {k: dict(v) for k, v in self._subagents.items()}

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
        text = re.sub(r'`(.+?)`', r'[bold]\1[/bold]', text)
        try:
            return Text.from_markup(text, emoji=False)
        except MarkupError:
            # 单元格内 * 与 ` 混排时，上面的顺序替换可能凑出错配标签而抛异常；
            # 表格只是展示，降级成纯文本，不能让整轮对话被打死。
            return Text(raw)

    def _natural_col_widths(self, header_cells: list, data_rows: list, n_cols: int) -> list:
        """各列一列放完所需的最小显示宽度（取表头与所有数据格的最大值）。

        宽度取自 _markdown_cell_to_text（已剥离 emoji 与 Markdown 标记），
        用 cell_len 而非 len，中文与全角标点按 2 计。
        """
        nat = [1] * n_cols
        for j in range(n_cols):
            w = self._markdown_cell_to_text(header_cells[j].strip()).cell_len
            for row in data_rows:
                if j < len(row):
                    w = max(w, self._markdown_cell_to_text(row[j].strip()).cell_len)
            nat[j] = max(1, w)
        return nat

    def _compute_col_caps(self, nat: list, n_cols: int) -> list:
        """按各列内容宽度分配列宽上限。

        做法是「等上限注水」：找一个统一上限 T，让每列取 min(自然宽, T)
        加起来不超过可用宽度。短列因此能拿到完整宽度一行放完，折行压力
        全部集中在最长的列上，而不是像均分上限那样把所有列一起砍碎。
        """
        # rich 每列实占 = 列宽 + 左右 padding 2 + 列间分隔符 1，按 3 计，
        # 否则预算偏大，末尾几列会被挤成 0 宽直接不显示
        avail = self._console.width - 3 * n_cols
        if avail < 4 * n_cols:
            # 可用宽度连 min_width=4 都放不下时均分缩小，避免整列被挤掉
            per = max(1, avail // n_cols)
            return [per] * n_cols
        if sum(nat) <= avail:
            return [max(4, w) for w in nat]

        lo, hi, best = 4, max(nat), 4
        while lo <= hi:
            mid = (lo + hi) // 2
            if sum(min(w, mid) for w in nat) <= avail:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        caps = [min(w, best) for w in nat]

        # 余数补给缺口最大的列（缩短最长列的折行行数）
        rem = avail - sum(caps)
        while rem > 0:
            i = max(range(n_cols), key=lambda k: nat[k] - caps[k])
            if nat[i] <= caps[i]:
                break
            caps[i] += 1
            rem -= 1
        return caps

    def _build_rich_table(self, table_text: str) -> RichTable | list[Text]:
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

        table = RichTable(
            box=rich_box.SIMPLE_HEAD, show_lines=False, padding=(0, 1), expand=False,
        )
        # 列宽上限按各列内容宽度分配（放得下不折行，放不下按比例压缩），
        # 不再用「终端宽 ÷ 列数」等分——那会把长列硬砍成碎行。
        n_cols = max(1, len(header_cells))
        nat = self._natural_col_widths(header_cells, data_rows, n_cols)
        caps = self._compute_col_caps(nat, n_cols)
        # 兜底降级：四选一即降级为键值列表，可读性都优于碎行表格
        #   1) 折行爆炸（单格要撕 8 行以上）
        #   2) 终端太窄（均分到每列不足 10 显示列 ≈ 5 个汉字）
        #   3) 内容超出可用宽度且【被压缩的列】里存在压到 10 显示列以下的。
        #      只统计确实发生压缩的列：短列表头（如「是否」「做吗」）自然宽本就
        #      不足 10，从未被压缩却被 min(caps) 计入，会让「短表头 + 一个长内容列」
        #      的正常表格被误降级成键值列表，可读性反而更差。
        #   4) 硬切：某列被压缩后放不下该列最长的不可断片段（英文标识符/路径/
        #      模型名）。这类串被 fold 拦腰切断后基本不可读，此时降级反而能完整显示。
        #   5) 两列表格本质是键值对：内容一旦需要折行，表格边框只剩开销（每列白吃
        #      3 列 padding+分隔符），降级成「键: 值」反而能多出可用宽度。内容都放
        #      得下时不降级，形如 |参数|值| 的小表仍然好看。
        compressed = any(nat[j] > caps[j] and caps[j] < 10 for j in range(n_cols))
        narrow = self._console.width // n_cols - 2 < 10
        hard_cut = any(
            nat[j] > caps[j]
            and caps[j] < _max_unbreakable_width(
                [row[j] for row in data_rows if j < len(row)]
            )
            for j in range(n_cols)
        )
        kv_pair = n_cols == 2 and any(nat[j] > caps[j] for j in range(n_cols))
        est_lines_per_cell = max(
            (_cell_est_lines(c, caps[j]) for row in data_rows
             for j, c in enumerate(row[:n_cols])),
            default=0,
        )
        if n_cols >= 2 and (est_lines_per_cell > 8 or narrow
                            or compressed or hard_cut or kv_pair):
            fallback: list[Text] = []
            for row in data_rows:
                line = Text()
                parts = 0
                for j in range(min(len(row), n_cols)):
                    val = row[j].strip()
                    if not val:
                        continue
                    # 段间用空格而非竖线：降级后是散文式短句，竖线在连续文字里
                    # 只是噪音；句子之间再补句号收尾，读起来是完整的一句话。
                    if parts:
                        line.append(" ")
                    # 表头为空（如首列留空的对照表）时不输出 ": value" 这种畸形前缀
                    head = header_cells[j].strip() if j < len(header_cells) else ''
                    seg = f"{head}: {val}" if head else val
                    if not _SENTENCE_END_RE.search(seg):
                        seg += "."
                    line.append_text(self._markdown_cell_to_text(seg))
                    parts += 1
                if parts:
                    fallback.append(line)
            if fallback:
                return fallback
        for j, (header, align) in enumerate(zip(header_cells, alignments)):
            table.add_column(
                Text(header.strip(), justify=align),
                overflow="fold",
                no_wrap=False,
                min_width=min(4, caps[j] if j < len(caps) else 4),
                max_width=caps[j] if j < len(caps) else 4,
                justify=align,
            )

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
                    # 紧邻的两张表之间没有空行时，下一张表的表头行也以 | 开头，
                    # 紧跟其后的就是分隔行。遇到「| 开头的行 + 分隔行」这个组合
                    # 就停止收集，否则两张表会被拼成一张，第二张的表头与分隔行
                    # 变成第一张的数据行。
                    if j + 1 < len(lines) and _TABLE_SEP_RE.match(lines[j + 1]):
                        break
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
                    if isinstance(table, list):
                        # 降级键值列表：逐行 Text，避免列表对象被当作 renderable
                        for ln in table:
                            renderables.append(ln if isinstance(ln, Text) else Text(ln))
                    else:
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
