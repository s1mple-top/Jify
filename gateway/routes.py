"""FastAPI routes / WebSocket / 聊天处理 —— 从 gateway.py 提取。"""

import json
import os
import time
import uuid
import threading
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass, field

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from agent_loop import AgentLoop, AgentConfig
from jify_tool import registry

from .auth import (
    _create_token,
    _validate_token,
    _revoke_token,
    _get_admin_token,
    AdminLoginRequest,
)
from .ws_console import WebSocketConsole
from config.system_prompt import _discover_skills
from plugins.loader import PluginLoader
from tools.approval import ApprovalBridge, set_web_bridge

# 应用初始化

app = FastAPI(title="Jify")

static_dir = Path(__file__).parent.parent / "gateway_web"
static_dir.mkdir(exist_ok=True)

# Web 审批桥（gateway 单人模式，进程级全局单例）：工具审批改走浏览器弹窗，而非进程 stdin
_approval_bridge = ApprovalBridge()
set_web_bridge(_approval_bridge)

# 插件加载（启动时一次性加载，注册到全局 registry）
try:
    _gw_config = AgentConfig.load_from_yaml()
    _plugin_loader = PluginLoader(plugins_dir=_gw_config.plugins_dir)
    _plugin_loader.load_all(enabled_only=_gw_config.enabled_plugins)
except Exception:
    _plugin_loader = None

# 会话管理

@dataclass
class GatewaySession:
    user_id: str
    agent: AgentLoop
    console: Any
    messages: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


_sessions: Dict[str, GatewaySession] = {}
_sessions_lock = threading.Lock()


def _get_or_create_session(user_id: str) -> GatewaySession:
    with _sessions_lock:
        if user_id not in _sessions:
            config = AgentConfig.load_from_yaml()
            agent = AgentLoop(agent_config=config)
            _ensure_team_initialized(agent, config)
            _sessions[user_id] = GatewaySession(
                user_id=user_id,
                agent=agent,
                console=None,
            )
        return _sessions[user_id]


def _ensure_team_initialized(agent: AgentLoop, config) -> None:
    """初始化 Team 模式（对齐 cli/app.py 的 JifyCLI.__init__）。

    gateway 此前只创建 AgentLoop，未注册 TeamLeader，导致模型调用任意 team_*
    工具都拿到 "Team 模式未启动" 错误。team 是进程内全局单例（team.set_leader /
    get_leader 用模块级变量），故仅在尚未注册时初始化一次，避免重复 new session
    时覆盖 leader 导致旧 worker 线程泄漏。
    """
    from team import TeamOrchestrator, set_leader, get_leader

    if get_leader() is not None:
        return
    try:
        orch = TeamOrchestrator(agent.model_client, config)
        set_leader(orch.leader)
    except Exception:
        # 与顶部 plugin loader 一致：team 初始化失败不应阻断 gateway 启动
        pass


# 页面路由

@app.get("/")
async def index():
    html_path = static_dir / "login.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Jify Gateway</h1><p>gateway_web/login.html not found</p>")


@app.get("/chat")
async def chat_page():
    html_path = static_dir / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Jify Gateway</h1><p>gateway_web/index.html not found</p>")


@app.get("/api/skills")
async def api_skills():
    skills = _discover_skills()
    result = []
    for skill in skills:
        for name, desc in skill.items():
            result.append({"name": name, "description": desc})
    return {"skills": result}


@app.get("/api/plugins")
async def api_plugins():
    loader = _plugin_loader or PluginLoader(plugins_dir="~/.jify/plugins")
    candidates = loader.discover()
    result = []
    for plugin_path in candidates:
        try:
            manifest = json.loads((plugin_path / "plugin.json").read_text(encoding="utf-8"))
            name = manifest.get("name", plugin_path.name)
            version = manifest.get("version", "")
            ptype = manifest.get("type", [])
            desc = manifest.get("description", "")
            loaded = loader.is_loaded(name)
            result.append({
                "name": name,
                "version": version,
                "type": ptype,
                "description": desc,
                "directory": plugin_path.name,
                "loaded": loaded,
            })
        except Exception:
            result.append({
                "name": plugin_path.name,
                "version": "",
                "type": [],
                "description": "",
                "directory": plugin_path.name,
                "loaded": False,
                "error": "failed to read manifest",
            })
    return {"plugins": result}


