# -*- coding: utf-8 -*-
"""工具调用的可终止资源协议。

问题：Python 无法强杀线程。工具超时后线程仍卡在 C 层阻塞（等进程 / 等网络 / 等 IO），
外层只能放弃等待，无法真正终止。

办法：把「可被外部杀死的资源句柄」交给调用方托管。工具在拿到外部资源（子进程、
容器、连接）时登记一个 killer；外层超时时从 watchdog 线程调用它，真正把资源干掉。
工具线程随后会因为资源消失而自行返回，不是被强行打断。

覆盖边界（两级机制互补，缺一不可）：

1. killer 协议：能终止「外部资源」（子进程 / 容器）。工具线程随之自行返回。
2. 取消信号：kill 不掉的东西（纯 Python 循环、模型流式响应、进程内等待）靠
   工具在自然边界主动检查后优雅退出。这需要工具配合，故只在长循环里接入。
"""

import contextvars
import threading
from typing import Any, Callable, List, Optional, Tuple


class CallContext:
    """单次工具调用的上下文，承载可终止资源的登记、取消信号与统一清理。

    线程安全：工具线程登记、watchdog 线程执行清理，两边并发访问。
    """

    def __init__(self, cancel_event: Optional[threading.Event] = None) -> None:
        self._killers: List[Tuple[str, Callable[[], Any]]] = []
        self._lock = threading.Lock()
        self._killed = False
        self.kill_results: List[Tuple[str, bool, str]] = []
        self._registered_after_kill = 0
        # 协作式取消信号：kill 不掉进程内阻塞，只能靠工具自行检查后退出。
        # 与 killer 分开：killer 杀资源（强制、立即），cancel_event 通知线程（协作、下一个检查点生效）。
        # 允许外部注入：跨线程场景（team worker）需要与调用方共用同一个 Event。
        self.cancel_event = cancel_event or threading.Event()

    def register_killer(self, name: str, killer: Callable[[], Any]) -> None:
        """登记外部资源的终止函数。

        竞态处理：若清理已经执行过（外层已超时），立即执行本次登记 —— 否则工具在
        超时后才创建的资源会永久泄漏。这是唯一正确的顺序，不能只记录。

        killer 应满足：幂等、快速返回、不抛异常（抛了也会被吞掉）。
        """
        with self._lock:
            already_killed = self._killed
            if not already_killed:
                self._killers.append((name, killer))
        if already_killed:
            self._registered_after_kill += 1
            self._run_one(name, killer)

    def _run_one(self, name: str, killer: Callable[[], Any]) -> None:
        try:
            ok = bool(killer())
            note = ""
        except Exception as e:  # killer 自身异常不能影响清理流程
            ok = False
            note = str(e)
        self.kill_results.append((name, ok, note))

    def kill_all(self) -> List[Tuple[str, bool, str]]:
        """执行全部已登记的 killer，并把上下文标记为已清理。"""
        with self._lock:
            self._killed = True
            pending = list(self._killers)
            self._killers.clear()
        for name, killer in pending:
            self._run_one(name, killer)
        return list(self.kill_results)

    @property
    def killed(self) -> bool:
        return self._killed

    def cancel(self) -> None:
        """置位取消信号并清理全部外部资源。

        顺序：先 set 信号再 kill 资源 —— 工具若正在检查信号，能立刻看到；
        工具若卡在 killer 能杀的地方，资源消失后自然返回。
        """
        self.cancel_event.set()
        self.kill_all()

    def summary(self) -> Optional[str]:
        """把清理结果压成一句给人/模型看的话；无登记资源时返回 None。"""
        results = list(self.kill_results)
        if not results and not self._registered_after_kill:
            return None
        ok_items = [n for n, ok, _ in results if ok]
        fail_items = [(n, note) for n, ok, note in results if not ok]
        parts = []
        if ok_items:
            parts.append("已终止: " + ", ".join(ok_items))
        if self._registered_after_kill:
            parts.append(f"另有 {self._registered_after_kill} 项在超时后才创建，已即时清理")
        for n, note in fail_items:
            parts.append("终止失败: %s%s" % (n, ("（%s）" % note) if note else ""))
        return "；".join(parts)


# 当前工具调用的上下文（contextvars，供工具内部获取）
_call_context: 'contextvars.ContextVar[Optional[CallContext]]' = \
    contextvars.ContextVar('jify_call_context', default=None)


def get_call_context() -> Optional[CallContext]:
    """工具内部调用，拿到本次调用的上下文；不在工具调用中时返回 None。"""
    return _call_context.get()


def set_call_context(ctx: Optional[CallContext]):
    return _call_context.set(ctx)


def reset_call_context(token) -> None:
    try:
        _call_context.reset(token)
    except ValueError:
        # token 来自其他 Context，忽略（跨线程 reset 的边界情况）
        pass


def register_killer(name: str, killer: Callable[[], Any]) -> bool:
    """工具内部调用：登记一个可终止的外部资源。

    Returns:
        True 表示已登记（或已超时并立即清理）；False 表示没有活跃上下文。
    """
    ctx = get_call_context()
    if ctx is None:
        return False
    ctx.register_killer(name, killer)
    return True


def is_cancelled(extra_event: Optional[threading.Event] = None) -> bool:
    """当前调用是否已被取消。工具在长循环的自然边界调用它，命中即优雅退出。

    两级信号源，取或：
    - extra_event：显式传入的事件。跨线程场景必须走这条 —— 裸 threading.Thread
      不继承 contextvars（如 team worker 线程），拿不到调用上下文。
    - 当前线程的 CallContext.cancel_event。

    注意：这不是"能被强杀"。检查点之间的阻隔（一次模型流式响应、一次长命令）
    仍然要等它自然结束才能被发现，因此工具应在循环最内层也插入检查。
    """
    if extra_event is not None and extra_event.is_set():
        return True
    ctx = get_call_context()
    return ctx is not None and ctx.cancel_event.is_set()
