# -*- coding: utf-8 -*-
"""builtin tool — 白盒单元级动态验证

在 Docker 沙箱容器内用对应语言的解释器执行「喂了恶意输入的 sink 代码片段」，
观察运行时输出（stdout / stderr / exit code），辅助确认漏洞是否可被驱动。

设计要点：
- 强制所有验证在 Docker 容器内执行，本地解释器不再直跑（网络隔离、内存受限、只读根文件系统、非 root）
- 镜像需预先 build（见 docker/sandbox/Dockerfile），运行期不拉取、不下载
- 字符串类语言把代码作为容器的解释器参数透传，不经 shell，杜绝命令注入面
- 语言硬白名单，对应固定解释器
- 严格超时 + 输出截断
- 返回结构化 stdout / stderr / exit_code / duration，供 Agent 自行判断
  是「漏洞成立」还是「环境缺失（import/连库失败）」
"""

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from typing import Any, Dict, Optional

from tools.registry import register_tool
from tools.builtin.exec_tool import MAX_OUTPUT_BYTES
from tools.call_context import register_killer, is_cancelled
from event_bus import UIEvent, event_bus

# 沙箱默认超时独立于 exec：两者语义不同（exec 跑本地命令，沙箱含容器启停开销），
# 共用一个常量会让改 exec 的默认值意外改变沙箱行为
DEFAULT_TIMEOUT = 15

# ============================================================
# Docker 强制沙箱
# 所有 code_sandbox 验证一律在 Docker 容器内执行，本地解释器不再直跑。
# 镜像需预先 build 好（见 docker/sandbox/Dockerfile），运行期不拉取、不下载。
# ============================================================

# 沙箱镜像名，可通过环境变量 JIFY_SANDBOX_IMAGE 覆盖
SANDBOX_IMAGE = os.environ.get("JIFY_SANDBOX_IMAGE", "jify-code-sandbox:latest")

# 容器安全配置：网络隔离、内存/CPU 限制、只读根文件系统、非 root 用户
SANDBOX_MEMORY_LIMIT = os.environ.get("JIFY_SANDBOX_MEMORY", "512m")
SANDBOX_CPU_LIMIT = float(os.environ.get("JIFY_SANDBOX_CPU", "1.0"))
SANDBOX_USER = os.environ.get("JIFY_SANDBOX_USER", "1000:1000")  # 容器内非 root，需镜像创建对应 uid

CONTAINER_BASE = [
    "docker", "run", "--rm",
    "--network=none",
    f"--memory={SANDBOX_MEMORY_LIMIT}",
    f"--cpus={SANDBOX_CPU_LIMIT}",
    "--read-only",
    "--tmpfs", "/tmp:rw,size=100m,mode=1777",
    f"--user={SANDBOX_USER}",
]

# 语言 -> (解释器可执行名, [前置参数])；前置参数统一用列表形式拼接代码，避免 shell 注入
LANG_TO_CMD: Dict[str, tuple] = {
    "python":   ("python3", ["-c"]),
    "python3":  ("python3", ["-c"]),
    "py":       ("python3", ["-c"]),
    "php":      ("php",     ["-r"]),
    "php-cli":  ("php",     ["-r"]),
    "ruby":     ("ruby",    ["-e"]),
    "rb":       ("ruby",    ["-e"]),
    "node":     ("node",    ["-e"]),
    "javascript": ("node",  ["-e"]),
    "js":       ("node",    ["-e"]),
    "shell":    ("bash",    ["-c"]),
    "bash":     ("bash",    ["-c"]),
    "sh":       ("bash",    ["-c"]),
    "go":       ("go",      ["run"]),   # 需要落盘临时文件，见 exec 分支
    "java":     ("java",    []),
}

# 通过临时文件执行的语言（go run / javac 需要真实文件而不是 -c 字符串）
TEMP_FILE_LANGS = {"go": ".go", "java": ".java"}

ALLOWED_LANGS = set(LANG_TO_CMD.keys())


