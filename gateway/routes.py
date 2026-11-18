"""FastAPI routes / WebSocket / 聊天处理 —— 从 gateway.py 提取。"""

import asyncio
import json
import logging
import os
import time
import uuid
import threading
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass, field

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from agent_loop import AgentLoop, AgentConfig
from jify_tool import registry
from tools.registry import build_tool_schemas
from version_check import get_current_version

from .auth import (
    _create_token,
    _validate_token,
    _revoke_token,
    _get_admin_token,
    AdminLoginRequest,
    is_login_rate_limited,
    record_login_failure,
    clear_login_failures,
)
from .ws_console import WebSocketConsole
from config.system_prompt import _discover_skills
from plugins.loader import PluginLoader
from tools.approval import ApprovalBridge, set_web_bridge

logger = logging.getLogger(__name__)

# 应用初始化

app = FastAPI(title="Jify")

static_dir = Path(__file__).parent.parent / "gateway_web"
static_dir.mkdir(exist_ok=True)

# 静态资源（如 vendored 的 marked.js）本地挂载，避免依赖外部 CDN 可达性
try:
    from fastapi.staticfiles import StaticFiles
    _vendor_dir = static_dir / "vendor"
    _vendor_dir.mkdir(exist_ok=True)
    app.mount("/static/vendor", StaticFiles(directory=_vendor_dir), name="vendor")
except Exception:
    pass

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

# MCP 加载（后台线程：逐个 server 建连可能耗时数十秒，不能阻塞 uvicorn 启动）。
# 工具注册进 registry 后即可用，因为工具 schema 在每条 chat 消息处理时实时构建，
# 加载完成前已建立的 WebSocket 连接也能看到新工具。
try:
    from tools.mcp.manager import mcp_manager

    if mcp_manager.load_servers() > 0:
        mcp_manager.load_async()
except Exception:
    pass

# 会话管理

@dataclass
class GatewaySession:
    session_id: str
    user_id: str
    agent: AgentLoop
    console: Any
    title: str = ""
    messages: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)

    @property
    def active(self) -> bool:
        """是否有 WS 连接正在使用。活跃会话不参与回收，否则会打断正在进行的对话。"""
        return self.console is not None


_sessions: Dict[str, GatewaySession] = {}
_sessions_lock = threading.Lock()

# 内存会话回收参数（环境变量可覆盖，便于部署时按机器内存调整）
# 单个会话压着一个 AgentLoop 的完整上下文，长对话可达数十 MB，故必须设上限。
_SESSION_MAX = int(os.environ.get("JIFY_GATEWAY_MAX_SESSIONS", "20"))
_SESSION_IDLE_TTL = float(os.environ.get("JIFY_GATEWAY_SESSION_TTL", str(30 * 60)))
_SWEEP_INTERVAL = float(os.environ.get("JIFY_GATEWAY_SWEEP_INTERVAL", "60"))
_sweeper_started = False
_sweeper_lock = threading.Lock()


def _sweep_sessions_locked() -> list:
    """回收超限或超时的内存会话，返回被移除的会话列表。

    调用方必须持有 _sessions_lock。被移除的会话需由调用方在**锁外**落库后丢弃
    （见 _flush_evicted），避免持锁做 SQLite IO 拖住其他请求。

    跳过 active 会话（有 WS 连接）；正在跑的会话 console 必然存在，因此天然受保护。
    """
    now = time.time()
    evicted: list = []

    # 1) 空闲超时：长时间没被访问的直接移除
    for sid, s in list(_sessions.items()):
        if s.active:
            continue
        if now - s.last_used > _SESSION_IDLE_TTL:
            _sessions.pop(sid, None)
            evicted.append(s)

    # 2) 数量上限：按最近使用时间淘汰最久未用的（LRU）
    overflow = len(_sessions) - _SESSION_MAX
    if overflow > 0:
        candidates = sorted(
            (s for s in _sessions.values() if not s.active),
            key=lambda s: s.last_used,
        )
        for s in candidates[:overflow]:
            _sessions.pop(s.session_id, None)
            evicted.append(s)

    return evicted


def _flush_evicted(evicted: list) -> None:
    """锁外把被淘汰的会话落库。会话数据在 SQLite 里，之后用户点回来会走 DB 恢复重建。

    落库失败不影响回收：会话本身已从内存移除，DB 里仍是上次保存的状态。
    """
    for s in evicted:
        try:
            s.agent.save_conversation()
        except Exception:
            logger.debug("evict save failed: %s", s.session_id, exc_info=True)


