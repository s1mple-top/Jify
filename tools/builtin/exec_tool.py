# -*- coding: utf-8 -*-
"""builtin tool — shell 命令执行（白名单 + 审批 + 软超时转后台）

超时分两级，语义完全不同：
  软超时（DEFAULT_TIMEOUT）——到点【不杀进程】，把命令转入后台继续跑，
      已经产出的输出原样交还模型，并给出日志路径供后续 cat。解决两件事：
        1. 慢命令（nmap -p-、nuclei 全模板）不再被误判为失败；
        2. 网络抖动导致的实际耗时超预期，不会白等一场。
  硬超时（HARD_TIMEOUT）——真正的保活兜底。转后台后由 watchdog 线程到点
      杀整个进程组，防止真卡死的命令永久占住 worker（SubagentRunner 是同步
      执行，不杀就永远回不来）。
"""

import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from tools.registry import register_tool
from tools.builtin.allowlist import check_allowlist, is_file_modifying
from tools.call_context import register_killer, is_cancelled

# 软超时：到点转为后台，不杀进程
DEFAULT_TIMEOUT = int(os.environ.get("JIFY_EXEC_TIMEOUT", "30"))
# 硬超时：转后台后最多再跑这么久，到点强杀进程组
HARD_TIMEOUT = int(os.environ.get("JIFY_EXEC_HARD_TIMEOUT", "600"))
# 纯 sleep 命令的豁免上限。模型常用 `exec sleep N` 做轮询间隔，30s 软超时会把
# sleep 30/45/60 一并掐断（sleep 30 正好卡边界）。上限受工具外层超时 180s 约束，
# 留 30s 余量给收尾；更长的等待应走真正的后台任务，而不是 sleep。
_MAX_INLINE_SLEEP = 150
MAX_OUTPUT_BYTES = 102400  # 100KB
_KILL_GRACE = 2.0  # SIGTERM 后等待优雅退出的秒数，超时补 SIGKILL
_LOG_DIR = Path(tempfile.gettempdir()) / "jify_exec"
_LOG_KEEP = 60  # 保留最近 N 份日志，避免长跑堆积成灾

# 仍在后台运行的进程：{pid: log_path}。_prune_logs 跳过这些文件的清理，
# 否则「输出很少、mtime 很旧」的静默长任务（nmap 扫端口）会被当成垃圾删掉，
# 模型此后 cat 到的是空文件，转后台的价值直接归零。
_live_background: dict = {}
_live_lock = threading.Lock()

_SLEEP_ONLY_RE = re.compile(r"^\s*sleep\s+(\d+(?:\.\d+)?)\s*[;&]?\s*$")


def _prune_logs() -> None:
    """只保留最近 _LOG_KEEP 份日志，且绝不删仍在后台运行的任务。

    清理失败不影响执行。
    """
    try:
        with _live_lock:
            protected = {str(p) for p in _live_background.values()}
        files = sorted(_LOG_DIR.glob("exec-*.log"), key=lambda p: p.stat().st_mtime)
        removable = [p for p in files if str(p) not in protected]
        for old in removable[:-_LOG_KEEP]:
            old.unlink(missing_ok=True)
    except Exception:
        pass


def _soft_timeout_for(command: str) -> float:
    """解析本条命令的软超时。

    纯 `sleep N` 按 N 放宽（+5s 收尾余量），封顶 _MAX_INLINE_SLEEP：
    轮询间隔是模型的高频动作，被 30s 硬掐会白白多耗一轮推理。
    """
    m = _SLEEP_ONLY_RE.match(command)
    if m:
        try:
            return min(float(m.group(1)) + 5.0, _MAX_INLINE_SLEEP)
        except ValueError:
            pass
    return float(DEFAULT_TIMEOUT)


def _schedule_hard_kill(proc: subprocess.Popen, kill_fn, delay: float) -> None:
    """转后台后挂一个 daemon watchdog：到点仍未退出就杀整个进程组。

    必须是 daemon 线程：进程退出时不该被它 join 住。
    退出时注销 _live_background，让该日志重新可被 prune。
    """
    def _watch() -> None:
        try:
            proc.wait(timeout=delay)
        except subprocess.TimeoutExpired:
            kill_fn()
        except Exception:
            pass
        finally:
            with _live_lock:
                _live_background.pop(proc.pid, None)

    threading.Thread(target=_watch, name=f"exec-hardkill-{proc.pid}", daemon=True).start()


def _read_log(path: Path, limit: int = MAX_OUTPUT_BYTES) -> str:
    """读取日志文件；不存在或读失败返回空串。"""
    try:
        with open(path, "r", errors="replace") as f:
            return f.read(limit)
    except Exception:
        return ""


