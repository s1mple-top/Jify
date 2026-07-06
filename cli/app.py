"""JifyCLI - CLI Agent + Slash commands + REPL loop."""

from __future__ import annotations

import argparse
import time
import json
import os
import queue
import random
import re
import select
import textwrap
import threading
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, WordCompleter
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.keys import Keys
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style as PTStyle
from rich.markdown import Markdown
from rich.padding import Padding
from rich.rule import Rule
from rich.text import Text

from agent_loop import AgentLoop, AgentConfig
from self_evolution import SelfEvolutionEngine, EvolutionTask, TaskPhase
from model_client import get_model_client
from agent_p2p import (
    init_p2p, stop_p2p, set_request_handler,
    build_prompt_from_message,
)
from output_engine import JifyTheme
from tools.approval import termios_lock
from cli.console import CLIConsole
from .bootstrap import ensure_jify_home

console = JifyTheme.create_console()


# Slash 命令系统 — 输入 "/" 自动补全
SLASH_COMMANDS: Dict[str, str] = {
    "model": "切换模型          —  /model <model_name>",
    "resume": "加载历史对话      —  /resume <session_id>",
    "sessions": "列出最近对话会话  —  /sessions",
    "clear": "清除对话历史      —  /clear",
    "help": "显示帮助信息      —  /help",
    "hook": "显示hook信息      —  /hook",
    "skill": "列出所有可用 skill  —  /skill",
    "jify": "分析当前目录下的项目，生成Jify.md      —  /jify",
    "exit": "退出程序          —  /exit",
}


class SlashCompleter(Completer):
    """
    专为 / 命令设计的补全器。

    WordCompleter 默认 WORD=False，会把 /model 的 / 当作分隔符，
    只拿到 "model" 去匹配，导致 /m 无法补全 /model。
    这里用 sentence=True 模式，直接对整段文本做前缀匹配。


    A completer designed specifically for `/` commands.

    By default, `WordCompleter` sets `WORD=False`, treating the `/` in `/model` as a delimiter;
    it only uses "model" for matching, meaning `/m` fails to complete `/model`.
    Here, the `sentence=True` mode is used to perform prefix matching against the entire text string.
    """

    def __init__(self, commands: Dict[str, str]) -> None:
        self._words = ["/" + cmd for cmd in commands]
        self._wc = WordCompleter(self._words, sentence=True)

    def get_completions(self, document, complete_event):
        if document.text.startswith("/"):
            yield from self._wc.get_completions(document, complete_event)


# 粘贴遮蔽全局缓冲区
_paste_buffers: list = []  # 按粘贴顺序保存 (占位符, 原始内容)


def _create_cli_keybindings() -> KeyBindings:
    """创建带粘贴遮蔽的 KeyBindings：BracketedPaste → 占位符，Enter → 还原"""
    kb = KeyBindings()

    @kb.add(Keys.Enter)
    def _submit(event):
        global _paste_buffers
        buffer = event.app.current_buffer
        user_input = buffer.text

        # 按顺序还原所有粘贴占位符
        for placeholder, content in _paste_buffers:
            user_input = user_input.replace(placeholder, content, 1)

        _paste_buffers = []
        # 写入历史，否则上键无法回溯之前输入
        buffer.append_to_history()
        event.app.exit(result=user_input)

    @kb.add(Keys.BracketedPaste)
    def _on_paste(event):
        global _paste_buffers
        paste_data = event.data
        if not paste_data:
            return

        buffer = event.app.current_buffer
        if buffer.selection_state:
            buffer.cut_selection()

        if len(paste_data) > 100 or '\n' in paste_data:
            idx = len(_paste_buffers)
            placeholder = f"[Parse text {idx}:{len(paste_data)}]"
            _paste_buffers.append((placeholder, paste_data))
            buffer.insert_text(placeholder)
        else:
            buffer.insert_text(paste_data)

    return kb


_cli_session = PromptSession(
    completer=SlashCompleter(SLASH_COMMANDS),
    key_bindings=_create_cli_keybindings(),
    history=InMemoryHistory(),
    style=PTStyle.from_dict({
        "": "#cdd6f4",
        "completion-menu.completion": "bg:#1e1e2e #cdd6f4",
        "completion-menu.completion.current": "bg:#45475a #cdd6f4",
    }),
)


