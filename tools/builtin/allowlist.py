# -*- coding: utf-8 -*-
"""
命令白名单机制 — 取代旧的黑名单模式

命令分三类：
  - 只读/无害命令 → 直接放行
  - 危险命令       → 需用户审批
  - 不在白名单     → 需用户审批
"""

import os
import re
import shlex

# ═══════════════════════════════════════════════════════════
# 白名单命令集
# ═══════════════════════════════════════════════════════════

ALLOWED_READONLY_COMMANDS = {
    # 文件浏览与信息
    'ls', 'find', 'tree', 'file', 'stat', 'du', 'df',
    # 文本查看与搜索
    'cat', 'head', 'tail', 'less', 'more', 'grep', 'rg',
    # 文本统计与比较
    'wc', 'sort', 'uniq', 'diff', 'cmp', 'comm',
    # 路径与标识
    'pwd', 'dirname', 'basename', 'realpath', 'readlink', 'which', 'type',
    # 环境与进程信息
    'env', 'printenv', 'date', 'whoami', 'hostname', 'uname', 'id',
    # 基础输出
    'echo', 'printf', 'true', 'false', 'test', '[', 'sleep',
    # 网络诊断（只读）
    'curl', 'wget',
    # Python
    'python', 'python3', '.venv/bin/python',
}

ALLOWED_DANGEROUS_COMMANDS = {
    'mkdir', 'touch', 'cp', 'mv', 'rm', 'rmdir',
    'chmod', 'chown', 'ln', 'tar', 'gzip', 'gunzip', 'zip', 'unzip',
    'dd', 'sed', 'awk', 'tee', 'tr', 'cut',
    'pip', 'pip3', 'npm', 'yarn',
    'git',
}

GIT_SAFE_SUBCOMMANDS = {
    'status', 'log', 'diff', 'branch', 'tag', 'show',
    'rev-parse', 'rev-list', 'ls-files', 'ls-tree',
    'remote', 'stash',
}

# 文件修改重定向符 — 命中则触发审批
REDIRECT_PATTERNS = [
    r'(?<!\\)>>?(?!&)',   # > 和 >> （排除 >&）
    r'(?<!\\)\|',         # 管道
    r'(?<!\\)\$\(',       # 命令替换
    r'(?<!\\)`',          # 反引号命令替换
]

# ═══════════════════════════════════════════════════════════════
# safe_exec 全局开关 — 由 CLI --safe-exec 参数控制
# ═══════════════════════════════════════════════════════════════

_safe_exec = False


def set_safe_exec(value: bool) -> None:
    """设置白名单模式开关。True = 启用白名单检查，False = 放行所有命令。"""
    global _safe_exec
    _safe_exec = value


# 白名单检查

def check_allowlist(command: str):
    """检查命令是否在白名单中

    Returns:
        (allowed, needs_approval, reason, parsed_command)
        - allowed: bool      是否允许执行
        - needs_approval: bool 是否需要用户审批
        - reason: str        拒绝/审批原因
        - parsed_command: str 解析出的第一层命令名
    """
    cmd = command.strip()
    if not cmd:
        return (False, False, "空命令", "")

    parsed = _parse_command_name(cmd)
    if not parsed:
        return (False, False, "无法解析命令", "")

    # 白名单模式未启用：放行所有命令，不审批
    if not _safe_exec:
        return (True, False, "", parsed)

    # 检测重定向 — 文件修改行为
    for pattern in REDIRECT_PATTERNS:
        if re.search(pattern, cmd):
            return (True, True, f"命令包含重定向/管道/命令替换，可能修改文件", parsed)

    # git 特殊处理
    if parsed == "git":
        return _check_git_command(cmd)

    # 只读命令 — 直接放行
    if parsed in ALLOWED_READONLY_COMMANDS:
        return (True, False, "", parsed)

    # 危险命令 — 需审批
    if parsed in ALLOWED_DANGEROUS_COMMANDS:
        return (True, True, f"命令 '{parsed}' 可能修改文件，需要审批", parsed)

    # 不在任何白名单中 — 需审批
    return (True, True, f"命令 '{parsed}' 不在允许列表中，需要审批", parsed)


def _parse_command_name(command: str) -> str:
    """提取命令的第一层名称"""
    try:
        parts = shlex.split(command.strip())
        if not parts:
            return ""
        name = parts[0].rstrip(";|&")
        # 处理 sudo / env / 路径前缀
        if name in ("sudo",):
            if len(parts) > 1:
                name = parts[1].rstrip(";|&")
        name = os.path.basename(name)
        return name
    except ValueError:
        first = command.strip().split()[0] if command.strip().split() else ""
        return os.path.basename(first.rstrip(";|&"))


def _check_git_command(command: str):
    """git 命令细分检查"""
    try:
        parts = shlex.split(command.strip())
        if len(parts) < 2:
            return (True, False, "", "git")

        subcmd = parts[1]
        if subcmd in GIT_SAFE_SUBCOMMANDS:
            return (True, False, "", "git")
        else:
            return (True, True, f"git {subcmd} 可能修改仓库状态，需要审批", "git")
    except ValueError:
        return (True, True, "git 命令需要审批", "git")


def is_file_modifying(command: str) -> bool:
    """判断命令是否会修改文件（向后兼容接口）"""
    allowed, _, _, _ = check_allowlist(command)
    if not allowed:
        return True
    _, needs_approval, _, _ = check_allowlist(command)
    if needs_approval:
        return True
    return False
