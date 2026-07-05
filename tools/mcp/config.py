# -*- coding: utf-8 -*-
"""MCP 配置 — 加载 mcp_servers.json"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class MCPServerConfig:
    """单个 MCP server 的配置"""

    name: str
    transport: str  # "stdio" | "sse" | "mcporter"

    # stdio
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None

    # sse / mcporter
    url: Optional[str] = None

    # timeouts
    timeout: int = 30          # 秒，向后兼容（默认值）
    connect_timeout: int = 30  # 秒，connect + initialize 握手
    exec_timeout: int = 60     # 秒，工具调用 / list_tools

    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "MCPServerConfig":
        timeout = data.get("timeout", 30)
        return cls(
            name=data["name"],
            transport=data["transport"],
            command=data.get("command"),
            args=data.get("args"),
            env=data.get("env"),
            url=data.get("url"),
            timeout=timeout,
            connect_timeout=data.get("connect_timeout", timeout),
            exec_timeout=data.get("exec_timeout", 60),
            enabled=data.get("enabled", True),
        )


def load_config(config_path: Optional[str] = None) -> List[MCPServerConfig]:
    """加载 mcp_servers.json，返回配置列表。

    优先级:
    1. 参数指定的路径
    2. ~/.jify/mcp_servers.json
    """
    if config_path is None:
        # 默认查找 ~/.jify/mcp_servers.json
        config_path = os.path.join(os.path.expanduser("~/.jify"), "mcp_servers.json")

    path = Path(os.path.expanduser(config_path))

    if not path.exists():
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError) as e:
        raise ValueError(f"failed to read {path}: {e}")

    servers = data.get("servers", [])
    if not isinstance(servers, list):
        raise ValueError(f"'servers' must be a list, got {type(servers).__name__}")

    configs = []
    for entry in servers:
        if not isinstance(entry, dict):
            continue
        cfg = MCPServerConfig.from_dict(entry)
        if cfg.enabled:
            configs.append(cfg)

    return configs
