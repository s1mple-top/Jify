"""Jify 更新检测 —— 启动时对比本地与远程 pyproject.toml 的 version，用于 banner 提示新版本。"""

from __future__ import annotations

import logging
import tomllib
from typing import Optional

import requests

logger = logging.getLogger(__name__)

REPO = "s1mple-top/Jify"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}"
HTTP_TIMEOUT = 3.0


def get_current_version() -> str:
    """返回当前安装的 Jify 版本，取不到时回退为 '0.0.0'。"""
    try:
        import importlib.metadata as metadata
        return metadata.version("jify")
    except Exception:  # noqa: BLE001
        return "0.0.0"


def fetch_latest_version() -> Optional[str]:
    """读取远程仓库 main 分支 pyproject.toml 里的 version 字段。失败返回 None。"""
    url = f"{RAW_BASE}/main/pyproject.toml"
    try:
        resp = requests.get(url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = tomllib.loads(resp.text)
        version = (data.get("project") or {}).get("version")
        return version or None
    except Exception as exc:  # noqa: BLE001
        logger.debug("fetch remote version failed: %s", exc)
        return None


def _parse_version(version: str) -> tuple:
    """把 '0.1.0' / 'v0.2.0' / '0.2.0a1' 转成可比较的数字元组。"""
    parts = []
    for seg in (version or "").split("."):
        digits = ""
        for ch in seg:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def check_update() -> Optional[str]:
    """远程版本高于本地版本时返回远程版本号，否则返回 None。"""
    latest = fetch_latest_version()
    if not latest:
        return None

    try:
        newer = _parse_version(latest) > _parse_version(get_current_version())
    except Exception:  # noqa: BLE001
        return None

    return latest if newer else None