# Jify CLI Agent — 封装 AgentLoop

# 基础架构 / 协调工具：不应被自进化视为用户偏好
_INFRA_TOOLS = {
    "team_delegate", "team_delegate_parallel", "team_broadcast",
    "team_add_worker", "team_remove_worker", "team_status", "subagent_run",
    "update_todos", "mcp_reload", "mcp_list",
    "load_skill", "skill_create",
}

# P2P 可用名称列表（每次启动随机选取，无需文件持久化，crash 安全）

P2P_AGENT_NAMES = [
    "Jify_Alice", "Jify_Bob", "Jify_Charlie", "Jify_David",
    "Jify_Eva", "Jify_Frank", "Jify_Grace", "Jify_Henry",
    "Jify_Jack", "Jify_Kevin", "Jify_Lisa", "Jify_Mike",
    "Jify_Nina", "Jify_Oscar", "Jify_Paul", "Jify_Quinn",
    "Jify_Rose", "Jify_Sam", "Jify_Tina",
]


class JifyCLI:
    """CLI 风格 Jify Agent"""

    def __init__(self, think_stream: bool = False, safe_exec: bool = False) -> None:
        # 设置白名单模式开关
        from tools.builtin.allowlist import set_safe_exec
        set_safe_exec(safe_exec)

        # 加载配置
        self.config = AgentConfig.load_from_yaml()

        # 加载内置工具（触发 tools.builtin 的 @register_tool）
        from jify_tool import registry as reg

        # 插件系统-加载插件
        try:
            from plugins.loader import PluginLoader
            self.plugin_loader = PluginLoader(plugins_dir=self.config.plugins_dir)
            self.plugin_loader.load_all(enabled_only=self.config.enabled_plugins)
        except Exception:
            self.plugin_loader = None
        self.plugin_count = len(self.plugin_loader.list_loaded()) if self.plugin_loader else 0

        # 加载MCP 配置（快捷路径，仅加载配置不建连）
        self._mcp_loaded = False
        self._tools_lock = threading.RLock()
        try:
            from tools.mcp.manager import mcp_manager
            n = mcp_manager.load_servers()
            self._mcp_manager = mcp_manager if n > 0 else None
        except Exception:
            self._mcp_manager = None

        # 构建 tools schema（在所有工具注册完毕后统一收集，确保插件工具也被纳入）
        # MCP 工具异步加载后会通过 _rebuild_tools() 追加
        self._build_tools(reg)

        # 自进化引擎（后台线程，不阻塞主对话）
        self.evolution = SelfEvolutionEngine(
            summarizer=self._create_summarizer(),
            profile_interval=self.config.SelfEvolutionTurn,
        )

        # 统一 system prompt 构建（与 gateway 共享 SystemPromptBuilder）
        from config.system_prompt import SystemPromptBuilder
        self._prompt_builder = SystemPromptBuilder(evolution_engine=self.evolution)
        self.system_prompt = self._prompt_builder.build()
        # 保留 skills 引用以兼容旧调用
        self.skills = self._prompt_builder.skills


        # AgentLoop
        self.agent = AgentLoop(agent_config=self.config)
        # CLIConsole
        self.cli_console = CLIConsole(think_stream)

        # Team Mode: 初始化 TeamOrchestrator（仅注册 leader，不启动 Worker）
        from team import TeamOrchestrator, set_leader, set_output_engine
        from tools.approval import set_approval_engine
        self._team_orch = TeamOrchestrator(self.agent.model_client, self.config)
        set_leader(self._team_orch.leader)
        set_output_engine(self.cli_console._output)
        set_approval_engine(self.cli_console._output)

        # Agent 互斥锁：串行化 CLI chat 与 P2P 请求，防止并发使用 AgentLoop/CLIConsole
        self._agent_lock = threading.Lock()

        # P2P FIFO 队列 + 单消费者线程：保证多个智能体同时请求时的公平处理
        self._p2p_queue: queue.Queue = queue.Queue()
        self._p2p_consumer_thread = threading.Thread(
            target=self._p2p_consumer, daemon=True, name="p2p-consumer"
        )
        self._p2p_consumer_thread.start()

        # P2P 初始化：随机选取一个可用名称，socket 冲突时自动尝试下一个
        try:
            available = list(P2P_AGENT_NAMES)
            random.shuffle(available)
            name = ""
            for candidate in available:
                try:
                    init_p2p(candidate) # 开启监听
                    name = candidate
                    break
                except OSError:
                    continue
            if not name:
                raise RuntimeError("无可用jify name（所有 socket 已被占用）")
            self._p2p_name = name
            self.system_prompt = self.system_prompt.replace("你是Jify，", f"你是{name}，")

            # 注册 P2P 请求处理器：将 P2P 请求路由到 CLI 的 AgentLoop + CLIConsole
            _cli_ref = self  # 闭包捕获

            def _p2p_handler(p2p_msg):
                """P2P 请求处理器：入队 + 阻塞等待结果（FIFO 公平调度）。

                多个智能体同时请求时，按到达顺序排队处理，等待当前用户 chat 结束后再执行。
                不打断用户正在进行的对话。
                运行在 AutoAgent 的线程池中。
                """
                reply_event = threading.Event()
                reply_container: Dict[str, str] = {}

                _cli_ref._p2p_queue.put((p2p_msg, reply_event, reply_container))

                if reply_event.wait(timeout=300): # 等待 p2p 执行 5min
                    return reply_container.get('response', '')
                else:
                    return '[超时] P2P 请求处理超时，请稍后重试'

            set_request_handler(_p2p_handler)
        except Exception as e:
            meta(f"  P2P: init failed — {e}")
        self._interrupt = threading.Event()
        self._esc_stop = threading.Event()
        self._esc_thread: Optional[threading.Thread] = None


    def _p2p_consumer(self) -> None:
        """FIFO 消费者线程：从队列依次取出 P2P 请求并处理。

        不打断用户 chat，等待当前对话结束后再获取锁执行。
        保证多个智能体同时请求时的公平调度（先到先处理）。
        """
        while True:
            try:
                p2p_msg, reply_event, reply_container = self._p2p_queue.get(timeout=1)
            except queue.Empty:
                continue

            # 阻塞等待当前用户 chat 结束释放锁，不打断当前的chat，因为用户同时使用p2p和chat的概率极低
            self._agent_lock.acquire()

            # 停止入处理队列，并不停止接收
            self.cli_console.stop_p2p_listener()
            try:
                prompt = build_prompt_from_message(p2p_msg)
                result = self.agent.run( # 底层走同一套 loop engine
                    message_id=str(uuid.uuid4()),
                    user_message=prompt,
                    system_prompt=self.system_prompt,
                    console=self.cli_console,
                    tool_schemas=self.tools,
                )

                # 提交自进化任务
                tools_used: List[str] = []
                if result and result.get('messages'):
                    for msg in result['messages']:
                        if hasattr(msg, 'tool_calls') and msg.tool_calls:
                            for tc in msg.tool_calls:
                                tname = tc.function.name if hasattr(tc.function, 'name') else str(tc)
                                if tname and tname not in tools_used and tname not in _INFRA_TOOLS:
                                    tools_used.append(tname)
                final_resp = result.get('final_response', '') if result else ''
                self.evolution.submit(EvolutionTask(
                    phase=TaskPhase.PROFILE,
                    user_msg=prompt,
                    assistant_msg=final_resp,
                    tools_used=tools_used,
                    outcome=final_resp[:5000],
                ))

                reply_container['response'] = result.get('final_response', '') if result else ''

            except Exception as e:
                reply_container['response'] = f'[处理异常] {e}'
            finally:
                self.cli_console.finalize()
                self.cli_console.start_p2p_listener()
                self._agent_lock.release()
                reply_event.set() # 执行完毕set信号

    def _create_summarizer(self):
        """为自进化引擎创建一个独立的 model client

        优先级：
          1. SelfEvolutionModel 非空且命中 models 列表 → 用该条目建独立 client
          2. 未命中或为空 → 回退到当前激活模型
        """
        se_model_name = self.config.SelfEvolutionModel
        if se_model_name:
            mc = self.config.get_model_config(se_model_name)
            if mc is not None:
                client = get_model_client(
                    provider=mc.provider or self.config.provider,
                    api_key=mc.api_key or self.config.api_key,
                    base_url=mc.base_url or self.config.base_url,
                )
                model = mc.model

                def summarize(prompt: str) -> str:
                    response = client.chat(
                        messages=[{"role": "user", "content": prompt}],
                        tool_schemas=[],
                        model=model,
                        temperature=0.3,
                        max_tokens=1024,
                    )
                    return response.content

                return summarize

        # 回退到当前激活模型
        client = get_model_client(
            provider=self.config.provider,
            api_key=self.config.api_key,
            base_url=self.config.base_url,
        )
        model = self.config.model

        def summarize(prompt: str) -> str:
            response = client.chat(
                messages=[{"role": "user", "content": prompt}],
                tool_schemas=[],
                model=model,
                temperature=0.3,
                max_tokens=1024,
            )
            return response.content

        return summarize

    def chat(self, user_input: str) -> None:
        """处理一轮对话（think 状态栏由 CLIConsole.consume_stream 内部渲染）"""
        # 尝试获取锁，对话任务完成之后释放掉，主chat和p2p
        acquired = self._agent_lock.acquire(timeout=30)
        if not acquired:
            meta("  [智能体正忙，请稍候]")
            return

        try:
            self._interrupt.clear()
            self.cli_console.token_num = 0
            self.cli_console.last_reasoning_content = ""
            self.cli_console._stream_count = 0
            self.cli_console._pending_sent_tokens = 0

            # 开始执行的时候暂停监听，只暂停 p2p 的 event bus 接受展示，避免输出打印紊乱
            self.cli_console.stop_p2p_listener()

            input_is_active = self.cli_console._output._input_active.is_set()
            result = None
            try:
                if not input_is_active:
                    self._start_esc_listener() # 开启 esc 监听，打断机制
                result = self.agent.run(
                    message_id="cli",
                    user_message=user_input,
                    system_prompt=self.system_prompt,
                    console=self.cli_console,
                    tool_schemas=self.tools,
                )

                # 过滤基础架构工具，避免自进化误将"团队模式"等视为用户偏好
                tools_used = []
                if result and result.get('messages'):
                    for msg in result['messages']:
                        if hasattr(msg, 'tool_calls') and msg.tool_calls:
                            for tc in msg.tool_calls:
                                if isinstance(tc, dict):
                                    name = tc.get("function", {}).get("name", str(tc))
                                elif hasattr(tc, 'function') and hasattr(tc.function, 'name'):
                                    name = tc.function.name
                                else:
                                    name = str(tc)
                                if name and name not in tools_used and name not in _INFRA_TOOLS:
                                    tools_used.append(name)

                final_resp = result.get('final_response', '') if result else ''
                self.evolution.submit(EvolutionTask(
                    phase=TaskPhase.PROFILE,
                    user_msg=user_input,
                    assistant_msg=final_resp,
                    tools_used=tools_used,
                    outcome=final_resp[:5000],
                ))

                self.cli_console.drain_events()
            finally:
                if not input_is_active:
                    self._stop_esc_listener()
                self.cli_console.finalize()

            self.cli_console.start_p2p_listener()

            console.print()

            if result:
                meta(
                    f"✓ 完成 — {result.get('iterations', 0)} 轮迭代, 总耗时 {result.get('elapsed', 0):.1f}s, 总消耗 token {result.get('total_tokens', 0) // 2}")
        finally:
            self._agent_lock.release()

    def interrupt(self) -> None:
        self._interrupt.set()
        self.agent.interrupt()

    @staticmethod
    def cleanup_p2p() -> None:
        try:
            stop_p2p()
        except Exception:
            pass

    def _start_esc_listener(self) -> None:

        if self._esc_thread and self._esc_thread.is_alive():
            return

        self._esc_stop.clear()
        cli = self

        def _listen() -> None:
            import termios
            import tty
            fd = sys.stdin.fileno()
            with termios_lock:
                old_attrs = termios.tcgetattr(fd)
            try:
                with termios_lock:
                    tty.setcbreak(fd) # 修改为cbreak，cooked会缓冲
                while not cli._esc_stop.is_set():
                    from tools.approval import is_approval_active
                    # 暂停监听 0.15 秒，避免审批期间按 ESC 误触发中断
                    if is_approval_active():
                        cli._esc_stop.wait(0.15)
                        continue
                    r, _, _ = select.select([sys.stdin], [], [], 0.15)
                    if r:
                        try:
                            ch = sys.stdin.read(1)
                        except Exception:
                            break
                        if ch == '\x1b':
                            cli.interrupt()
                            console.print(Text("  [ESC 中断]", style=JifyTheme.RED))
            except Exception:
                pass
            finally:
                with termios_lock:
                    termios.tcsetattr(fd, termios.TCSAFLUSH, old_attrs)

        self._esc_thread = threading.Thread(target=_listen, daemon=True)
        self._esc_thread.start()

    def _stop_esc_listener(self) -> None:
        """终止 ESC 监听线程。

        _esc_stop 事件通知线程退出循环，线程在 finally 块中恢复终端属性。
        """
        self._esc_stop.set()
        if self._esc_thread and self._esc_thread.is_alive():
            self._esc_thread.join(timeout=2.0)
        self._esc_thread = None

    def _build_tools(self, reg) -> None:
        """从 registry 重建工具 schema 列表，线程安全。"""
        tools = []
        for name in reg.get_all_names():
            tool = reg.get(name)
            if tool:
                tools.append({
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                })

        # mcp p2p/chat 并发写 加锁
        with self._tools_lock:
            self.tools = tools

    def start_mcp_async(self) -> None:
        """后台线程连接 MCP server，不阻塞主线程。

        MCP 连接成功并注册工具后，自动更新 self.tools。
        """
        if self._mcp_loaded or self._mcp_manager is None:
            return
        self._mcp_loaded = True

        import logging
        _log = logging.getLogger(__name__)
        mgr = self._mcp_manager

        def _connect():
            try:
                connect_results = mgr.connect_all()
                discover_results = mgr.discover_and_register()
                total = sum(max(0, v) for v in discover_results.values())
                if total > 0:
                    _log.info(
                        "MCP: %d tools from %d server(s)",
                        total, len(discover_results),
                    )
                    from jify_tool import registry as reg
                    self._build_tools(reg)
            except Exception as e:
                _log.warning("MCP async init failed: %s", e)

        t = threading.Thread(target=_connect, daemon=True, name="mcp-loader")
        t.start()

    @staticmethod
    def _get_skills() -> list:
        """遍历 skills 目录，解析 skill 的 name 和 description。

        委托到 config.system_prompt._discover_skills()，消除重复实现。
        Returns:
            List[Dict[str, str]]: [{"skill_name": "description"}, ...]
        """
        from config.system_prompt import _discover_skills
        return _discover_skills()