@register_tool(
    name="exec",
    description=(
        "Execute a shell command on the local system. "
        "Soft timeout turns the command into a background process instead of killing it: "
        "on timeout you get {background:true, log_path, pid} plus whatever output was already "
        "produced. In that case DO NOT re-run the same command — poll with "
        "`cat <log_path>` (sleep a few seconds between polls) and read the result there."
    ),
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
    # 该工具内部会自行调用 request_approval（最长等待 120s），加命令执行时长，
    # 故不能落在 15s 默认档，否则用户还在审批时外层已报超时、命令却照旧执行。
    # 软超时最长 _MAX_INLINE_SLEEP(150)，须留出余量给收尾。
    timeout=180,
)
def exec_tool(command: str, workdir: Optional[str] = None) -> str:
    """Execute shell command with allowlist + approval + soft-timeout-to-background"""

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

    # 输出落文件而非内存管道：软超时转后台后，进程要继续往同一个地方写，
    # 模型后续 cat 这个文件就能看到增量。管道会在工具返回时关闭，做不到。
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _prune_logs()
    try:
        fd, log_path_str = tempfile.mkstemp(prefix="exec-", suffix=".log", dir=str(_LOG_DIR))
    except Exception as e:
        return json.dumps({"error": f"无法创建日志文件: {e}"})
    log_path = Path(log_path_str)

    # 执行
    try:
        log_file = os.fdopen(fd, "w", errors="replace")
    except Exception as e:
        return json.dumps({"error": f"无法打开日志文件: {e}"})

    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=workdir or None,
            stdout=log_file,          # stderr 合并进同一文件：保留完整时序，
            stderr=subprocess.STDOUT,  # 比原先「二选一」更能定位问题
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )
    except FileNotFoundError:
        log_file.close()
        return json.dumps({"error": "找不到 shell 或命令"})
    except Exception as e:
        log_file.close()
        return json.dumps({"error": f"执行异常: {e}"})

    # start_new_session=True 使 shell 成为新会话/进程组组长，pgid == pid。
    # 必须 kill 整个进程组：命令里的 `a && b`、`sh -c` 派生出的孙进程与 shell 同组，
    # 只杀 shell 会留下孤儿进程继续跑（实测 sleep 300 存活）。
    killed_flag = {"done": False}

    def _kill_group() -> bool:
        if killed_flag["done"]:
            return True
        killed_flag["done"] = True
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
    # 登记给外层：调用被取消时由外层触发清理
    register_killer(f"exec(pgid={pgid})", _kill_group)

    soft = _soft_timeout_for(command)
    try:
        proc.wait(timeout=soft)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
    except Exception as e:
        _kill_group()
        log_file.close()
        return json.dumps({"error": f"执行异常: {e}"})

    # 无论是正常结束还是转后台，日志文件都必须在这里关掉：
    # 父进程持有的写端不关，子进程的输出虽然已落盘，但读取时可能看不到尾部。
    try:
        log_file.flush()
        log_file.close()
    except Exception:
        pass

    if not timed_out:
        output = _read_log(log_path)
        stderr_like = output
        output = output[:MAX_OUTPUT_BYTES]

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

        if len(stderr_like) > MAX_OUTPUT_BYTES:
            output += "\n... [输出已截断]"

        output = output.strip()
        return output or f"(exit {proc.returncode})"

    # 软超时：不杀进程，转后台
    # 已产出的输出立刻回传（此前这段数据被丢弃，模型只能看到「超时」然后重跑）。
    partial = _read_log(log_path)[:MAX_OUTPUT_BYTES].strip()
    with _live_lock:
        _live_background[proc.pid] = log_path
    _schedule_hard_kill(proc, _kill_group, max(1.0, HARD_TIMEOUT - soft))

    payload = {
        "timed_out": True,
        "background": True,
        "log_path": str(log_path),
        "pid": proc.pid,
        "soft_timeout_sec": soft,
        "hard_timeout_sec": HARD_TIMEOUT,
        "partial_output": partial or None,
        "hint": (
            f"命令仍在后台运行（pid={proc.pid}），输出持续写入 {log_path}。"
            f"不要重跑同样的命令 —— 用 `exec cat {log_path}` 读取进展，"
            f"两次读取之间可用 `exec sleep 10` 间隔。"
            f"进程会在 {HARD_TIMEOUT}s 后被强制终止（除非先自行结束）。"
        ),
    }
    if partial:
        payload["output_so_far"] = partial
    return json.dumps(payload, ensure_ascii=False)