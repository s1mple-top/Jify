# -*- coding: utf-8 -*-
"""builtin tool — 漏洞黑盒验证（强制审批）

对静态分析发现的安全漏洞做本地黑盒实测验证。
所有验证请求都强制经过用户审批（requires_approval=True），审批面板展示：
  验证类型 / 目标 / Payload / 预期结果

安全约束：
  - 只允许请求本地地址（localhost/127.0.0.1），禁止对第三方系统发包
  - 拒绝明显破坏性 Payload（rm -rf / dd / mkfs / shutdown 等）
  - 默认不跟随重定向，防止被带到外部地址
"""

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import requests

from tools.registry import register_tool

VALID_VULN_TYPES = [
    "rce", "sqli", "xss", "ssrf", "path_traversal",
    "deserialization", "csrf", "idor", "auth_bypass",
    "info_disclosure", "other",
]

DANGER_LEVELS = {
    "rce": "高", "sqli": "高", "deserialization": "高",
    "csrf": "高", "auth_bypass": "高",
    "xss": "中", "ssrf": "中", "idor": "中", "path_traversal": "中",
    "info_disclosure": "低", "other": "中",
}

VALID_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}

MAX_BODY_LEN = 4000        # 返回给模型的响应体截断上限
REQUEST_TIMEOUT = 15       # 秒；时间盲注等超时探测依赖此值
CALLBACK_PLACEHOLDER = "{{CALLBACK}}"

# 明显破坏性 Payload 拦截（RCE/反序列化验证常见）
_DESTRUCTIVE_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\bdd\s+if=",
    r"\bmkfs\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bpoweroff\b",
    r">\s*/dev/sd",
    r"format\s+C:",
    r"\{\s*:\s*\}\s*\(\s*\)\s*\{",  # fork bomb :(){ :|:& };:
]
_DESTRUCTIVE_RE = re.compile("|".join(_DESTRUCTIVE_PATTERNS), re.IGNORECASE)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "127.0.0.2"}


def _is_local_target(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    return host in _LOCAL_HOSTS or host.startswith("127.")


# ═══════════════════════════════════════════════════════════
# 审批预览 — 展示「验证类型 / 目标 / Payload / 预期结果」
# ═══════════════════════════════════════════════════════════

def _preview_vuln_verify(vuln_type=None, target=None, method=None, payload=None,
                         expected_outcome=None, data=None, headers=None, **kwargs):
    vuln_type = vuln_type or "other"
    lines = [
        f"验证类型   : {vuln_type}（危险度: {DANGER_LEVELS.get(vuln_type, '?')}）",
        f"目标       : {target or '(未填写)'}",
        f"方法       : {method or 'GET'}",
        f"Payload    : {payload or '(无)'}",
    ]
    if data:
        lines.append(f"请求体     : {data}")
    if expected_outcome:
        lines.append(f"预期结果   : {expected_outcome}")
    if target and not _is_local_target(target):
        lines.append(f"⚠ 警告     : 目标不是本地地址，请确认不会影响第三方系统")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# 本地回调监听（SSRF 回连验证）
# ═══════════════════════════════════════════════════════════

class _CallbackHandler(BaseHTTPRequestHandler):
    def _record(self, body: bytes = b""):
        self.server.hits.append({
            "method": self.command,
            "path": self.path,
            "body": body.decode("utf-8", "replace")[:200],
            "time": time.time(),
        })
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self):
        self._record()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        self._record(self.rfile.read(length) if length else b"")

    def log_message(self, *args):
        pass


def _start_listener():
    """启动本地回调监听，返回 (server, port)"""
    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.hits = []
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, server.server_address[1]


# ═══════════════════════════════════════════════════════════
# 主工具
# ═══════════════════════════════════════════════════════════