def _start_sweeper() -> None:
    """启动后台回收线程（懒启动，仅 gateway 进程内会触发）。让空闲 TTL 不依赖新会话创建。"""
    global _sweeper_started
    with _sweeper_lock:
        if _sweeper_started:
            return
        _sweeper_started = True

    def _loop():
        while True:
            time.sleep(_SWEEP_INTERVAL)
            try:
                with _sessions_lock:
                    evicted = _sweep_sessions_locked()
                _flush_evicted(evicted)
            except Exception:
                logger.debug("session sweep failed", exc_info=True)

    threading.Thread(target=_loop, name="jify-session-sweeper", daemon=True).start()


def _create_session(user_id: str, session_id: Optional[str] = None) -> GatewaySession:
    config = AgentConfig.load_from_yaml()
    agent = AgentLoop(agent_config=config)
    _ensure_team_initialized(agent, config)
    restored = None
    if session_id:
        # 从 DB 恢复历史会话：注入 resume 上下文并接管 DB session_id，
        # 后续保存走全量覆盖语义，恢复前的轮次不会丢
        try:
            restored = agent.load_conversation(session_id)
        except Exception:
            restored = None
            session_id = None  # 恢复失败（DB 无此会话/损坏）降级为全新会话
    if not session_id:
        # 新建会话：统一使用 DB 8-hex 会话 id（_ensure_session 幂等，失败降级 uuid）
        session_id = agent._ensure_session() or uuid.uuid4().hex[:8]
    session = GatewaySession(
        session_id=session_id,
        user_id=user_id,
        agent=agent,
        console=None,
    )
    if restored:
        first_user = next((m.content for m in restored if m.role == "user"), "")
        session.title = (first_user or "").strip().replace("\n", " ")[:20] or "历史会话"
    _sessions[session_id] = session
    return session


def _get_or_create_session(user_id: str, session_id: Optional[str] = None) -> GatewaySession:
    evicted: list = []
    with _sessions_lock:
        if session_id and session_id in _sessions:
            session = _sessions[session_id]
            if session.user_id == user_id:
                # 刷新活跃时间，避免正在用的会话被 TTL 误回收
                session.last_used = time.time()
                return session
        session = None
        if session_id:
            # 进程重启/页面刷新后内存无此会话，但 DB 有 → 恢复
            from storage.db import get_db
            try:
                if get_db().get_session(session_id) is not None:
                    session = _create_session(user_id, session_id)
            except Exception:
                pass  # DB 不可用时降级为新建
        if session is None:
            session = _create_session(user_id)
        evicted = _sweep_sessions_locked()
    # 落库放锁外：SQLite 写入不应阻塞其他会话操作
    _flush_evicted(evicted)
    _start_sweeper()
    return session


def _list_sessions(user_id: str) -> list:
    with _sessions_lock:
        items = [s for s in _sessions.values() if s.user_id == user_id]
        items.sort(key=lambda s: s.created_at)
        return items


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
        orch = TeamOrchestrator(lambda: agent.model_client, config)
        set_leader(orch.leader)
    except Exception:
        # 与顶部 plugin loader 一致：team 初始化失败不应阻断 gateway 启动
        pass


# 鉴权依赖

def _require_user(token: str = Query("")) -> str:
    """校验 query 中的 token，返回 user_id。

    走 Query 而非 Header 是为了与前端现有 fetch 调用保持一致（无需改动请求头）。
    缺省空串时返回 401 而非 422，便于前端统一处理未登录。
    """
    user_id = _validate_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="未授权：无效或过期的 token")
    return user_id


# 页面路由

@app.get("/")
async def index():
    html_path = static_dir / "login.html"
    if html_path.exists():
        # footer 版本号随 pyproject 走，避免硬编码过期
        html = html_path.read_text(encoding="utf-8").replace("__VERSION__", get_current_version())
        return HTMLResponse(html)
    return HTMLResponse("<h1>Jify Gateway</h1><p>gateway_web/login.html not found</p>")


@app.get("/chat")
async def chat_page():
    html_path = static_dir / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Jify Gateway</h1><p>gateway_web/index.html not found</p>")


@app.get("/api/skills")
async def api_skills(user_id: str = Depends(_require_user)):
    skills = _discover_skills()
    result = []
    for skill in skills:
        for name, desc in skill.items():
            result.append({"name": name, "description": desc})
    return {"skills": result}


