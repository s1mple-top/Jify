# -*- coding: utf-8 -*-
"""MCPManager — 连接管理 + 工具发现 + 动态注册到 Jify ToolRegistry。

基于官方 mcp 包（Anthropic MCP Python SDK），使用后台 asyncio event loop
桥接 async API 到 Jify 的同步代码。

生命周期:
    1. load_servers()        → 加载配置，创建 _McpConnection
    2. connect_all()         → 逐个 connect（含 initialize 握手）
    3. discover_and_register() → tools/list → 动态注册到 registry
    4. call_tool()           → 运行时工具调用（含自动重连）
"""

import asyncio
import logging
import os
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

from tools.registry import registry
from tools.mcp.config import load_config, MCPServerConfig

logger = logging.getLogger(__name__)



# 错误类型（保留，mcp_tools.py 中的 handler 会 catch）
class MCPError(Exception):
    """MCP 协议层错误"""

    def __init__(self, message: str, code: int = -1, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


def _make_tool_name(server_name: str, tool_name: str) -> str:
    """构造命名空间隔离的工具名: mcp__<server>__<tool>"""
    return f"mcp__{server_name}__{tool_name}"



# 单个 MCP 连接（async → sync 桥接）
class _McpConnection:
    """管理单个 MCP server 的连接。

    为每个 server 创建独立的后台线程 + asyncio event loop，
    通过 run_coroutine_threadsafe 将 async API 转为同步调用。
    """

    def __init__(self, cfg: MCPServerConfig):
        self.cfg = cfg
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._session = None  # mcp.client.session.ClientSession
        self._thread: Optional[threading.Thread] = None
        self._connected: bool = False
        self._shutdown: Optional[asyncio.Event] = None
        self._init_done: Optional[threading.Event] = None
        self._init_error: Optional[Exception] = None
        self._close_lock = threading.Lock()

    # 连接
    def connect(self) -> None:
        """启动后台 event loop 并建立 MCP 会话（含 initialize 握手）。

        _connect_and_serve() 在后台永久运行，这里只等待初始化完成。
        """
        self._loop = asyncio.new_event_loop()
        self._shutdown = asyncio.Event()
        self._init_done = threading.Event()
        self._init_error = None

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        # 调度后台协程（永不返回，一直运行到 shutdown）
        self._dispatch(self._connect_and_serve())

        # 等待初始化完成或超时
        if not self._init_done.wait(timeout=self.cfg.connect_timeout):
            self.close()
            # raise MCPError(
            #     f"Connection timeout after {self.cfg.connect_timeout}s"
            # )

        if self._init_error is not None:
            self.close()
            raise self._init_error

        if not self._connected:
            self.close()
            raise MCPError("Connection failed")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _dispatch(self, coro):
        """将协程调度到后台 loop，返回 concurrent.futures.Future。"""
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _connect_and_serve(self) -> None:
        """创建 transport + ClientSession，保持直到 shutdown 信号。

        初始化成功后设置 _init_done，失败则记录 _init_error。
        """
        try:
            from mcp.client.session import ClientSession
        except ImportError:
            self._init_error = MCPError(
                "mcp package not installed. Run: pip install mcp"
            )
            self._init_done.set()
            return

        try:
            if self.cfg.transport == "stdio":
                from mcp.client.stdio import stdio_client, StdioServerParameters
                server_params = StdioServerParameters(
                    command=self.cfg.command,
                    args=self.cfg.args or [],
                    env=self.cfg.env,
                )
                async with stdio_client(server_params, errlog=subprocess.DEVNULL) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        self._session = session
                        self._connected = True
                        self._init_done.set()
                        await self._shutdown.wait()

            elif self.cfg.transport == "sse":
                from mcp.client.sse import sse_client
                async with sse_client(
                    self.cfg.url,
                    timeout=self.cfg.connect_timeout,
                    sse_read_timeout=self.cfg.exec_timeout,
                ) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        self._session = session
                        self._connected = True
                        self._init_done.set()
                        await self._shutdown.wait()

            else:
                self._init_error = MCPError(
                    f"unsupported transport: {self.cfg.transport}"
                )
                self._init_done.set()
        except Exception as e:
            self._init_error = e
            self._init_done.set()
        finally:
            self._connected = False
            self._session = None

    # 重连
    def reconnect(self) -> None:
        """断开并重新建立连接。异常时自动 close。"""
        self.close()
        self._loop = asyncio.new_event_loop()
        self._shutdown = asyncio.Event()
        self._init_done = threading.Event()
        self._init_error = None
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        self._dispatch(self._connect_and_serve())

        if not self._init_done.wait(timeout=self.cfg.connect_timeout):
            self.close()
            raise MCPError(
                f"Reconnection timeout after {self.cfg.connect_timeout}s"
            )

        if self._init_error is not None:
            self.close()
            raise self._init_error

        if not self._connected:
            self.close()
            raise MCPError("Reconnection failed")

    def is_alive(self) -> bool:
        """检查连接是否活跃（轻量级，不触发网络 IO）。"""
        return (
            self._connected
            and self._loop is not None
            and self._loop.is_running()
            and self._session is not None
        )

    # 工具操作
    def list_tools(self) -> list:
        """返回工具列表（每项为 dict）。"""

        async def _list():
            result = await self._session.list_tools()
            return [t.model_dump() for t in result.tools]

        return self._dispatch(_list()).result(timeout=self.cfg.exec_timeout)

    def call_tool(self, name: str, arguments: dict) -> Any:
        """调用工具，返回 CallToolResult 的 dict。"""

        async def _call():
            result = await self._session.call_tool(name, arguments)
            return result.model_dump()

        return self._dispatch(_call()).result(timeout=self.cfg.exec_timeout)

    # 清理
    def close(self) -> None:
        """优雅关闭连接（线程安全）。"""
        with self._close_lock:
            if self._loop and self._shutdown:
                try:
                    self._loop.call_soon_threadsafe(self._shutdown.set)
                    time.sleep(0.3)  # 给 async with 块一点时间清理
                    self._loop.call_soon_threadsafe(self._loop.stop)
                except Exception:
                    pass
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=5)
            self._connected = False
            self._session = None
            self._loop = None
            self._shutdown = None



