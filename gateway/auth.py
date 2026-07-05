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


# 请求模型

class AdminLoginRequest(BaseModel):
    token: str
