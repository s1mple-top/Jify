# -*- coding: utf-8 -*-
"""MCP 管理工具 — mcp_reload / mcp_list

启动时自动注册，Agent 可通过这两个工具管理 MCP 连接。
"""

import json
from tools.registry import register_tool
from tools.mcp.manager import mcp_manager


@register_tool(
    name="mcp_reload",
    description="热重载 MCP server 连接和工具。重新加载 mcp_servers.json 配置，断开旧连接，建立新连接，重新发现并注册所有 MCP 工具。",
    parameters={
        "type": "object",
        "properties": {
            "config_path": {
                "type": "string",
                "description": "mcp_servers.json 文件路径（可选，默认使用 ~/.jify/mcp_servers.json）",
            }
        },
        "required": [],
    },
    parallel_safe=False,
    # requires_approval=False,
)
def mcp_reload(config_path: str = None) -> str:
    """热重载所有 MCP 连接和工具"""
    try:
        result = mcp_manager.reload(config_path)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})


@register_tool(
    name="mcp_list",
    description="列出所有已配置的 MCP server 及其状态：名称、传输方式、连接状态、工具数量、工具列表。",
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
    parallel_safe=True,
    # requires_approval=False,
)
def mcp_list() -> str:
    """列出所有 MCP server 状态"""
    servers = mcp_manager.list_servers()
    return json.dumps(servers, ensure_ascii=False, indent=2)
