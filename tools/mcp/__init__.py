# -*- coding: utf-8 -*-
"""MCP (Model Context Protocol) integration — 基于官方 mcp 包的动态工具发现。

使用 Anthropic 官方 MCP Python SDK（pip install mcp），
通过后台 asyncio event loop 桥接 async API 到 Jify 同步代码。

架构:
    config.py   → 加载 mcp_servers.json
    manager.py  → 连接管理 + 工具发现 + 动态注册到 ToolRegistry
                   （内部使用 mcp.client.stdio / mcp.client.sse）
"""

from tools.mcp.config import load_config, MCPServerConfig
from tools.mcp.manager import MCPManager, MCPError, mcp_manager

__all__ = [
    "load_config",
    "MCPServerConfig",
    "MCPError",
    "MCPManager",
    "mcp_manager",
]
