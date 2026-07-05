# -*- coding: utf-8 -*-
"""
工具审批模块 — 当工具需要审批时，直接同步向用户请求审批。

使用 Rich Panel 展示审批信息，暂停 Live 动画后等待用户输入：
  - Enter (Yes) → 放行，继续执行工具
  - n (No)      → 拒绝本次调用，返回错误供 LLM 调整方案
  - b (Break)   → 中断整个 agent loop

审批请求通过队列序列化，由独立 consumer 线程串行读取 input()，
调用线程阻塞在 Future 上等待审批结果。
"""
import json
import queue
import select
import shutil
import sys
from concurrent.futures import Future
import termios
import threading
from os import wait
from typing import Optional

from rich.panel import Panel
from rich.text import Text

from output_engine import OutputEngine, JifyTheme

_engine: Optional[OutputEngine] = None
break_requested = threading.Event()
_approval_active = False
_approval_queue: queue.Queue = queue.Queue()
_consumer_started = False
_consumer_lock = threading.Lock()

def set_approval_engine(engine: OutputEngine) -> None:
    global _engine
    _engine = engine


def is_approval_active() -> bool:
    return _approval_active


def clear_break() -> None:
    break_requested.clear()


class ApprovalBreak(Exception):
    def __init__(self, tool_name: str = ""):
        self.tool_name = tool_name
        break_requested.set()
        super().__init__(f"审批中断: 用户拒绝了 {tool_name} 并选择 break")


class ApprovalDenied(Exception):
    def __init__(self, tool_name: str = ""):
        self.tool_name = tool_name
        super().__init__(f"审批拒绝: 用户拒绝了 {tool_name}")


def _format_args(args: dict) -> str:
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 200:
            s = s[:197] + "..."
        parts.append(f"{k}={s}")
    return ", ".join(parts)


def _render_diff(diff: str) -> Text:
    lines = diff.rstrip("\n").split("\n")
    t = Text()
    for i, line in enumerate(lines):
        if line.startswith("---") or line.startswith("+++"):
            t.append(line, style=f"bold {JifyTheme.SUBTLE}")
        elif line.startswith("@@"):
            t.append(line, style=f"bold {JifyTheme.ACCENT}")
        elif line.startswith("+"):
            t.append(line, style="on #1a3a1a")
        elif line.startswith("-"):
            t.append(line, style="on #3a1a1a")
        else:
            t.append(line, style=JifyTheme.SUBTLE)
        if i < len(lines) - 1:
            t.append("\n")
    return t


def _build_panel(tool_name: str, args_preview: str, preview: Optional[str],
                 width: int, queue_len: int) -> Panel:
    """构建审批 Panel"""
    panel_content = Text()
    panel_content.append(f"工具 '{tool_name}' 请求执行\n", style=f"bold {JifyTheme.ACCENT}")
    if args_preview:
        panel_content.append(f"\n参数: {args_preview}\n", style=JifyTheme.SUBTLE)
    else:
        panel_content.append("\n(无参数)\n", style=JifyTheme.SUBTLE)

    if preview:
        panel_content.append("\n" + "─" * (width - 4) + "\n", style=JifyTheme.SUBTLE)
        panel_content.append(_render_diff(preview))
        panel_content.append("─" * (width - 4) + "\n", style=JifyTheme.SUBTLE)

    footer = "[Enter] yes    [n] no    [b] break"
    if queue_len > 0:
        footer = f"[{queue_len + 1} 个审批排队]  " + footer
    panel_content.append(f"\n{footer}", style=JifyTheme.YELLOW)

    return Panel(
        panel_content,
        title=f"[bold {JifyTheme.ACCENT}]⏳ 审批: {tool_name}[/]",
        border_style=JifyTheme.ACCENT,
        width=width,
    )


def _print_fallback(tool_name: str, args_preview: str, preview: Optional[str],
                    width: int, queue_len: int) -> None:
    """非 Rich 环境的回退渲染 构建审批块"""
    print()
    print(f"  ╭─ 审批: {tool_name} ─{'─' * (width - 13)}╮")
    print(f"  │  工具 '{tool_name}' 请求执行{' ' * (width - 17 - len(tool_name))}│")
    if args_preview:
        for line in _wrap_text(f"  参数: {args_preview}", width):
            print(line)
    if preview:
        print(f"  │{'─' * (width - 4)}│")
        for line in preview.split("\n")[:60]:
            print(f"  │ {line}{' ' * (width - 4 - len(line))} │")
        if len(preview.split("\n")) > 60:
            remaining = len(preview.splitlines()) - 60
            print(f"  │ ... ({remaining} more lines) ...{' ' * (width - 24 - len(str(remaining)))} │")
    print(f"  │  [Enter] yes    [n] no    [b] break{' ' * (width - 35)}│")
    print(f"  ╰{'─' * width}╯")
    print()