@app.get("/api/plugins")
async def api_plugins(user_id: str = Depends(_require_user)):
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
async def api_get_config(user_id: str = Depends(_require_user)):
    """读取当前 config.yaml 并以结构化形式返回（敏感值只返回脱敏标记）。"""
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        else:
            raw = {}
    except Exception:
        raw = {}

    # 敏感值一律不返回明文，只给 *_masked 供前端展示
    # models 列表中的 api_key 同样移除，仅保留 api_key_masked
    models_raw = raw.get("models", [])
    models_masked = []
    for m in (models_raw if isinstance(models_raw, list) else []):
        if isinstance(m, dict):
            masked = dict(m)
            key = masked.pop("api_key", "")
            if key:
                masked["api_key_masked"] = _mask_sensitive(key)
            models_masked.append(masked)

    fields = {
        "provider": raw.get("provider", ""),
        "model": raw.get("model", ""),
        "base_url": raw.get("base_url", ""),
        "api_key_masked": _mask_sensitive(raw.get("api_key", "")),
        "max_iterations": raw.get("max_iterations", 500),
        "tool_delay": raw.get("tool_delay", 0.0),
        "max_workers": raw.get("max_workers", 8),
        "tool_timeout": raw.get("tool_timeout", 120.0),
        "SelfEvolutionModel": raw.get("SelfEvolutionModel", ""),
        "SelfEvolutionTurn": raw.get("SelfEvolutionTurn", 8),
        "context_compress_threshold": raw.get("context_compress_threshold", 1800000),
        "admin_token_masked": _mask_sensitive(raw.get("admin_token", "")),
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


# 敏感字段：提交值为空或带脱敏标记时一律跳过，避免用占位符覆盖真实密钥
_SECRET_FIELDS = ("api_key", "admin_token")
_MASK_MARK = "***"


def _is_secret_placeholder(value) -> bool:
    """判断提交的敏感字段值是否为占位符（空串或含脱敏标记）。"""
    return not isinstance(value, str) or value == "" or _MASK_MARK in value


@app.post("/api/config")
async def api_save_config(req: ConfigUpdateRequest, user_id: str = Depends(_require_user)):
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

    # 敏感字段回写保护：占位符跳过
    for key in _SECRET_FIELDS:
        if key in updates and _is_secret_placeholder(updates[key]):
            del updates[key]

    # models 列表中每个模型的 api_key 同样做占位符保护：
    # 前端只回传 api_key_masked，真实密钥按原列表位置保留
    models_update = updates.get("models")
    if isinstance(models_update, list):
        old_models = current.get("models") or []
        cleaned = []
        for i, m in enumerate(models_update):
            if not isinstance(m, dict):
                cleaned.append(m)
                continue
            item = dict(m)
            item.pop("api_key_masked", None)
            if _is_secret_placeholder(item.get("api_key")):
                item.pop("api_key", None)
                old_key = old_models[i].get("api_key") if i < len(old_models) and isinstance(old_models[i], dict) else None
                if old_key:
                    item["api_key"] = old_key
            cleaned.append(item)
        updates["models"] = cleaned

    current.update(updates)

    # 写回
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(current, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    return {"ok": True, "message": "配置已保存，重启 Gateway 生效"}


@app.post("/api/admin_login")
async def api_admin_login(req: AdminLoginRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if is_login_rate_limited(client_ip):
        raise HTTPException(status_code=429, detail="登录失败次数过多，请稍后再试")

    admin_token = _get_admin_token()
    if not admin_token:
        raise HTTPException(status_code=500, detail="未配置 admin_token")
    if req.token != admin_token:
        record_login_failure(client_ip)
        raise HTTPException(status_code=401, detail="管理员 token 错误")

    clear_login_failures(client_ip)
    token = _create_token("admin", "admin")
    return {"token": token, "user_id": "admin", "username": "admin"}


# 退出登录

@app.post("/api/logout")
async def api_logout(token: str = __import__('fastapi').Query(...)):
    ok = _revoke_token(token)
    return {"ok": ok}


# 会话管理 REST API（按 user 隔离；列表合并内存 live 会话与 DB 持久会话）

@app.get("/api/sessions")
async def api_list_sessions(token: str = Query(...)):
    user_id = _validate_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="未授权：无效或过期的 token")

    # DB 持久会话（标题取首条用户输入；message_count=0 的空壳会话不展示）
    db_sessions = []
    try:
        from storage.db import get_db
        db_sessions = [s for s in get_db().list_sessions(limit=50) if s["message_count"]]
    except Exception:
        db_sessions = []

    # 合并：live 会话的标题优先（含未保存的最新一轮），DB 补充进程重启前的历史
    seen = set()
    merged = []
    for s in _list_sessions(user_id):
        seen.add(s.session_id)
        merged.append({"id": s.session_id, "title": s.title or "新会话", "created_at": s.created_at, "live": True})
    for s in db_sessions:
        if s["id"] not in seen:
            title = (s["first_user_message"] or "").strip().replace("\n", " ")[:20] or "历史会话"
            merged.append({"id": s["id"], "title": title, "created_at": s["created_at"], "live": False})

    return {"sessions": merged}


@app.get("/api/sessions/{session_id}/messages")
async def api_get_session_messages(session_id: str, token: str = Query(...)):
    """返回 DB 中该会话的消息记录（刷新/切换会话时前端渲染历史用）"""
    user_id = _validate_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="未授权：无效或过期的 token")
    from storage.db import get_db
    try:
        messages = get_db().load_messages(session_id)
    except Exception:
        messages = []
    return {
        "messages": [
            {"role": m["role"], "content": m["content"]}
            for m in messages
        ]
    }


@app.post("/api/sessions")
async def api_create_session(token: str = Query(...)):
    user_id = _validate_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="未授权：无效或过期的 token")
    with _sessions_lock:
        session = _create_session(user_id)
        evicted = _sweep_sessions_locked()
    # 落库丢线程池：新建会话路径上不做同步 SQLite 写，避免冻结事件循环
    if evicted:
        await asyncio.to_thread(_flush_evicted, evicted)
    _start_sweeper()
    return {"id": session.session_id, "title": session.title or "新会话"}