@register_tool(
    name="code_sandbox",
    description=(
        "白盒单元级动态验证：在 Docker 沙箱容器内用指定语言的解释器执行代码片段，"
        "观察运行时输出以辅助判断漏洞是否可被驱动。适用于已定位到可独立执行的 sink 代码片段"
        "（跳过起服务，直接把 mock 好的输入 + 源码喂给解释器）。"
        "\n强制要求：所有验证一律在 Docker 容器内执行（网络隔离、内存受限、只读根文件系统），"
        "镜像 SANDBOX_IMAGE 需预先 build（见 docker/sandbox/Dockerfile），运行期不拉取。"
        "返回 stdout/stderr/exit_code/duration，由 Agent 判断是漏洞成立还是环境缺失。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "language": {
                "type": "string",
                "description": "编程语言: python, php, ruby, node/javascript, bash/shell, go, java",
            },
            "code": {
                "type": "string",
                "description": "要执行的代码片段（LLM 已拼接 mock 输入 + 原始 sink 源码）",
            },
            "mock_params": {
                "type": "object",
                "description": "可选，模拟请求参数，等价于 $_GET 赋值；会自动注入环境感知的请求对象",
            },
            "workdir": {
                "type": "string",
                "description": "可选，工作目录（仅用于受影响文件类语言；否则忽略）",
            },
            "timeout": {"type": "integer", "description": "超时秒数，默认 15"},
        },
        "required": ["language", "code"],
    },
    parallel_safe=False,
    requires_approval=False,
    # 内部 timeout 可由参数指定（默认 15），且 Docker 起容器本身有额外开销；
    # 外层若停在 15s 默认档，会把用户显式指定的更长 timeout 提前掐断
    timeout=300,
)
def code_sandbox(
    language: str,
    code: str,
    mock_params: Optional[Dict[str, Any]] = None,
    workdir: Optional[str] = None,
    timeout: Optional[int] = None,
) -> str:
    """在 Docker 沙箱中执行代码片段，返回结构化 JSON 结果。"""
    lang = language.lower().strip()
    event_bus.put(UIEvent("TEXT", f"* preparing code_sandbox ( lang={lang}, sandbox=docker )"))

    if lang not in ALLOWED_LANGS:
        return json.dumps({
            "error": f"不支持的语言: {language}。允许: {sorted(ALLOWED_LANGS)}",
            "blocked": True,
        }, ensure_ascii=False)

    executable, prefix_args = LANG_TO_CMD[lang]

    # Docker 可用性检查（所有验证强制走容器）
    if shutil.which("docker") is None:
        return json.dumps({
            "error": "未找到 docker 命令，code_sandbox 强制要求 Docker。",
            "env_gap": True,
        }, ensure_ascii=False)

    timeout = timeout or DEFAULT_TIMEOUT

    # 若提供了 mock_params，拼装到代码开头
    if mock_params:
        code = _build_mock_wrapper(lang, mock_params) + code

    need_temp = lang in TEMP_FILE_LANGS

    try:
        import tempfile, os
        # ---------- 临时文件类语言（go/java）：源码挂进容器再跑 ----------
        if need_temp:
            ext = TEMP_FILE_LANGS[lang]
            with tempfile.TemporaryDirectory() as td:
                src_file = os.path.join(td, "_sandbox_main" + ext)
                with open(src_file, "w", encoding="utf-8") as f:
                    f.write(code)

                container_cmd = CONTAINER_BASE + [
                    "-v", f"{td}:/workspace:rw",
                    "-w", "/workspace",
                    SANDBOX_IMAGE,
                ]
                if lang == "go":
                    container_cmd += ["go", "run", "_sandbox_main.go"]
                else:  # java
                    container_cmd += ["java", "_sandbox_main.java"]

                result = _run_docker(container_cmd, timeout)
                return _format_result(result)

        # ---------- -c / -e / -r 字符串类语言：代码作为容器命令参数透传 ----------
        container_cmd = CONTAINER_BASE + [SANDBOX_IMAGE, executable, *prefix_args, code]
        result = _run_docker(container_cmd, timeout)
        return _format_result(result)

    except FileNotFoundError:
        return json.dumps({"error": f"找不到 docker 可执行文件"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"执行异常: {e}"}, ensure_ascii=False)


def _build_mock_wrapper(lang: str, params: Dict[str, Any]) -> str:
    """根据语言构造 mock 请求输入的前置代码（等价于把攻击者输入注入 sink 的 source）。"""
    import json as _json

    if lang in ("python", "python3", "py"):
        p = _json.dumps(params, ensure_ascii=False)
        return (
            "import sys as _j\n"
            "class _JReq:\n"
            "    args={p}\n"
            "    form={p}\n"
            "    values={p}\n"
            "    json={p}\n"
            "    def get_json(self,*a,**k): return {p}\n"
            "    def get(self,k,d=None): return {p}.get(k,d)\n"
            "request=_JReq()\n".replace("{p}", p)
        )
    if lang in ("php", "php-cli"):
        lines = []
        for k, v in params.items():
            lines.append(f"$_GET['{k}'] = '{v}';")
            lines.append(f"$_POST['{k}'] = '{v}';")
            lines.append(f"$_REQUEST['{k}'] = '{v}';")
        return "\n".join(lines) + "\n"
    if lang in ("node", "javascript", "js"):
        p = _json.dumps(params, ensure_ascii=False)
        return (
            "const req={query:" + p + ",body:" + p + ",params:" + p +
            ",headers: {},method:'GET',path:'/',url:'/'};\n"
            "process.argv=['node','script'," + ",".join(
                _json.dumps(str(v), ensure_ascii=False) for v in params.values()
            ) + "];\n"
        )
    if lang in ("ruby", "rb"):
        # Ruby 没有天然魔法参数对象，直接塞一个可收发 params 的模拟
        body = "{" + ",".join(f'"{k}"=>"{_escape_rb(v)}"' for k, v in params.items()) + "}"
        return (
            "def params; " + body + "; end\n"
            "def request; self; end\n"
        )
    if lang in ("shell", "bash", "sh"):
        lines = []
        for k, v in params.items():
            lines.append(f'export {k.upper()}="{v}"')
        return "\n".join(lines) + "\n"
    return ""


