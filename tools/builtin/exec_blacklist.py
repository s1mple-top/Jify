# -*- coding: utf-8 -*-
"""
exec 工具安全策略 — 白名单模式 需要用户手动启动jify时开启

is_file_modifying() 向后兼容接口，内部委托给 allowlist.check_allowlist()。
"""

from tools.builtin.allowlist import check_allowlist, is_file_modifying

__all__ = ["is_file_modifying", "check_allowlist"]