@app.delete("/api/sessions/{session_id}")
async def api_delete_session(session_id: str, token: str = Query(...)):
    user_id = _validate_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="未授权：无效或过期的 token")
    with _sessions_lock:
        session = _sessions.get(session_id)
        if session and session.user_id != user_id:
            session = None
        if session:
            del _sessions[session_id]
    # 删除前保存，避免丢消息。先从字典摘除再在锁外写库：
    # 锁内只做字典操作，写 SQLite 的耗时 IO 丢进线程池，不冻结事件循环
    if session:
        try:
            await asyncio.to_thread(session.agent.save_conversation)
        except Exception:
            pass
    # 持久会话同步清 DB（非 live 会话只有 DB 记录；live 会话已保存，一并删除）
    try:
        from storage.db import get_db
        await asyncio.to_thread(get_db().delete_session, session_id)
    except Exception:
        pass
    return {"ok": True}


# WebSocket 端点
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    token = ws.query_params.get("token", "")
    user_id = _validate_token(token)
    if not user_id:
        # 必须先 accept 再 close：未 accept 就 close 时 Starlette 回 HTTP 403，
        # 浏览器侧只能看到 close code 1006，前端无法区分「token 失效」与「网络断开」。
        # 那样网关重启（内存 token 丢失）后前端会一直重连、一直失败，也不会提示重新登录。
        # accept 后立即 close 不交换任何业务数据，不影响安全性。
        await ws.accept()
        await ws.close(code=4001, reason="未授权：无效或过期的 token")
        return
    await ws.accept()
    console = WebSocketConsole(ws)
    session_id = ws.query_params.get("session_id", "")
    # 恢复路径含 SQLite 读取 + AgentLoop 构造（磁盘 IO），丢线程池避免冻结事件循环
    session = await asyncio.to_thread(_get_or_create_session, user_id, session_id)
    session.console = console
    agent = session.agent

    # 通知前端当前绑定会话（首次连接无 session_id 时由后端新建并回传 id）
    # 同时回传模型列表与当前激活项，供输入框模型选择栏使用
    await ws.send_json({
        "type": "session_ready",
        "session_id": session.session_id,
        "title": session.title or "新会话",
        "models": [m.name for m in agent.config.models],
        "active_model": agent.config.active_model_name or (agent.config.models[0].name if agent.config.models else ""),
    })

    system_prompt = _build_system_prompt()

    # 工具 schema 不在此处定快照：MCP 工具是后台线程加载的，可能在连接建立
    # 之后才注册完成。改为每处理一条 chat 消息时用 build_tool_schemas() 实时
    # 构建，使已建立的连接也能拿到新工具。

    # 多会话下每个会话保留独立历史，连接时不再 clear_history；
    # 仅清空上一会话残留的 event_bus 事件（todo/diff/message 等），避免跨会话泄漏
    console.drain_events()

    _drain_stop = threading.Event()

    async def _drain_loop():
        while not _drain_stop.is_set():
            # 轮询 Web 审批桥：只取本会话的请求，避免多标签页/多会话时弹窗串台
            req = _approval_bridge.poll(session.session_id)
            while req is not None:
                await ws.send_json({"type": "approval_request", **req})
                req = _approval_bridge.poll(session.session_id)
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

                # 首次消息时生成会话标题
                if not session.title:
                    session.title = user_message[:20]
                    await ws.send_json({"type": "session_title", "session_id": session.session_id, "title": session.title})

                from tools.approval import clear_break
                clear_break()

                # 每条 chat 消息实时取一次工具集：MCP 工具若在连接建立后才加载
                # 完成，本轮即可用上，无需用户重连。单轮内 agent.run 用同一份
                # 传入的列表，工具不会中途变化。
                tool_schemas = build_tool_schemas()

                async def _run(_msg=user_message, _schemas=tool_schemas):
                    nonlocal run_task
                    # 标注审批归属：asyncio.to_thread 会传播 contextvars，
                    # 工具线程里发起的审批因此带上本会话 id，只推给本连接
                    from tools.approval import set_approval_owner, reset_approval_owner
                    owner_token = set_approval_owner(session.session_id)
                    try:
                        result = await asyncio.to_thread(
                            agent.run,
                            message_id=str(uuid.uuid4()),
                            user_message=_msg,
                            system_prompt=system_prompt,
                            console=console,
                            tool_schemas=_schemas,
                        )
                        await console.drain_outgoing()
                        await ws.send_json({
                            "type": "done",
                            "content": result.get("final_response", "") if result else "",
                        })
                    except Exception as e:
                        await ws.send_json({"type": "error", "content": str(e)})
                    finally:
                        reset_approval_owner(owner_token)
                        await ws.send_json({"type": "status", "phase": "idle", "text": "就绪"})
                        run_task = None

                run_task = asyncio.create_task(_run())

            elif msg_type == "approval_response":
                aid = data.get("id", "")
                approved = bool(data.get("approved", False))
                break_loop = bool(data.get("break", False))
                _approval_bridge.respond(aid, approved, break_loop)

            elif msg_type == "switch_model":
                # 运行时切换当前会话使用的模型配置（不写盘，仅影响本 AgentLoop 实例）
                name = data.get("name", "")
                if run_task and not run_task.done():
                    await ws.send_json({"type": "error", "content": "任务运行中，无法切换模型"})
                    continue
                if agent.config.activate_model(name):
                    agent.clear_reasoning_content()
                    await ws.send_json({"type": "model_switched", "name": name, "model": agent.config.model})
                else:
                    await ws.send_json({"type": "error", "content": f"未找到模型配置: {name}"})

            elif msg_type == "interrupt":
                agent.interrupt()
                await ws.send_json({"type": "interrupted"})

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
        # 解除活跃标记：active 为 True 的会话不参与回收，不清就会永远驻留内存。
        # 仅当仍是本连接的 console 时才清 —— 同一会话可能被另一个标签页接管，
        # 此时 session.console 已指向新连接的 console，不能被本连接清掉。
        if session.console is console:
            session.console = None
        # 断开时保存会话：写库丢进线程池，避免长会话序列化+全量重写冻结事件循环
        session_id = None
        try:
            session_id = await asyncio.to_thread(agent.save_conversation)
        except asyncio.CancelledError:
            # 服务关停/任务取消时线程池 await 无法完成，退回同步保存兜底一次，
            # 代价是关停瞬间阻塞（此时已无并发请求），换取最后一轮不丢
            try:
                session_id = agent.save_conversation()
            except Exception:
                session_id = None
        except Exception:
            pass  # 静默忽略保存失败（ws 可能已断开）
        if session_id:
            try:
                await ws.send_json({
                    "type": "session_saved",
                    "session_id": session_id,
                    "content": f"本次全程对话已保存，session_id: {session_id}",
                })
            except Exception:
                pass  # ws 已断开，通知发不出去属正常



# 构建 system prompt
def _build_system_prompt() -> str:
    """统一 system prompt 构建。Gateway 为单人模式，不按 user_id 隔离。"""
    from config.system_prompt import SystemPromptBuilder
    builder = SystemPromptBuilder()
    return builder.build()


if __name__ == "__main__":
    # 唯一启动入口是 cli/app.py 的 gateway 子命令。此块仅作提示。
    import sys
    print("请使用 Jify 的 gateway 子命令启动服务：\n"
          "  jify gateway [--port 9090] [--host 127.0.0.1]", file=sys.stderr)
    sys.exit(1)
