"""Auth / 用户管理 —— 从 gateway.py 提取。"""

import os
import secrets
import threading
import time
from pathlib import Path
from typing import Dict, Optional

import yaml
from pydantic import BaseModel

# Token 状态

_tokens: Dict[str, dict] = {}
_tokens_lock = threading.Lock()
_TOKEN_EXPIRE_SECONDS = 24 * 3600


# Token 管理

def _create_token(username: str, user_id: str) -> str:
    token = secrets.token_hex(32)
    with _tokens_lock:
        _tokens[token] = {
            "user_id": user_id,
            "username": username,
            "created_at": time.time(),
        }
    return token


def _validate_token(token: str) -> Optional[str]:
    with _tokens_lock:
        entry = _tokens.get(token)
        if not entry:
            return None
        if time.time() - entry["created_at"] > _TOKEN_EXPIRE_SECONDS:
            del _tokens[token]
            return None
        return entry["user_id"]


def _revoke_token(token: str) -> bool:
    """删除指定 token，用于主动退出登录。返回是否成功删除。"""
    with _tokens_lock:
        if token in _tokens:
            del _tokens[token]
            return True
        return False


def _get_admin_token() -> str:
    try:
        with open(os.path.expanduser("~/.jify/config.yaml")) as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("admin_token", "")
    except Exception:
        return ""


# 登录失败限速（内存计数，按来源 IP 滑动窗口）

_LOGIN_MAX_FAILS = 5
_LOGIN_WINDOW_SECONDS = 60
_login_fails: Dict[str, list] = {}
_login_lock = threading.Lock()


def _login_fails_recent(ip: str) -> list:
    """返回窗口内的失败时间戳，同时清理过期记录。"""
    now = time.time()
    with _login_lock:
        stamps = [t for t in _login_fails.get(ip, []) if now - t < _LOGIN_WINDOW_SECONDS]
        if stamps:
            _login_fails[ip] = stamps
        else:
            _login_fails.pop(ip, None)
        return stamps


def is_login_rate_limited(ip: str) -> bool:
    """窗口内失败次数达到上限则拒绝，返回 True 表示已限速。"""
    return len(_login_fails_recent(ip)) >= _LOGIN_MAX_FAILS


def record_login_failure(ip: str) -> None:
    now = time.time()
    with _login_lock:
        _login_fails.setdefault(ip, []).append(now)


def clear_login_failures(ip: str) -> None:
    with _login_lock:
        _login_fails.pop(ip, None)


# 请求模型

class AdminLoginRequest(BaseModel):
    token: str
