# -*- coding: utf-8 -*-
"""builtin tool — shell 命令执行（白名单 + 审批 + 超时）"""

import json
import subprocess
from typing import Optional

from tools.registry import register_tool
from tools.builtin.allowlist import check_allowlist, is_file_modifying

DEFAULT_TIMEOUT = 30
MAX_OUTPUT_BYTES = 102400  # 100KB


@register_tool(
    name="exec",
    description="Execute a shell command on the local system",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "workdir": {"type": "string", "description": "Working directory"},
        },
        "required": ["command"],
    },
    parallel_safe=False,
    requires_approval=False,
)
def exec_tool(command: str, workdir: Optional[str] = None) -> str:
    """Execute shell command with allowlist + approval + timeout"""

    # 白名单检查
    allowed, needs_approval, reason, parsed = check_allowlist(command)
    if not allowed:
        return json.dumps({
            "error": f"命令被拒绝: {reason}",
            "blocked": True,
        })

    # 审批流程
    approved = True
    if needs_approval or is_file_modifying(command):
        from tools.approval import request_approval, ApprovalBreak
        try:
            approved = request_approval("exec", {"command": command, "workdir": workdir})
        except ApprovalBreak:
            return json.dumps({
                "error": "exec 被用户中断 (break)。",
                "approval_break": True,
            })
        if not approved:
            return json.dumps({
                "error": "exec 命令被用户拒绝执行，请调整方案。",
                "approval_denied": True,
            })

    # 执行
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=workdir or None,
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT,
        )

        stdout = (proc.stdout or "")[:MAX_OUTPUT_BYTES]
        stderr = (proc.stderr or "")[:MAX_OUTPUT_BYTES]

        if len(stdout) == MAX_OUTPUT_BYTES:
            stdout += "\n... [输出已截断]"
        if len(stderr) == MAX_OUTPUT_BYTES:
            stderr += "\n... [输出已截断]"

        output = stdout.strip() if stdout else stderr.strip()
        return output or f"(exit {proc.returncode})"

    except subprocess.TimeoutExpired:
        return json.dumps({
            "error": f"命令超时 ({DEFAULT_TIMEOUT}s)",
            "timed_out": True,
        })
    except FileNotFoundError:
        return json.dumps({"error": "找不到 shell 或命令"})
    except Exception as e:
        return json.dumps({"error": f"执行异常: {e}"})
