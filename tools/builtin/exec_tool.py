# -*- coding: utf-8 -*-
"""builtin tool — shell 命令执行（白名单 + 审批 + 超时）"""

import json
import os
import signal
import subprocess
import time
from typing import Optional

from tools.registry import register_tool
from tools.builtin.allowlist import check_allowlist, is_file_modifying
from tools.call_context import register_killer, is_cancelled

DEFAULT_TIMEOUT = 30
MAX_OUTPUT_BYTES = 102400  # 100KB
_KILL_GRACE = 2.0  # SIGTERM 后等待优雅退出的秒数，超时补 SIGKILL


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
    # 该工具内部会自行调用 request_approval（最长等待 120s），加命令执行 30s，
    # 故不能落在 15s 默认档，否则用户还在审批时外层已报超时、命令却照旧执行
    timeout=180,
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
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=workdir or None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )
    except FileNotFoundError:
        return json.dumps({"error": "找不到 shell 或命令"})
    except Exception as e:
        return json.dumps({"error": f"执行异常: {e}"})

    # start_new_session=True 使 shell 成为新会话/进程组组长，pgid == pid。
    # 必须 kill 整个进程组：命令里的 `a && b`、`sh -c` 派生出的孙进程与 shell 同组，
    # 只杀 shell 会留下孤儿进程继续跑（实测 sleep 300 存活）。
    def _kill_group() -> bool:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return True  # 组已不存在，等于已终止
        except Exception:
            return False
        deadline = time.time() + _KILL_GRACE
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        # 组长可能已退出但组内仍有孙进程，补一刀；组不存在则忽略
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            return False
        return True

    pgid = proc.pid
    # 登记给外层：工具自身超时之外，调用超时时也会由外层触发清理
    register_killer(f"exec(pgid={pgid})", _kill_group)

    try:
        stdout, stderr = proc.communicate(timeout=DEFAULT_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_group()
        try:
            proc.communicate(timeout=1)  # 排空管道，避免 fd 泄漏
        except Exception:
            pass
        return json.dumps({
            "error": (
                f"命令超时（{DEFAULT_TIMEOUT}s），已终止其进程组（pgid={pgid}）及全部子进程。"
                "命令可能已执行到中途，副作用可能部分发生。"
            ),
            "timed_out": True,
            "terminated": True,
        })
    except Exception as e:
        _kill_group()
        return json.dumps({"error": f"执行异常: {e}"})

    stdout = (stdout or "")[:MAX_OUTPUT_BYTES]
    stderr = (stderr or "")[:MAX_OUTPUT_BYTES]

    # 被信号终止时说明是被外部取消（killer 发 SIGTERM），不是命令自身失败。
    # 若只说 (exit -15)，模型会误判为普通错误并重试。
    if proc.returncode is not None and proc.returncode < 0 and is_cancelled():
        return json.dumps({
            "error": (
                "调用已被取消，命令收到 SIGTERM 后终止。命令可能已执行到中途，"
                "副作用可能部分发生。"
            ),
            "cancelled": True,
            "terminated": True,
            "exit_code": proc.returncode,
        })

    if len(stdout) == MAX_OUTPUT_BYTES:
        stdout += "\n... [输出已截断]"
    if len(stderr) == MAX_OUTPUT_BYTES:
        stderr += "\n... [输出已截断]"

    output = stdout.strip() if stdout else stderr.strip()
    return output or f"(exit {proc.returncode})"