# 辅助函数
def divider() -> None:
    console.print(Rule(style="dim #585b70", characters="─"))


def meta(text: str) -> None:
    console.print(Text(text, style=f"italic {JifyTheme.SUBTLE}"))


# 主 REPL
def read_input(prompt: str = "> ") -> str:
    global _paste_buffers
    try:
        return _cli_session.prompt(prompt, wrap_lines=True)
    except (EOFError, KeyboardInterrupt):
        _paste_buffers = []  # 非正常退出时清理粘贴占位符，防止污染下次输入
        return ""


def main_loop(think_stream: bool = False, safe_exec: bool = False) -> None:
    console.clear()

    # 初始化 Agent
    agent_cli = JifyCLI(think_stream, safe_exec)
    agent_cli.start_mcp_async()

    # Banner
    name = getattr(agent_cli, '_p2p_name', 'Jify')
    cwd = os.getcwd()
    config = agent_cli.config
    W = 49  # content width inside box borders

    def _box(content="", indent=3) -> Text:
        line = f"{' ' * indent}{content}".ljust(W)
        return Text(f"│{line}│", style=JifyTheme.ACCENT)

    console.print(Text(f"╭{'─' * W}╮", style=JifyTheme.ACCENT))
    console.print(_box(f"✻ Welcome to {name} Agent!", indent=1))
    console.print(_box())
    console.print(_box("/help for help"))
    console.print(_box())
    console.print(_box(f"cwd: {cwd}"))
    console.print(_box())
    console.print(_box("─" * (W - 5)))
    console.print(_box())
    console.print(_box(f"Model: {config.model}"))
    console.print(_box())
    console.print(_box("• API Base URL:"))
    console.print(_box(config.base_url or "(not set)", indent=3))
    console.print(Text(f"╰{'─' * W}╯", style=JifyTheme.ACCENT))
    console.print()
    console.print(Text(" Tips for getting started:", style=f"bold {JifyTheme.ACCENT}"))
    console.print()
    console.print(Text(" 1. Use Jify to help with vibecoding", style=JifyTheme.SUBTLE))
    console.print(Text(" 2. Run /resume to resume your conversation", style=JifyTheme.SUBTLE))
    console.print(Text(" 3. Run /jify to create a Jify.md file with instructions for Jify", style=JifyTheme.SUBTLE))
    console.print(Text(" 4. Jify can evolve on its own. Please experience it as time goes by", style=JifyTheme.SUBTLE))
    console.print()
    divider()

    # 启动 JifyP 协议后台监听output展示
    agent_cli.cli_console.start_p2p_listener()

    # REPL
    _conversation_saved = False  # 防止 finally 重复保存
    try:
        while True:
            # console.print()
            sys.stdout.flush()
            time.sleep(0.1)

            # 自进化 Skill 审批弹窗
            pending = agent_cli.evolution.get_pending_skills()
            if pending:
                console.print()
                meta("  [自进化] 检测到可沉淀的工作流模式：")
                for i, s in enumerate(pending):
                    console.print(
                        Text(f"    [{i + 1}] {s.name}", style=f"bold {JifyTheme.ACCENT}"),
                        Text(f"        {s.description}", style=JifyTheme.SUBTLE),
                        sep="\n",
                    )
                    if s.steps:
                        console.print(Text(f"        步骤: {s.steps[:200]}", style=JifyTheme.SUBTLE))
                    if s.tools_used:
                        console.print(Text(f"        工具: {', '.join(s.tools_used)}", style=JifyTheme.SUBTLE))
                console.print()
                console.print(Text("  [A]ccept  [R]eject  [D]efer (跳过)", style=JifyTheme.SUBTLE))
                agent_cli.cli_console.set_input_active(True)
                choice = read_input("  skill >     ")
                agent_cli.cli_console.set_input_active(False)

                choice = choice.strip().lower()
                if choice == "a":
                    for s in pending:
                        agent_cli.evolution.approve_skill(s.name)
                    meta(f"  ✓ 已沉淀 {len(pending)} 个 skill")
                elif choice == "r":
                    for s in pending:
                        agent_cli.evolution.reject_skill(s.name)
                    meta(f"  ✗ 已拒绝 {len(pending)} 个 skill")
                else:
                    meta("  ⏳ 已推迟，下次对话前会再次提醒")
                divider()

            agent_cli.cli_console.set_input_active(True)
            user_input = read_input("> ")
            agent_cli.cli_console.set_input_active(False)

            if not user_input.strip():
                continue

            # Slash 命令处理
            if user_input.strip().startswith("/"):
                parts = user_input.strip().split(maxsplit=1)
                cmd = parts[0][1:]  # 去掉 "/"
                arg = parts[1] if len(parts) > 1 else ""

                if cmd == "exit":
                    divider()
                    session_id = agent_cli.agent.save_conversation()
                    _conversation_saved = True
                    if session_id:
                        meta(f"  再见！本次全程对话已保存，使用 /resume {session_id} 可恢复")
                    else:
                        meta("  再见！")
                    console.print()
                    break
                elif cmd == "clear":
                    console.clear()
                    agent_cli.agent.clear_history()
                    meta("  对话历史已清除")
                    divider()
                    continue
                elif cmd == "help":
                    for name, desc in SLASH_COMMANDS.items():
                        meta(f"  /{name:<10} {desc}")
                    divider()
                    continue
                elif cmd == "hook":
                    from plugins.hook_manager import hook_manager
                    hooks = hook_manager.list_hooks()
                    meta("  📦 已注册 Hook:")
                    if any(hooks.values()):
                        for hook_point, count in hooks.items():
                            meta(f"    • {hook_point}  —  {count} 个")
                    else:
                        meta("    (无)")
                    # 补充显示已加载的 hook 类型插件
                    if agent_cli.plugin_loader:
                        hook_plugins = [
                            name for name, info in agent_cli.plugin_loader.loaded.items()
                            if "hook" in info.get("manifest", {}).get("type", [])
                        ]
                        if hook_plugins:
                            console.print()
                            meta("  🔌 Hook 插件:")
                            for p in hook_plugins:
                                meta(f"    • {p}")
                    divider()
                    continue
                elif cmd == "skill":
                    # 子命令: /skill delete <name>
                    if arg.startswith("delete "):
                        target_name = arg[7:].strip()
                        if not target_name:
                            meta("  用法: /skill delete <name>")
                            divider()
                            continue
                        suggestion_path = Path(os.path.expanduser("~")) / ".jify" / "self_evolution" / "skills" / "cli_user.json"
                        if not suggestion_path.exists():
                            meta(f"  ⚠ 暂无自进化沉淀 skill")
                            divider()
                            continue
                        try:
                            data = json.loads(suggestion_path.read_text(encoding="utf-8"))
                            approved = set(data.get("approved", []))
                            if target_name not in approved:
                                meta(f"  ⚠ 未找到自进化沉淀 skill: {target_name}")
                            else:
                                approved.discard(target_name)
                                data["approved"] = sorted(approved)
                                suggestion_path.write_text(
                                    json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                                    encoding="utf-8",
                                )
                                meta(f"  ✓ 已删除自进化沉淀 skill: {target_name}")
                        except (json.JSONDecodeError, IOError) as e:
                            meta(f"  ⚠ 操作失败: {e}")
                        divider()
                        continue

                    # 列出所有 skill
                    skills_list = JifyCLI._get_skills()
                    meta("  📦 已安装 Skill:")
                    if skills_list:
                        for s in skills_list:
                            if isinstance(s, dict):
                                for name, desc in s.items():
                                    desc_text = desc if desc else "(无描述)"
                                    meta(f"    • {name}  —  {desc_text}")
                    else:
                        meta("    (无)")

                    # 自进化沉淀 skill
                    suggestion_path = Path(os.path.expanduser("~")) / ".jify" / "self_evolution" / "skills" / "cli_user.json"
                    console.print()
                    meta("  🧬 自进化沉淀 Skill:")
                    if suggestion_path.exists():
                        try:
                            raw = suggestion_path.read_text(encoding="utf-8").strip()
                            data = json.loads(raw) if raw else {}
                            suggestions = data.get("suggestions", [])
                            approved = set(data.get("approved", []))
                            approved_skills = [s for s in suggestions if s.get("name") in approved]
                            if approved_skills:
                                for s in approved_skills:
                                    name = s.get("name", "?")
                                    desc = s.get("description", "(无描述)")
                                    freq = s.get("frequency", 0)
                                    meta(f"    • {name}  —  {desc}  (触发 {freq} 次)")
                            else:
                                meta("    (无)")
                        except (json.JSONDecodeError, IOError) as e:
                            meta(f"    ⚠ 读取失败: {e}")
                    else:
                        meta("    (无)")
                    divider()
                    continue
                elif cmd == "model":
                    if not arg:
                        meta(f"  当前模型: {agent_cli.config.model}")
                        names = agent_cli.config.model_names
                        if names:
                            meta("  已配置模型:")
                            for n in names:
                                mc = agent_cli.config.get_model_config(n)
                                marker = " ← 当前" if mc and mc.model == agent_cli.config.model else ""
                                meta(f"    {n}  ({mc.provider if mc else '?'}){marker}")
                        else:
                            meta("  (未配置多模型，使用顶层 provider/model 字段)")
                        meta(f"  用法: /model <model_name>")
                        divider()
                        continue
                    if not agent_cli.config.activate_model(arg):
                        names = agent_cli.config.model_names
                        if names:
                            meta(f"  ✗ 未找到模型配置: {arg}")
                            meta(f"  可用: {', '.join(names)}")
                        else:
                            meta(f"  ✗ 未配置多模型列表，无法切换")
                            meta(f"  请在 config.yaml 的 models 字段中配置模型")
                    else:
                        meta(f"  已切换模型 → {arg}")
                        meta(f"    provider: {agent_cli.config.provider}")
                        meta(f"    base_url: {agent_cli.config.base_url}")
                    divider()
                    continue
                elif cmd == "sessions":
                    try:
                        sessions = agent_cli.agent.list_sessions(limit=20)
                        if not sessions:
                            meta("  (无历史会话)")
                        else:
                            meta(f"  最近 {len(sessions)} 个会话:")
                            for s in sessions:
                                sid = s.get("id", "?")
                                created = s.get("created_at", "?")
                                model = s.get("model", "?")
                                count = s.get("message_count", 0)
                                meta(f"    {sid}  {created}  {model}  ({count} 条消息)")
                    except Exception as e:
                        meta(f"  ⚠ 加载失败: {e}")
                    divider()
                    continue
                elif cmd == "resume":
                    if not arg:
                        meta("  用法: /resume <session_id>")
                        meta("  先用 /sessions 查看可用会话")
                        divider()
                        continue
                    try:
                        agent_cli.agent.load_conversation(arg)
                        meta(f"  已加载会话 {arg}")
                    except Exception as e:
                        meta(f"  ⚠ 加载失败: {e}")
                    divider()
                    continue
                elif cmd == "jify":
                    try:
                        agent_cli.chat(
                            "浏览当前目录下的项目，了解下整体的架构，把最终的理解生成Jify.md写在当前目录下")
                    except Exception as e:
                        meta(f"  ⚠ 了解项目失败: {e}")
                    divider()
                    continue

                else:
                    meta(f"  未知命令: /{cmd}，输入 /help 查看可用命令")
                    divider()
                    continue

            console.print() # 空行

            # 显示用户消息
            meta(f"You · {datetime.now().strftime('%H:%M')}")
            console.print(Text(f"{user_input.strip()}", style="bold"))

            # AI 回复
            meta(f"Jify · {datetime.now().strftime('%H:%M')}")
            try:
                agent_cli.chat(user_input)
            except KeyboardInterrupt:
                agent_cli.interrupt()
                console.print()
                meta("  [已中断]")
                console.print()
                continue
            divider()

    except (EOFError, KeyboardInterrupt):
        console.print()
        divider()
        session_id = agent_cli.agent.save_conversation()
        _conversation_saved = True
        if session_id:
            meta(f"  再见！本次全程对话已保存，使用 /resume {session_id} 可恢复")
        else:
            meta("  再见！")
        console.print()
    except Exception as e:
        console.print()
        console.print(Text(f"  ⚠ 错误: {e}", style=JifyTheme.RED))
    finally:
        if not _conversation_saved:
            try:
                agent_cli.agent.save_conversation()
            except Exception:
                pass  # 静默忽略保存失败
        agent_cli.cli_console.stop_p2p_listener()
        JifyCLI.cleanup_p2p()