# MCP 管理器（单例）
class MCPManager:
    """管理所有 MCP 连接和工具注册。"""

    def __init__(self):
        self._servers: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self._mcp_tool_names: set = set()


    # 启动流程
    def load_servers(self, config_path: Optional[str] = None) -> int:
        """加载 mcp_servers.json，为每个 server 创建 _McpConnection。

        Returns: 加载的 server 数量
        """
        configs = load_config(config_path)
        count = 0

        with self._lock:
            for cfg in configs:
                if cfg.name in self._servers:
                    continue

                if cfg.transport not in ("stdio", "sse"):
                    msg = (
                        f"unsupported transport '{cfg.transport}' "
                        f"for server '{cfg.name}' — skipped"
                    )
                    logger.warning(msg)
                    print(f"  MCP:  {msg}")
                    continue

                conn = _McpConnection(cfg)
                self._servers[cfg.name] = {
                    "config": cfg,
                    "connection": conn,
                    "tools": {},
                    "connected": False,
                }
                count += 1

        return count

    def connect_all(self) -> Dict[str, str]:
        """逐个连接所有 MCP server（含 initialize 握手）。

        Returns: {server_name: "ok" | "error: ..."}
        """
        results = {}

        for name, entry in list(self._servers.items()):
            conn = entry["connection"]
            try:
                conn.connect()
                entry["connected"] = True
                results[name] = "ok"
                logger.info(f"MCP server '{name}' connected")
            except Exception as e:
                results[name] = f"error: {e}"
                # logger.warning(f"MCP server '{name}' connection failed: {e}")

        return results

    def discover_and_register(self) -> Dict[str, int]:
        """对已连接的 server 执行 tools/list，动态注册工具。

        Returns: {server_name: tool_count}  (-1 = 发现失败)
        """
        results = {}

        for name, entry in list(self._servers.items()):
            if not entry["connected"]:
                results[name] = -1
                continue

            conn = entry["connection"]
            try:
                tools = conn.list_tools()
                entry["tools"] = {}
                for tool in tools:
                    tool_name = tool.get("name", "")
                    if not tool_name:
                        continue
                    entry["tools"][tool_name] = tool
                    self._register_mcp_tool(name, tool_name, tool)

                results[name] = len(tools)
            except Exception as e:
                results[name] = -1
                logger.warning(f"Failed to discover tools for '{name}': {e}")

        return results


    # 运行时工具调用（含自动重连）
    _MAX_RECONNECT_RETRIES = 2

    def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Any:
        """调用指定 MCP server 上的工具。

        连接断开时自动尝试重连（最多 {_MAX_RECONNECT_RETRIES} 次，指数退避）。
        """
        for attempt in range(self._MAX_RECONNECT_RETRIES + 1):
            with self._lock:
                entry = self._servers.get(server_name)
                if entry is None:
                    raise MCPError(f"unknown MCP server: {server_name}")

                is_connected = entry["connected"]
                conn = entry["connection"]

            # 如果未连接，尝试重连
            if not is_connected:
                if attempt >= self._MAX_RECONNECT_RETRIES:
                    raise MCPError(
                        f"MCP server '{server_name}' is not connected "
                        f"after {self._MAX_RECONNECT_RETRIES} retries"
                    )
                backoff = 2 ** attempt
                logger.info(
                    f"MCP server '{server_name}' disconnected, "
                    f"reconnect attempt {attempt + 1}/{self._MAX_RECONNECT_RETRIES} "
                    f"in {backoff}s"
                )
                time.sleep(backoff)
                try:
                    conn.reconnect()
                    with self._lock:
                        entry["connected"] = True
                    logger.info(f"MCP server '{server_name}' reconnected")
                except Exception as e:
                    logger.warning(
                        f"MCP server '{server_name}' reconnection failed: {e}"
                    )
                    if attempt == self._MAX_RECONNECT_RETRIES - 1:
                        raise MCPError(
                            f"MCP server '{server_name}' reconnection failed: {e}"
                        )
                    continue

            # 已连接，尝试调用
            try:
                return conn.call_tool(tool_name, arguments)
            except Exception as e:
                logger.warning(
                    f"MCP tool '{server_name}/{tool_name}' failed: {e}"
                )
                # 连接可能已死，标记断开，触发下一轮重连
                with self._lock:
                    entry["connected"] = False
                if attempt == self._MAX_RECONNECT_RETRIES:
                    raise
                continue

        # 不应到达这里
        raise MCPError(f"exhausted retries for '{server_name}/{tool_name}'")


    # 热重载
    def reload(self, config_path: Optional[str] = None) -> Dict[str, Any]:
        """热重载所有 MCP server 连接和工具。

        1. 断开所有现有连接
        2. 注销所有旧的 MCP 工具
        3. 重新加载配置
        4. 重新连接 + 发现 + 注册

        Returns: {results: {server: status}, tool_count: int}
        """
        self.shutdown()

        server_count = self.load_servers(config_path)
        connect_results = self.connect_all()
        discover_results = self.discover_and_register()

        total_tools = sum(max(0, v) for v in discover_results.values())

        return {
            "results": connect_results,
            "servers": server_count,
            "tool_count": total_tools,
            "discover": discover_results,
        }

    def list_servers(self) -> List[Dict[str, Any]]:
        """列出所有已配置的 MCP server 及其状态。"""
        result = []
        with self._lock:
            for name, entry in self._servers.items():
                cfg = entry["config"]
                connected = entry["connected"]
                tool_count = len(entry.get("tools", {}))

                if not connected:
                    status = "disconnected"
                elif tool_count > 0:
                    status = "ready"
                else:
                    status = "connected (no tools discovered)"

                result.append({
                    "name": name,
                    "transport": cfg.transport,
                    "status": status,
                    "connected": connected,
                    "tool_count": tool_count,
                    "tools": list(entry.get("tools", {}).keys()),
                })
        return result


    # 清理
    def _unregister_mcp_tools(self) -> None:
        """从 registry 清理所有 MCP 工具。"""
        with self._lock:
            for tool_name in list(self._mcp_tool_names):
                registry.unregister(tool_name)
            self._mcp_tool_names.clear()

    def shutdown(self) -> None:
        """断开所有 MCP 连接并清理注册。"""
        self._unregister_mcp_tools()
        with self._lock:
            for name, entry in list(self._servers.items()):
                try:
                    entry["connection"].close()
                except Exception:
                    pass
            self._servers.clear()


    # 内部
    def _register_mcp_tool(
        self,
        server_name: str,
        tool_name: str,
        tool_def: Dict[str, Any],
    ) -> None:
        """将 MCP 工具注册到 Jify ToolRegistry。

        registry.dispatch("mcp__<server>__<tool>", args)
          → self.call_tool(server, tool, args)
        """
        full_name = _make_tool_name(server_name, tool_name)
        input_schema = tool_def.get("inputSchema", {})
        description = tool_def.get(
            "description",
            f"MCP tool from '{server_name}': {tool_name}", # 需要精细化描述
        )

        def make_handler(srv_name: str, t_name: str):
            """为每个 MCP 工具创建 handler 闭包，返回 dict（不再二次 JSON 包装）"""

            def handler(**kwargs):
                try:
                    return self.call_tool(srv_name, t_name, kwargs)
                except MCPError as e:
                    return {"error": str(e), "code": e.code}

            handler._mcp_server = srv_name
            handler._mcp_tool = t_name
            return handler

        registry.register(
            name=full_name,
            description=description,
            parameters=input_schema,
            handler=make_handler(server_name, tool_name),
            parallel_safe=False,
        )

        with self._lock:
            self._mcp_tool_names.add(full_name)


# 全局单例
mcp_manager = MCPManager()