def _escape_rb(v: str) -> str:
    """转义 Ruby 字符串中的特殊字符。"""
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("#", "\\#")


def _kill_container(container_name: str) -> bool:
    """强制终止并删除沙箱容器。

    subprocess.run(timeout=) 超时只会杀掉 docker CLI 客户端，容器已交给 daemon
    托管，会继续跑满 CPU/内存/网络。--rm 仅作用于容器退出时，不是 kill。
    因此超时后必须按容器名显式 rm -f（对 running 容器等价于 SIGKILL + 删除）。
    """
    if not container_name:
        return False
    try:
        proc = subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _run_docker(container_cmd, timeout):
    """在 Docker 容器内执行命令，返回结构化字典。

    container_cmd 为完整 docker run 命令（含镜像与容器内的解释器调用），
    避免经 shell 拼接代码，杜绝命令注入面。

    为容器显式命名，超时后据此强制终止：超时若不 kill，容器会在后台继续运行。
    """
    container_name = f"jify-sandbox-{uuid.uuid4().hex[:12]}"
    # --name 插在 docker run 之后，便于超时按名 kill
    named_cmd = [container_cmd[0], container_cmd[1], "--name", container_name, *container_cmd[2:]]

    try:
        import time
        start = time.time()
        proc = subprocess.Popen(
            named_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )
    except Exception as e:
        return {
            "success": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "duration_ms": 0,
            "timed_out": False,
            "error": str(e),
        }

    # 登记给外层调用：工具自身超时之外，调用超时（线程卡住）时也要清掉容器，
    # 否则 docker run 客户端被丢弃、容器由 daemon 托管继续跑满 CPU/内存
    register_killer(
        f"code_sandbox({container_name})",
        lambda: _kill_container(container_name),
    )

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        duration_ms = int((time.time() - start) * 1000)
        # 容器被外部取消 rm -f 时，docker run 只会返回非零码；不点明的话
        # 模型会当成普通执行失败去重试
        if proc.returncode != 0 and is_cancelled():
            return {
                "success": False,
                "exit_code": proc.returncode,
                "stdout": _truncate(stdout),
                "stderr": _truncate(stderr),
                "duration_ms": duration_ms,
                "timed_out": False,
                "cancelled": True,
                "terminated": True,
                "error": (
                    f"调用已被取消，容器 {container_name} 已强制终止。"
                    "代码可能已执行到中途，副作用可能部分发生。"
                ),
            }
        return {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": _truncate(stdout),
            "stderr": _truncate(stderr),
            "duration_ms": duration_ms,
            "timed_out": False,
            "error": None,
        }
    except subprocess.TimeoutExpired:
        killed = _kill_container(container_name)
        try:
            proc.communicate(timeout=2)  # 排空管道并回收客户端进程
        except Exception:
            pass
        return {
            "success": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "duration_ms": timeout * 1000,
            "timed_out": True,
            "killed": killed,
            # 文案必须与实际动作一致：kill 失败时明确告知容器可能仍在跑，
            # 否则模型会以为状态已清理而漏掉后续检查
            "error": (
                f"容器执行超时 ({timeout}s)，已强制终止容器 {container_name}"
                if killed else
                f"容器执行超时 ({timeout}s)，强制终止容器 {container_name} 失败，"
                f"该容器可能在后台继续运行，请手动确认并清理"
            ),
        }
    except Exception as e:
        # 非超时异常同样意味着本次执行已放弃，容器状态不可知，一并清理
        _kill_container(container_name)
        try:
            proc.communicate(timeout=2)
        except Exception:
            pass
        return {
            "success": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "duration_ms": 0,
            "timed_out": False,
            "error": str(e),
        }


def _truncate(text: str) -> str:
    """输出截断，防止超大输出撑爆上下文。"""
    if not text:
        return ""
    if len(text) > MAX_OUTPUT_BYTES:
        return text[:MAX_OUTPUT_BYTES] + "\n... [输出已截断]"
    return text


def _format_result(result: dict) -> str:
    """序列化为 Agent 可直接消费的结构化输出。"""
    return json.dumps(result, ensure_ascii=False)