# 配置 API

CONFIG_PATH = os.path.expanduser("~/.jify/config.yaml")


def _mask_sensitive(value: str) -> str:
    """对敏感值做部分脱敏显示"""
    if not value or len(value) <= 8:
        return "***" if value else ""
    return value[:4] + "***" + value[-4:]


@app.get("/api/config")
async def api_get_config():
    """读取当前 config.yaml 并以结构化形式返回"""
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        else:
            raw = {}
    except Exception:
        raw = {}

    # 返回全量配置；api_key 做脱敏标记
    # models 列表中的 api_key 也需要脱敏
    models_raw = raw.get("models", [])
    models_masked = []
    for m in (models_raw if isinstance(models_raw, list) else []):
        if isinstance(m, dict):
            masked = dict(m)
            if masked.get("api_key"):
                masked["api_key_masked"] = _mask_sensitive(masked["api_key"])
            models_masked.append(masked)

    fields = {
        "provider": raw.get("provider", ""),
        "model": raw.get("model", ""),
        "base_url": raw.get("base_url", ""),
        "api_key": raw.get("api_key", ""),
        "api_key_masked": _mask_sensitive(raw.get("api_key", "")),
        "max_iterations": raw.get("max_iterations", 100),
        "tool_delay": raw.get("tool_delay", 0.0),
        "max_workers": raw.get("max_workers", 8),
        "tool_timeout": raw.get("tool_timeout", 120.0),
        "SelfEvolutionModel": raw.get("SelfEvolutionModel", ""),
        "SelfEvolutionTurn": raw.get("SelfEvolutionTurn", 8),
        "context_compress_threshold": raw.get("context_compress_threshold", 900000),
        "admin_token": raw.get("admin_token", ""),
        "plugins_dir": raw.get("plugins_dir", "~/.jify/plugins"),
        "enabled_plugins": raw.get("enabled_plugins", None),
        "models": models_masked,
    }
    return {"config": fields}


