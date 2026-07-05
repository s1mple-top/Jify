# -*- coding: utf-8 -*-

import queue
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class UIEvent:
    type: str
    data: Any = None
    think: Optional[bool] = False


# 共享事件总线 — 工具 / 插件 / AgentLoop 向 CLIConsole 投递 UI 事件
event_bus = queue.Queue()