def _read_approval_choice(tool_name: str, timeout: float = 120.0) -> bool:
    """读取用户审批输入（运行在 consumer 线程，保证只有一个 input() 活跃）

    stop_live 由 consumer 在外层调用；本函数负责 input() 循环。
    使用 select.select() 监听 stdin，超时未输入则自动 break。

    Args:
        timeout: 等待用户输入的超时秒数，超时后自动 break

    Returns:
        True  → 批准
        False → 拒绝
    Raises:
        ApprovalBreak → 用户选择中断 或 审批超时
    """
    global _approval_active

    _approval_active = True
    fd = sys.stdin.fileno()
    _saved_tcattr = termios.tcgetattr(fd)
    try:
        # 先排空所有待输出内容，确保 Panel / prompt 已传输到终端
        sys.stdout.flush()
        _canonical = termios.tcgetattr(fd)
        _canonical[3] |= termios.ECHO | termios.ICANON
        # TCSANOW 立即切换模式（不等输出排空），紧接 TCIFLUSH 清残余 raw 输入。
        # 避免 TCSAFLUSH 的输出排空窗口内用户 Enter 被当作 raw 字节刷掉。
        termios.tcsetattr(fd, termios.TCSANOW, _canonical)
        termios.tcflush(fd, termios.TCIFLUSH)

        while True:
            prompt = f"  Approve [{tool_name}]? [Enter] yes / [n]o / [b]reak: "
            sys.stdout.write(prompt)
            sys.stdout.flush()

            # Flush any leftover input before blocking on select
            termios.tcflush(fd, termios.TCIFLUSH)

            try:
                rlist, _, _ = select.select([sys.stdin], [], [], timeout)
            except (KeyboardInterrupt, ValueError):
                print("\n  [审批] 输入中断，默认拒绝")
                return False

            if not rlist:
                print("\n  [审批] ⊘ 超时未操作，默认中断循环\n")
                raise ApprovalBreak(tool_name)

            try:
                choice = sys.stdin.readline()
            except (EOFError, KeyboardInterrupt):
                print("\n  [审批] 输入中断，默认拒绝")
                return False

            if not choice:  # EOF
                print("\n  [审批] 输入中断，默认拒绝")
                return False

            choice = choice.strip().lower()

            if not choice:
                print("  [审批] ✓ 已批准\n")
                return True
            if choice in ("y", "yes"):
                print("  [审批] ✓ 已批准\n")
                return True
            if choice in ("n", "no"):
                print("  [审批] ✗ 已拒绝\n")
                return False
            if choice in ("b", "break"):
                print("  [审批] ⊘ 中断循环\n")
                raise ApprovalBreak(tool_name)
            print("  无效输入，请按回车批准 / n(拒绝) / b(中断)")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, _saved_tcattr)
        _approval_active = False


def _approval_consumer() -> None:
    """consumer 守护线程：从队列逐个取出审批请求，串行调用 input()"""
    while True:
        item = _approval_queue.get()
        if item is None:
            break

        tool_name, args, preview, timeout, future = item
        f: Future = future

        if break_requested.is_set():
            f.set_result(False)
            continue

        try:
            if _engine is not None:
                _engine.stop_live()

            args_preview = _format_args(args)
            term_width = shutil.get_terminal_size().columns
            width = min(term_width - 4, 100)
            queue_len = _approval_queue.qsize()

            panel = _build_panel(tool_name, args_preview, preview, width, queue_len)

            if _engine is not None and hasattr(_engine, '_console'):
                _engine._console.print()
                _engine._console.print(panel)
                _engine._console.print()
            else:
                _print_fallback(tool_name, args_preview, preview, width, queue_len)

            approved = _read_approval_choice(tool_name, timeout=timeout)
            f.set_result(approved)
        except ApprovalBreak as e:
            f.set_exception(ApprovalBreak(e.tool_name))
            _drain_queue()
        except Exception as e:
            f.set_exception(e)
        finally:
            # restart live AFTER terminal is fully restored (by _read_approval_choice's finally)
            if _engine is not None:
                _engine.restart_live()


def _drain_queue() -> None:
    """清空队列中所有未处理的审批请求（break 时调用）"""
    while not _approval_queue.empty():
        try:
            _, _, _, _, f = _approval_queue.get_nowait()
            f.set_result(False)
        except queue.Empty:
            break


def _start_consumer() -> None:
    global _consumer_started
    with _consumer_lock:
        if _consumer_started:
            return
        clear_break()
        t = threading.Thread(target=_approval_consumer, daemon=True)
        t.start()
        _consumer_started = True


def request_approval(tool_name: str, args: dict, preview: Optional[str] = None,
                     timeout: float = 120.0) -> bool:
    """请求用户审批工具执行。

    将审批请求放入队列，由 consumer 线程串行处理。
    调用线程阻塞在 Future 上直到审批完成。

    Args:
        timeout: 等待用户输入的超时秒数，超时后自动视为 break（默认 120 秒）

    Returns:
        True  → 批准
        False → 拒绝
    Raises:
        ApprovalBreak → 用户在任意审批中选择中断 或 审批超时
    """
    # queue串行，防止多个线程同时调用 input() 抢 stdin 导致竞态
    _start_consumer()
    f: Future = Future()
    _approval_queue.put((tool_name, args, preview, timeout, f))
    try:
        # 阻塞等用户反馈
        return f.result()
    except ApprovalBreak as e:
        raise e


def _wrap_text(text: str, width: int) -> list:
    result = []
    inner = width - 4
    while len(text) > inner:
        result.append(f"  │ {text[:inner]} │")
        text = "  " + text[inner:]
    if text:
        result.append(f"  │ {text}{' ' * (inner - len(text))} │")
    return result