def single_turn(user_input: str, think_stream: bool = False, safe_exec: bool = False) -> None:
    """单轮快速提问模式（python cli.py -q "你好"）"""
    agent_cli = JifyCLI(think_stream, safe_exec)

    console.print()
    meta(f"Jify · {datetime.now().strftime('%H:%M')}")
    try:
        agent_cli.chat(user_input)
    except KeyboardInterrupt:
        agent_cli.interrupt()
        console.print()
        meta("  [已中断]")
    finally:
        JifyCLI.cleanup_p2p()

def main() -> None:
    ensure_jify_home()

    parser = argparse.ArgumentParser(description="Jify CLI")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # 默认 CLI 模式（无子命令时走这里）
    parser.add_argument("-q", "--quick", type=str, default=None,
                        help="单轮快速提问，执行完毕后自动退出")
    parser.add_argument("--think-stream", action="store_true", default=False,
                        help="流式输出思考内容；开启后思考内容实时分段显示")
    parser.add_argument("--no-think-stream", action="store_false", dest="think_stream",
                        help="关闭思考内容流式输出")
    parser.add_argument("--safe-exec", action="store_true", default=False,
                        help="启用 exec 命令白名单模式，拦截危险命令")

    # gateway 子命令
    gw_parser = subparsers.add_parser("gateway", help="启动 Jify Gateway 服务")
    gw_parser.add_argument("--port", type=int, default=9090,
                           help="监听端口（默认 9090）")
    gw_parser.add_argument("--host", type=str, default="127.0.0.1",
                           help="监听地址（默认 127.0.0.1）")

    args, _ = parser.parse_known_args()

    # 子命令路由
    if args.command == "gateway":
        _run_gateway(args)
        return

    # 默认 CLI 模式
    if args.quick:
        single_turn(args.quick, args.think_stream, args.safe_exec)
    else:
        main_loop(args.think_stream, args.safe_exec)


def _run_gateway(args: argparse.Namespace) -> None:
    """启动 Jify Gateway 服务。"""
    from gateway import app as gw_app
    import uvicorn

    print(f"\n  Jify Gateway 启动中...")
    print(f"  登录: http://localhost:{args.port}")
    print(f"  聊天: http://localhost:{args.port}/chat\n")
    uvicorn.run(gw_app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