@register_tool(
    name="vuln_verify",
    description=(
        "对静态分析发现的漏洞进行本地黑盒实测验证（强制审批）。"
        "参数 target 为完整请求 URL，payload 需由你按注入点构造并 URL 编码嵌入 target；"
        "payload 字段用于审批展示与回显检测。"
        "SSRF 验证时在 target 或 payload 中使用 {{CALLBACK}} 占位符，工具会自动替换为本地监听地址。"
        "只允许请求本地地址（localhost/127.0.0.1），禁止破坏性 Payload，默认不跟随重定向。"
        "返回状态码、响应体、耗时，以及针对漏洞类型的检测信号。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "vuln_type": {
                "type": "string",
                "enum": VALID_VULN_TYPES,
                "description": "漏洞维度，决定审批面板展示与检测信号",
            },
            "target": {
                "type": "string",
                "description": "完整请求 URL（已嵌入 payload 并 URL 编码），必须是本地地址",
            },
            "payload": {
                "type": "string",
                "description": "注入的 Payload，用于审批展示与回显/反射检测",
            },
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
                "description": "请求方法，默认 GET",
            },
            "expected_outcome": {
                "type": "string",
                "description": "预期结果：漏洞确认时应观察到的现象（如命令回显、时间延迟、文件内容）",
            },
            "data": {
                "type": "string",
                "description": "请求体（POST/PUT 等），非 GET 时使用",
            },
            "headers": {
                "type": "object",
                "description": "额外请求头",
            },
        },
        "required": ["vuln_type", "target", "payload"],
    },
    parallel_safe=False,
    requires_approval=True,
    preview_handler=_preview_vuln_verify,
)
def vuln_verify(vuln_type: str, target: str, payload: str, method: str = "GET",
                expected_outcome: str = "", data: str = "",
                headers: dict = None) -> dict:
    """执行一次漏洞黑盒验证，返回验证证据。"""

    # ── 参数校验 ──
    if vuln_type not in VALID_VULN_TYPES:
        return {"success": False, "error": f"无效的 vuln_type '{vuln_type}'，可选: {', '.join(VALID_VULN_TYPES)}"}
    if not target or not target.startswith(("http://", "https://")):
        return {"success": False, "error": "target 必须是完整的 http(s) URL"}
    method = (method or "GET").upper()
    if method not in VALID_METHODS:
        return {"success": False, "error": f"无效的 method '{method}'"}

    # ── 安全约束 ──
    if not _is_local_target(target):
        return {"success": False, "error": "拒绝：target 不是本地地址，黑盒验证仅允许 localhost/127.0.0.1"}
    for candidate in (payload or "", data or ""):
        m = _DESTRUCTIVE_RE.search(candidate)
        if m:
            return {"success": False, "error": f"拒绝：Payload 包含破坏性操作 '{m.group(0)}'，请使用无副作用载荷"}

    # ── SSRF 回调监听 ──
    listener = None
    if vuln_type == "ssrf":
        listener, port = _start_listener()
        callback_url = f"http://127.0.0.1:{port}/callback"
        target = target.replace(CALLBACK_PLACEHOLDER, callback_url)
        payload = payload.replace(CALLBACK_PLACEHOLDER, callback_url)

    request_headers = dict(headers or {})
    request_headers.setdefault("User-Agent", "Jify-VulnVerify/1.0")

    # ── 发送请求 ──
    started = time.monotonic()
    try:
        resp = requests.request(
            method, target, data=data or None, headers=request_headers,
            timeout=REQUEST_TIMEOUT, allow_redirects=False,
        )
        elapsed = round(time.monotonic() - started, 3)
        body = resp.text or ""
        if len(body) > MAX_BODY_LEN:
            body = body[:MAX_BODY_LEN] + "\n... [响应已截断]"
    except requests.exceptions.Timeout:
        elapsed = round(time.monotonic() - started, 3)
        return {
            "success": True, "data": {
                "status": "timeout", "elapsed_sec": elapsed,
                "evidence": f"请求超时（>{REQUEST_TIMEOUT}s），疑似时间型注入/阻塞。",
                "vuln_type": vuln_type, "target": target,
            },
        }
    except requests.RequestException as e:
        return {"success": False, "error": f"请求失败: {e}"}
    finally:
        # 等回调（SSRF 回连可能需要时间）
        if listener is not None:
            deadline = time.monotonic() + 3.0
            while not listener.hits and time.monotonic() < deadline:
                time.sleep(0.1)
            listener.shutdown()

    # ── 检测信号（按漏洞维度） ──
    signals = {}
    low_body = body.lower()

    signals["reflected"] = bool(payload) and payload.lower() in low_body

    if vuln_type == "path_traversal":
        signals["file_content_leaked"] = any(
            kw in low_body for kw in ["root:x:", "/bin/bash", "daemon:x:", "nobody:x:"]
        )
    if vuln_type in ("rce", "sqli"):
        signals["time_delay"] = elapsed >= 2.5
    if vuln_type == "xss":
        signals["markup_injected"] = "<script" in low_body or "onerror=" in low_body
    if vuln_type == "info_disclosure":
        signals["stack_trace"] = "traceback" in low_body or "at " in low_body and "error" in low_body

    result = {
        "success": True,
        "data": {
            "vuln_type": vuln_type,
            "request": {"method": method, "url": target, "body": data or None},
            "status_code": resp.status_code,
            "response_body": body,
            "elapsed_sec": elapsed,
            "signals": signals,
            "expected_outcome": expected_outcome,
        },
    }
    if listener is not None:
        result["data"]["callback_hits"] = list(listener.hits)
        result["data"]["ssrf_confirmed"] = len(listener.hits) > 0

    return result