class ConfigUpdateRequest(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    max_iterations: Optional[int] = None
    tool_delay: Optional[float] = None
    max_workers: Optional[int] = None
    tool_timeout: Optional[float] = None
    SelfEvolutionModel: Optional[str] = None
    SelfEvolutionTurn: Optional[int] = None
    context_compress_threshold: Optional[int] = None
    admin_token: Optional[str] = None
    plugins_dir: Optional[str] = None
    enabled_plugins: Optional[list] = None
    models: Optional[list] = None


@app.post("/api/config")
async def api_save_config(req: ConfigUpdateRequest):
    """保存配置到 config.yaml"""
    # 读取现有配置作为基础
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                current = yaml.safe_load(f) or {}
        else:
            current = {}
    except Exception:
        current = {}

    # 仅覆盖传入的非 None 字段
    updates = req.dict(exclude_none=True)
    current.update(updates)

    # 写回
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(current, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    return {"ok": True, "message": "配置已保存，重启 Gateway 生效"}


@app.post("/api/admin_login")
async def api_admin_login(req: AdminLoginRequest):
    admin_token = _get_admin_token()
    if not admin_token:
        raise HTTPException(status_code=500, detail="未配置 admin_token")
    if req.token != admin_token:
        raise HTTPException(status_code=401, detail="管理员 token 错误")
    token = _create_token("admin", "admin")
    return {"token": token, "user_id": "admin", "username": "admin"}


# 退出登录

@app.post("/api/logout")
async def api_logout(token: str = __import__('fastapi').Query(...)):
    ok = _revoke_token(token)
    return {"ok": ok}


# WebSocket 端点
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    import asyncio

    token = ws.query_params.get("token", "")
    user_id = _validate_token(token)
    if not user_id:
        await ws.close(code=4001, reason="未授权：无效或过期的 token")
        return
    await ws.accept()
    console = WebSocketConsole(ws)
    session = _get_or_create_session(user_id)
    session.console = console
    agent = session.agent

    system_prompt = _build_system_prompt()

    tool_schemas = []
    for name in registry.get_all_names():
        tool = registry.get(name)
        if tool:
            tool_schemas.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            })

    agent.clear_history()
    # 清空上一会话残留的 event_bus 事件（todo/diff/message 等），避免跨会话泄漏
    console.drain_events()

    _drain_stop = threading.Event()

    async def _drain_loop():
        while not _drain_stop.is_set():
            # 轮询 Web 审批桥：有请求则推送给前端弹窗
            req = _approval_bridge.poll()
            while req is not None:
                await ws.send_json({"type": "approval_request", **req})
                req = _approval_bridge.poll()
            await console.drain_outgoing()
            await asyncio.sleep(0.05)

    drain_task = asyncio.create_task(_drain_loop())
    run_task: Optional[asyncio.Task] = None

    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type", "")

            if msg_type == "chat":
                if run_task and not run_task.done():
                    await ws.send_json({"type": "error", "content": "已有任务运行中"})
                    continue

                user_message = data.get("content", "").strip()
                if not user_message:
                    continue

                from tools.approval import clear_break
                clear_break()

                async def _run(_msg=user_message):
                    nonlocal run_task
                    try:
                        result = await asyncio.to_thread(
                            agent.run,
                            message_id=str(uuid.uuid4()),
                            user_message=_msg,
                            system_prompt=system_prompt,
                            console=console,
                            tool_schemas=tool_schemas,
                        )
                        await console.drain_outgoing()
                        await ws.send_json({
                            "type": "done",
                            "content": result.get("final_response", "") if result else "",
                        })
                    except Exception as e:
                        await ws.send_json({"type": "error", "content": str(e)})
                    finally:
                        await ws.send_json({"type": "status", "phase": "idle", "text": "就绪"})
                        run_task = None

                run_task = asyncio.create_task(_run())

            elif msg_type == "approval_response":
                aid = data.get("id", "")
                approved = bool(data.get("approved", False))
                break_loop = bool(data.get("break", False))
                _approval_bridge.respond(aid, approved, break_loop)

            elif msg_type == "interrupt":
                agent.interrupt()
                await ws.send_json({"type": "interrupted"})

            elif msg_type == "new":
                # 保存当前会话 → 清除历史 → 通知前端
                session_id = agent.save_conversation()
                agent.clear_history()
                content = f"本次全程对话已保存，session_id: {session_id}" if session_id else "本次对话无内容，无需保存"
                await ws.send_json({
                    "type": "session_saved",
                    "session_id": session_id,
                    "content": content,
                })
                await ws.send_json({"type": "status", "phase": "idle", "text": "就绪"})

            elif msg_type == "clear":
                agent.clear_history()
                await ws.send_json({"type": "cleared"})
                await ws.send_json({"type": "status", "phase": "idle", "text": "就绪"})

    except WebSocketDisconnect:
        pass
    finally:
        _drain_stop.set()
        if drain_task and not drain_task.done():
            drain_task.cancel()
        if run_task and not run_task.done():
            run_task.cancel()
        # 断开时保存会话
        try:
            session_id = agent.save_conversation()
            if session_id:
                await ws.send_json({
                    "type": "session_saved",
                    "session_id": session_id,
                    "content": f"本次全程对话已保存，session_id: {session_id}",
                })
        except Exception:
            pass  # 静默忽略保存失败（ws 可能已断开）


# ── 构建 system prompt ──────────────────────────────────────────────────

def _build_system_prompt() -> str:
    """统一 system prompt 构建。Gateway 为单人模式，不按 user_id 隔离。"""
    from config.system_prompt import SystemPromptBuilder
    builder = SystemPromptBuilder()
    return builder.build()


# ── 启动入口 ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9090, help="监听端口")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="监听地址")
    args = parser.parse_args()

    print(f"\n  Jify 启动中...")
    print(f"  登录: http://localhost:{args.port}")
    print(f"  聊天: http://localhost:{args.port}/chat\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
