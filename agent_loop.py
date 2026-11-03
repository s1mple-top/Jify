# -*- coding: utf-8 -*-
import asyncio
import json
import math
import concurrent.futures
import contextvars
import logging
import os
import time
import queue
import threading
import re
from collections import deque
from operator import length_hint
from typing import List, Dict, Any, Optional, Callable, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import yaml
import uuid

logger = logging.getLogger(__name__)

from context_manager import ContextManager

from model_client import get_model_client, format_api_error
from context_shed import persist_large_result, shed_tool_outputs, SUMMARY_HEADER
from jify_tool import registry, ToolCall, should_parallel, _MAX_WORKERS
from tools.registry import set_agent_config, reset_agent_config, accepts_timeout_param
from tools.call_context import (
    CallContext, set_call_context, reset_call_context,
)
from event_bus import event_bus, UIEvent
from plugins.hook_manager import hook_manager



# Agent Loop 核心
@dataclass
class Message:
    """对话消息"""
    # message_id: Optional[str] = None
    role: str  # system / user / assistant / tool
    content: str
    tool_calls: Optional[List[Dict]] = None
    reasoning_content: Optional[str] = None
    tool_call_id: Optional[str] = None
    # Anthropic 扩展思考块（有序），须随 thinking 块原样回传以支持多轮工具调用
    thinking_blocks: Optional[List[Dict]] = None


@dataclass
class ModelConfig:
    """单个模型配置 —— 包含 provider / model / base_url / api_key 的完整配置"""
    name: str            # 显示名称，如 "gpt-4o", "deepseek-chat"
    provider: str        # openai | anthropic
    model: str           # 实际模型 ID，如 "gpt-4o-2024-08-06"
    base_url: str = ""
    api_key: str = ""
    extra_body: dict = field(default_factory=dict)


@dataclass
class AgentConfig:
    """Agent 配置"""
    model: str = "Jify-Code pas 5"
    provider: str = "openai"  # openai | anthropic
    base_url: str = "https://Jify.xy/v1"
    api_key: str = ""
    tool_delay: float = 0.0
    max_workers: int = 8
    tool_timeout: float = 120.0  # 单次工具调用超时（秒）
    approval_tool_timeout: float = 180.0  # 审批类工具外层超时（秒），须大于审批等待 120s
    max_iterations: int = 5000  # 单轮最大迭代数（长程任务按天计，需覆盖数小时~一天的迭代）
    emergency_compress_limit: int = 5  # 单轮内允许的紧急压缩次数上限，每轮 run() 开始时复位
    extra_body: dict = field(default_factory=dict)  # 附加到 API 请求体的参数，按 provider 自行配置
    plugins_dir: str = "~/.jify/plugins" # 目录
    SelfEvolutionModel: str= ""
    SelfEvolutionTurn: int = 8  # 每 N 轮做一次画像提取
    enabled_plugins: Optional[List[str]] = None  # None = 全部加载
    context_compress_threshold: int = 1800000  # 总字符数超过此阈值时触发上下文压缩
    # 单轮长程折叠：按 API 轮次分组后，尾部保留的字符预算（按 token 预算
    # 换算的字符额度，不再用「保留最近 N 块」固定窗口）
    compact_keep_chars: int = 175000
    summary_chunk_chars: int = 60000  # 降级分块压缩时单块字符上限（整段压缩失败后才用）
    summary_prompt_cap: int = 200000  # 单次压缩摘要请求的原文上限（字符），超出直接 peel 不摘要
    summary_parallel_workers: int = 2  # 分段摘要并行度（1 = 串行），兼顾限流风险与折叠延迟
    summary_model: str = ""  # 独立摘要模型（空 = 用主模型），可指向便宜大窗模型
    summary_timeout: float = 600.0  # 摘要单次请求超时（秒），2 次尝试；整段压缩需给足，等待可被 Ctrl+C 中断
    # 工具结果摄入侧闸门（级 0）：超限即落盘，上下文只留 preview，避免巨型结果先进上下文再压
    tool_result_max_chars: int = 50000  # 单条工具结果上限（字符），<=0 关闭闸门
    tool_result_preview_chars: int = 2000  # 落盘后上下文中保留的 preview 字符数
    # 历史工具结果减重（级 1）：按条数触发，一次清到保留窗口，保留消息结构与 tool_call_id
    tool_shed_enabled: bool = True  # 级 1 总开关
    tool_shed_trigger_count: int = 20  # 未清空的工具结果超过此条数才动手（避免逐条清、反复改写前缀）
    tool_shed_keep_recent: int = 10  # 保留最近 N 条工具结果原文（工作现场）
    models: List[ModelConfig] = field(default_factory=list)  # 多模型配置列表
    active_model_name: Optional[str] = None  # 当前激活的模型配置名，None 表示 config.yaml 首个默认

    @classmethod
    def load_from_yaml(cls, path: str = None) -> "AgentConfig":
        """
        从 ~/.jify/config.yaml 加载配置，缺失字段用默认值填充。

        优先级：config.yaml 的值 > AgentConfig 的默认值
        """
        if path is None:
            path = os.path.expanduser("~/.jify/config.yaml")
        defaults = cls()

        if not os.path.exists(path):
            return defaults

        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        # 解析 models 列表
        models_raw = raw.get("models", [])
        models = []
        for entry in (models_raw if isinstance(models_raw, list) else []):
            if isinstance(entry, dict):
                models.append(ModelConfig(
                    name=entry.get("name", ""),
                    provider=entry.get("provider", "openai"),
                    model=entry.get("model", ""),
                    base_url=entry.get("base_url", ""),
                    api_key=entry.get("api_key", ""),
                    extra_body=entry.get("extra_body", {}),
                ))

        # 只解析 models 列表：有则取第一个，否则回退到默认值。
        if models:
            first = models[0]
            model = first.model
            provider = first.provider
            base_url = first.base_url
            api_key = first.api_key
            extra_body = first.extra_body
        else:
            model = defaults.model
            provider = defaults.provider
            base_url = defaults.base_url
            api_key = defaults.api_key
            extra_body = defaults.extra_body
            models.append(ModelConfig(
                name=model,
                provider=provider,
                model=model,
                base_url=base_url,
                api_key=api_key,
            ))

        return cls(
            model=model,
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            max_iterations=raw.get("max_iterations", defaults.max_iterations),
            emergency_compress_limit=raw.get("emergency_compress_limit", defaults.emergency_compress_limit),
            tool_delay=raw.get("tool_delay", defaults.tool_delay),
            max_workers=raw.get("max_workers", defaults.max_workers),
            tool_timeout=raw.get("tool_timeout", defaults.tool_timeout),
            approval_tool_timeout=raw.get("approval_tool_timeout", defaults.approval_tool_timeout),
            extra_body=extra_body,
            plugins_dir=raw.get("plugins_dir", defaults.plugins_dir),
            SelfEvolutionModel=raw.get("SelfEvolutionModel", defaults.SelfEvolutionModel),
            SelfEvolutionTurn=raw.get("SelfEvolutionTurn", defaults.SelfEvolutionTurn),
            enabled_plugins=raw.get("enabled_plugins", defaults.enabled_plugins),
            context_compress_threshold=raw.get("context_compress_threshold", defaults.context_compress_threshold),
            compact_keep_chars=raw.get("compact_keep_chars", defaults.compact_keep_chars),
            summary_chunk_chars=raw.get("summary_chunk_chars", defaults.summary_chunk_chars),
            summary_prompt_cap=raw.get("summary_prompt_cap", defaults.summary_prompt_cap),
            summary_parallel_workers=raw.get("summary_parallel_workers", defaults.summary_parallel_workers),
            summary_model=raw.get("summary_model", defaults.summary_model),
            summary_timeout=raw.get("summary_timeout", defaults.summary_timeout),
            tool_result_max_chars=raw.get("tool_result_max_chars", defaults.tool_result_max_chars),
            tool_result_preview_chars=raw.get("tool_result_preview_chars", defaults.tool_result_preview_chars),
            tool_shed_enabled=raw.get("tool_shed_enabled", defaults.tool_shed_enabled),
            tool_shed_trigger_count=raw.get("tool_shed_trigger_count", defaults.tool_shed_trigger_count),
            tool_shed_keep_recent=raw.get("tool_shed_keep_recent", defaults.tool_shed_keep_recent),
            models=models,
        )

    def get_model_config(self, name: str) -> Optional[ModelConfig]:
        """根据名称查找模型配置"""
        for m in self.models:
            if m.name == name:
                return m
    @property
    def model_names(self) -> List[str]:
        """
        返回所有已配置的模型名称
        """
        return [m.name for m in self.models]

    def activate_model(self, name: str) -> bool:
        """
        激活指定模型配置 —— 同步 provider / model / base_url / api_key / extra_body
        """
        mc = self.get_model_config(name)
        if not mc:
            return False
        self.model = mc.model
        self.provider = mc.provider
        self.base_url = mc.base_url
        self.api_key = mc.api_key
        self.extra_body = (mc.extra_body or {}).copy()
        self.active_model_name = name
        return True


def _get_provider_extra_body(config: "AgentConfig") -> dict:
    """根据 base_url 域名 / model 前缀精确识别供应商，自动选择 API extra_body 参数。"""
    base = (config.base_url or "").lower()
    model = (config.model or "").lower()
    extra = {}

    # if "minimax" in base or model.startswith("minimax"):
    #     extra["reasoning_split"] = True
    #     extra["include_usage"] = True
    if "bigmodel.cn" in base or model.startswith("glm"):  # GLM think模式默认开启
        extra["thinking"] = {"type":"enabled"}
    if "deepseek.com" in base or model.startswith("deepseek"):
        # DeepSeek 思考档位：服务端默认已开启 high。仅在用户显式配置 reasoning_effort 时，
        # 按 provider 映射为对应格式（anthropic → thinking(含 budget_tokens)/output_config，
        # openai → thinking/reasoning_effort）。
        effort = (config.extra_body or {}).get("reasoning_effort")
        if effort:
            if config.provider in ("anthropic", "claude"):
                if effort == "none":
                    extra["thinking"] = {"type": "disabled"}
                else:
                    # Anthropic 协议要求 budget_tokens 必填且 < max_tokens；
                    # 这里按档位给初值，最终由 AnthropicClient 依实际 max_tokens 再裁剪。
                    budget = {"low": 2048, "medium": 4096, "high": 8192}.get(
                        str(effort).lower(), 4096
                    )
                    extra["thinking"] = {"type": "enabled", "budget_tokens": budget}
                    extra["output_config"] = {"effort": effort}
            else:
                extra["thinking"] = {"type": "disabled" if effort == "none" else "enabled"}
                if effort != "none":
                    extra["reasoning_effort"] = effort

    return extra



# Agent Loop
def _resolve_tool_timeout(td, config: "AgentConfig") -> float:
    """
    解析单次工具调用的外层超时（秒）。

    优先级：工具显式注册 timeout > config.tool_timeout，
    其中审批类工具（requires_approval=True）最低取 config.approval_tool_timeout
    （须 > 审批等待 120s，否则用户批准了、文件也改了，但外层已报假超时）。

    注意 approval_tool_timeout 是下限而非候选值：即便是显式注册了 timeout 的
    审批类工具，也必须在两者中取大，否则审批保护会被显式值绕过。
    """
    if td and td.timeout is not None:
        timeout = td.timeout
    else:
        timeout = config.tool_timeout
    if td and td.requires_approval:
        timeout = max(timeout, config.approval_tool_timeout)
    return timeout


# 单次调用超时的硬上限（秒）。工具参数里的 timeout 来自模型，属外部输入，
# 不设上限时一次调用就能把外层超时推到任意大，等于变相取消超时保护。
_MAX_CALL_TIMEOUT = 3600.0

# 参数 timeout 之外留给工具「超时后收尾」的余量：内层 subprocess/future 超时后
# 还要返回结果、写输出，外层必须略大于内层，否则外层先超时会误报。
_CALL_TIMEOUT_MARGIN = 30.0


def _resolve_call_timeout(td, config: "AgentConfig", args: Any = None) -> float:
    """解析单次工具调用超时 = 注册值/配置档 与 参数 timeout 取大。

    模型的工具参数里可以带 timeout（如 code_sandbox 跑长样本、team_delegate 等长任务），
    旧实现完全忽略它，导致这些参数被外层超时提前掐断且模型无从知晓。

    规则：
    - 仅当工具接受 timeout 参数时才认参数值（判定见 registry.accepts_timeout_param）；
    - 只放大不放小：参数需严格大于基准值才生效，避免模型把快工具压到 1s；
    - 参数非法（非数字 / bool / 非正数 / NaN / inf）时忽略，回落基准值；
    - 叠加余量后封顶 _MAX_CALL_TIMEOUT，params 是外部输入不能无限放大。
    """
    base = _resolve_tool_timeout(td, config)
    if not isinstance(args, dict):
        return base
    raw = args.get("timeout")
    # bool 是 int 子类，必须显式排除；NaN/inf 会让后续比较全部失效，也排除
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return base
    if not math.isfinite(raw):
        return base
    if raw <= 0 or float(raw) <= base:
        return base
    if not accepts_timeout_param(td.handler if td else None):
        return base
    return min(float(raw) + _CALL_TIMEOUT_MARGIN, _MAX_CALL_TIMEOUT)


def _submit_daemon(fn, *args):
    """在 daemon 线程中执行 fn，返回 (Future, CallContext)。

    不用 ThreadPoolExecutor 的原因：concurrent.futures.thread 会把每个 worker
    注册进 atexit，解释器退出时要 join 全部 worker。工具永久 hang 时线程无法强杀，
    这些 worker 会让进程退不出去（CLI 卡在退出）。

    daemon 线程不参与 atexit join，超时后可直接丢弃。
    限制仍在：Python 无法强杀线程；但通过 CallContext，工具登记的外部资源
    （子进程 / 容器）可在超时后被真正终止，工具线程随之自行返回。

    必须用 copy_context().run 而非裸线程：裸 threading.Thread 不继承 contextvars，
    工具将拿不到调用上下文（也无法登记 killer）。
    """
    fut: concurrent.futures.Future = concurrent.futures.Future()
    ctx = CallContext()
    parent = contextvars.copy_context()

    def _runner():
        # cancel() 生效时不执行 fn，语义与 executor 一致
        if not fut.set_running_or_notify_cancel():
            return

        def _inner():
            token = set_call_context(ctx)
            try:
                return fn(*args)
            finally:
                reset_call_context(token)

        try:
            fut.set_result(parent.run(_inner))
        except BaseException as e:  # 含 SystemExit，保证 Future 一定有结果
            fut.set_exception(e)

    threading.Thread(target=_runner, daemon=True, name="jify-tool-call").start()
    return fut, ctx


def _tool_timeout_result(timeout: float, ctx: Optional[CallContext] = None) -> str:
    """构造超时结果。

    必须如实告知当前处于哪种状态，两者对模型的下一步决策完全不同：

    - 已终止：工具登记的外部资源（子进程 / 容器）已被杀掉，执行确实停住了，
      但副作用可能只做了一半（命令跑到中途、文件写了一半）。
    - 无法终止：Python 线程无法强杀，工具仍在后台跑，副作用还在持续发生。

    若不说清楚，模型会把两种情况都当成「失败」直接重试，导致重复执行。
    """
    results = list(ctx.kill_results) if ctx is not None else []
    summary = ctx.summary() if ctx is not None else None

    if results:
        all_ok = all(ok for _, ok, _ in results)
        if all_ok:
            head = (
                f"工具执行超时（{timeout:g}s），已停止等待并终止其占用的外部资源"
                f"（{summary}）。执行已确实停止，但副作用可能已部分发生"
                "（如命令执行到中途、文件未写完）。"
            )
            payload = {"timed_out": True, "terminated": True}
        else:
            head = (
                f"工具执行超时（{timeout:g}s），已尝试终止其外部资源，部分失败"
                f"（{summary}）。残留资源可能仍在运行。"
            )
            payload = {"timed_out": True, "terminated": False, "may_still_be_running": True}
    else:
        head = (
            f"工具执行超时（{timeout:g}s），已停止等待并发出取消信号。"
            "支持协作式取消的工具会在下一个检查点退出；其余工具仍会继续在后台运行，"
            "其副作用可能仍持续发生。"
        )
        payload = {
            "timed_out": True,
            "terminated": False,
            "cancel_requested": True,
            "may_still_be_running": True,
        }

    payload["error"] = head + "请先检查实际状态再决定是否重试，避免重复执行。"
    return json.dumps(payload, ensure_ascii=False)


def _result_is_error(raw: str) -> bool:
    """判断工具结果字符串是否表示失败。

    error 字段按真值判断而非「键存在」：code_sandbox 正常完成时返回
    {"success": true, "error": null}，用 `"error" in parsed` 会把成功误判成失败。
    """
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return False
    return isinstance(parsed, dict) and bool(parsed.get("error"))


def _timed_out_call(tc: "ToolCall", timeout: float, elapsed: float,
                    ctx: Optional[CallContext] = None) -> "ToolCall":
    """为超时的工具调用构造独立结果对象。

    不复用原对象：后台线程仍在运行，若它稍后回写 tc.result，会与超时结果产生竞态，
    导致同一次调用在 messages 里出现「先报超时、后变成成功」的不一致记录。
    """
    return ToolCall(
        id=tc.id,
        name=tc.name,
        args=tc.args,
        result=_tool_timeout_result(timeout, ctx),
        duration=elapsed,
        error=True,
    )


_COMPACT_RETRY_MARKER = "[上下文压缩重试锚点]"


class AgentLoop:
    """
    Agent 循环核心类
    """

    def __init__(self, model_client: Optional[Any] = None,
                 agent_config: Optional[AgentConfig] = None):
        self.messages: List[Message] = []
        self.iteration_count = 0
        self._interrupt_requested = False
        self._interrupt_event = threading.Event()  # 中断事件
        # 优先用外部注入的配置，否则从 config.yaml 加载，再否则用默认值
        self.config = agent_config or AgentConfig.load_from_yaml()
        self._injected_client = model_client

        # 会话级上下文管理器（增量压缩，每 3 轮通过 LLM 追加摘要到旁路，不干扰当前上下文，只方便最后的替换）
        self.ctx = ContextManager(summarizer=self._summarize)

        # 会话持久化（cli.py 退出时保存到 SQLite）
        self._session_messages: List[Message] = []
        self._session_messages_lock = threading.Lock()
        self._session_id: Optional[str] = None
        self._resume_messages: List[Message] = []  # /resume 加载的历史消息
        self._last_api_error: Optional[str] = None
        self._emergency_count: int = 0  # 本轮紧急压缩累计次数，run() 开始时复位（见 emergency_compress_limit）
        self._turn_start_idx: int = 0  # 本轮新增消息的起始索引（折叠前缀时保留尾部）
        self._turn_archived: bool = True  # 本轮消息是否已归档进 _session_messages（True=无待归档）
        # 单轮长程折叠：分组与保留预算都在压缩时现算（按 assistant 消息起点
        # 切分 API 轮次），不再维护迭代块边界索引，省掉平移/失效逻辑
        # 压缩前最近读取过的文件路径，折叠后作为「召回提示」写进摘要消息
        self._recent_read_paths: deque = deque(maxlen=20)
        # 摘要熔断：连续失败达上限后冷却期内直接短路，避免长任务被反复超时拖死
        # （分段摘要并行时由同一把锁串行化计数，避免 += 丢计数导致提前/延后熔断）
        self._summary_lock = threading.Lock()
        self._summary_fail_streak: int = 0
        self._summary_cooldown_until: float = 0.0
        self._active_turn_transcript: Optional[List[Dict]] = None  # run() 本轮 transcript 引用
        self._console: Any = None  # 本轮 console 引用（压缩进度实时回报用）

        # 活跃流引用：ESC 中断时关闭底层 HTTP 连接
        self._active_stream: Any = None

    @property
    def model_client(self):
        """懒加载 client，同一参数只创建一次（线程安全）"""
        if self._injected_client is not None:
            return self._injected_client
        return get_model_client(
            provider=self.config.provider,
            api_key=self.config.api_key or None,
            base_url=self.config.base_url or None,
        )

    def list_sessions(self, limit: int = 20):
        """列出最近会话"""
        from storage.db import get_db
        return get_db().list_sessions(limit)


    # 对话接口
    def clear_history(self) -> None:
        """清除所有累积的消息历史，重置上下文。"""
        self.ctx.shutdown()
        self.messages.clear()
        self.iteration_count = 0
        self._interrupt_requested = False
        self._interrupt_event.clear()
        self._last_api_error = None
        self.ctx = ContextManager(summarizer=self._summarize)
        self._turn_archived = True
        # 摘要熔断状态一并重置：/clear 是用户明确的"重来"信号，
        # 保留冷却期会让清空后的首次压缩直接跳过 LLM 走规则兜底
        with self._summary_lock:
            self._summary_fail_streak = 0
            self._summary_cooldown_until = 0.0
        with self._session_messages_lock:
            self._session_messages.clear()

    def clear_reasoning_content(self) -> None:
        """切换模型后清空历史消息中的 reasoning_content，避免跨供应商残留污染。

        reasoning_content 是思考模型（DeepSeek/GLM 等）上一轮的思考原文，仅在
        同供应商的多轮 tool call 循环中续接使用。切换供应商后原样回传可能被
        严格校验的网关拒绝，故切换时统一清空（文本上下文 content 不受影响，
        模型切换回来会基于文本重新思考）。
        """
        for m in self.messages:
            if m.reasoning_content:
                m.reasoning_content = None
            # 模型切换后签名不再有效，必须同时清空，否则会被当作非法 thinking 块回传
            m.thinking_blocks = None
        with self._session_messages_lock:
            for m in self._session_messages:
                if m.reasoning_content:
                    m.reasoning_content = None
                m.thinking_blocks = None

    # 会话持久化
    def save_conversation(self) -> Optional[str]:
        """将当前会话全部消息写入 SQLite，返回 session_id。

        保存策略：
        - 保留 user 消息和 assistant 文本回复
        - assistant 的 tool_calls 转为文本描述合并到 content（不保留原始 tool_calls 字段）
        - 跳过 system / tool 消息（system 在 resume 时重建，tool 结果不保存）
        """
        from storage.db import get_db

        with self._session_messages_lock:
            messages = list(self._session_messages)

        if not messages:
            # 兜底：异常路径可能跳过归档（_session_messages 为空），回退到内存中
            # 的完整消息序列（resume 历史 + 本轮），避免整轮对话丢失且无法 /resume
            messages = [m for m in self.messages if m.role != "system"]
            if not messages:
                return None

        raw = []
        for m in messages:
            # 跳过 system（resume 时重建）和 tool 结果
            if m.role in ("system", "tool"):
                continue

            content = m.content or ""

            # assistant 消息：将 tool_calls 转为文本描述，合并到 content
            tc = getattr(m, "tool_calls", None)
            if m.role == "assistant" and tc:
                tc_text = self._format_tool_calls_text(tc)
                if content:
                    content = content + "\n\n" + tc_text
                else:
                    content = tc_text

            raw.append({"role": m.role, "content": content})

        db = get_db()
        if self._session_id is None:
            self._session_id = db.create_session(
                jify_name="cli",
                model=getattr(self.config, "model", ""),
            )
        db.save_messages(
            self._session_id, raw,
            jify_name="cli",
            model=getattr(self.config, "model", ""),
        )
        # 保存后清空，防止重复保存
        with self._session_messages_lock:
            self._session_messages.clear()
        return self._session_id

    def _persist_stream(self, role: str, content: str, tool_name: str = "") -> None:
        """旁路持久化：全量消息（含 tool 消息）实时落库到 stream 表。

        修复「压缩黑洞」：messages/save_conversation 中的压缩与覆盖只影响
        活上下文，stream 表 append-only 永不删除，供 session_search 检索召回。
        落库失败静默（检索是旁路能力，不阻塞主流程）。返回的 stream id 挂在
        Message._stream_id 上，供折叠时精确打 compacted 标记。
        """
        try:
            from storage.stream import get_stream_store
            if self._session_id is None:
                from storage.db import get_db
                self._session_id = get_db().create_session(
                    jify_name="cli",
                    model=getattr(self.config, "model", ""),
                )
            from storage.stream import set_current_session
            set_current_session(self._session_id)
            return get_stream_store().append(
                self._session_id, role, content, tool_name)
        except Exception:
            logger.debug("stream persist failed", exc_info=True)
            return None

    def _mark_folded_stream(self, old_messages: List[Message]) -> None:
        """前缀折叠/全量替换后，把被删除消息在 stream 表标记 compacted=1。

        P0 标记位方案：压缩只改标记（active=0），原文保留在库里、仍可检索。
        通过 Message._stream_id 与内存对象精确对应，不依赖下标推算。
        """
        try:
            kept = {id(m) for m in self.messages}
            ids = [m._stream_id for m in old_messages
                   if id(m) not in kept and getattr(m, "_stream_id", None)]
            if ids and self._session_id:
                from storage.stream import get_stream_store
                get_stream_store().mark_compacted(self._session_id, ids)
        except Exception:
            logger.debug("stream fold mark failed", exc_info=True)

    def _replace_messages(self, new_msgs: List[Message]) -> None:
        """整体替换 messages 序列，并把被替换掉的消息在 stream 表标记 compacted=1。

        所有折叠/截断路径的统一出口：替换动作与存档标记绑定，避免某条路径
        忘了打标记导致「活上下文已删、库里仍显示未压缩」。
        """
        old = list(self.messages)
        self.messages = new_msgs
        self._mark_folded_stream(old)

    def _archive_current_turn(self) -> None:
        """把本轮新增消息归档进 _session_messages（幂等，每轮至多归档一次）。

        正常完成 / 异常收尾路径各调用一次，run() 的 finally 再兜底一次：
        Ctrl+C 硬中断会直接展开调用栈、跳过收尾路径的归档代码，若不补救，
        _session_messages 为空 → save_conversation 返回 None → /exit 后
        无法生成 /resume 会话，整轮对话丢失。
        """
        if self._turn_archived:
            return
        self._turn_archived = True
        with self._session_messages_lock:
            self._session_messages.extend(
                [m for m in self.messages[self._turn_start_idx:] if m.role != "system"]
            )

    def load_conversation(self, session_id: str) -> List[Message]:
        """从 SQLite 加载会话消息，返回 Message 列表并存入 _resume_messages。

        resume 历史以**真实 message** 形式在本轮注入（append 到 messages 头部），
        不再重格式化为单条 user 文本写入 session_summary。
        """
        from storage.db import get_db

        db = get_db()
        raw = db.load_messages(session_id)

        messages: List[Message] = []
        for d in raw:
            m = Message(role=d["role"], content=d["content"])
            if d.get("tool_calls"):
                m.tool_calls = d["tool_calls"]
            if d.get("tool_call_id"):
                m.tool_call_id = d["tool_call_id"]
            messages.append(m)

        self._resume_messages = messages
        self._session_id = session_id
        return messages

    def _repair_dangling_tool_calls(self) -> None:
        """补齐上一轮被中断遗留的悬空 tool_calls。

        Ctrl+C 落在工具执行 / 审批等待期间时，assistant(tool_calls) 已 append，
        但对应的 role="tool" 结果未 append，序列会永久非法（OpenAI 兼容网关 400：
        assistant message with 'tool_calls' must be followed by tool messages；
        Anthropic：tool_use 缺少 tool_result），且每次请求都会同样失败。

        一轮内 Ctrl+C 直接展开调用栈、没有修复时机，故只在 run() 入口做一次
        全量配对检查：缺失的 tool_call_id 就地补一条中断结果。仅比较 role 与
        tool_call_id，不触碰 content。
        """
        if not self.messages:
            return

        repaired: List[Message] = []
        missing_total = 0
        i = 0
        n = len(self.messages)
        while i < n:
            msg = self.messages[i]
            repaired.append(msg)
            i += 1
            if msg.role != "assistant" or not msg.tool_calls:
                continue

            expected = []
            for tc in msg.tool_calls:
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tc_id:
                    expected.append(tc_id)
            if not expected:
                continue

            # 收集紧随其后的 tool 结果，已应答的 id 记账
            answered = set()
            while i < n and self.messages[i].role == "tool":
                tm = self.messages[i]
                if tm.tool_call_id:
                    answered.add(tm.tool_call_id)
                repaired.append(tm)
                i += 1

            for tc_id in expected:
                if tc_id not in answered:
                    repaired.append(Message(
                        role="tool",
                        content="[Skipped — user interrupt]",
                        tool_call_id=tc_id,
                    ))
                    missing_total += 1

        if missing_total:
            self.messages = repaired
            logger.warning("repaired %d dangling tool_calls from interrupted turn", missing_total)

    def run(self, message_id: str, user_message: str, system_prompt: str, console,
            tool_schemas, initial_messages: Optional[List[Message]] = None) -> Dict[str, Any]:
        """run 入口：无论以何种方式退出（正常 / API 错误 / Ctrl+C 硬中断 / 任意异常），
        都保证本轮消息归档进 _session_messages，否则 /exit 后无法 /resume。"""
        try:
            return self._run_impl(message_id, user_message, system_prompt,
                                  console, tool_schemas, initial_messages)
        finally:
            self._archive_current_turn()

    def _run_impl(self, message_id: str, user_message: str, system_prompt: str, console, tool_schemas,
            initial_messages: Optional[List[Message]] = None) -> Dict[str, Any]:
        """
        启动一轮 Agent 对话

        Args:
            initial_messages: --resume 加载的历史消息（不含 system），
                              注入到本轮 system prompt 之后、user 消息之前。

        Returns:
            {
                "final_response": str,       # 最终回复文本
                "messages": List[Message],  # 完整消息历史
                "iterations": int,          # 消耗的迭代数
                "completed": bool,          # 是否正常完成
            }
        """
        self.iteration_count = 0
        self._last_api_error = None
        self._emergency_count = 0  # 紧急压缩额度按轮计：一天期任务不应被历史轮的失败额度锁死
        self._interrupt_requested = False
        self._interrupt_event.clear()
        # 轮次起点兜底：上一轮异常退出（中断 / API 报错）可能残留思考原文，
        # 不清空会被当作本轮首次请求的 extra_body.reasoning_content 发给模型
        console.last_reasoning_content = ""
        from tools.approval import clear_break
        clear_break()

        run_start = time.time()

        console.token_num = 0

        # Hook before_prompt_build
        ctx = hook_manager.trigger("before_prompt_build", {
            "system_prompt": system_prompt,
            "user_message": user_message,
        })
        # Jify 内 hook 提供修改 sp 的能力，目的是用于企业级敏感信息过滤
        system_prompt = ctx.get("system_prompt", system_prompt)

        # 更新或预置 system prompt（不清理 messages，跨轮累积）
        if system_prompt:
            if self.messages and self.messages[0].role == "system":
                self.messages[0].content = system_prompt
            else:
                self.messages.insert(0, Message(role="system", content=system_prompt))

        # 本轮的原始用户消息（不含历史拼接），供 ctx.end_turn 记录
        raw_user_message = user_message

        # 持有 console 引用：压缩等长耗时同步操作需要实时回报进度
        # （CLI 运行期无 event_bus 消费者，走总线会看不到）
        self._console = console

        # 本轮 transcript，供 ctx.end_turn 记录
        _turn_transcript: List[Dict] = [{"role": "user", "content": user_message}]

        # --resume 历史以真实 message 注入（仅一次），不写入 session_summary
        _resume = initial_messages or self._resume_messages
        if _resume:
            for msg in _resume:
                if msg.role == "system":
                    continue
                self.messages.append(msg)
            self._resume_messages.clear()

        # 补齐上一轮中断遗留的悬空 tool_calls（必须在 append 本轮 user 之前，
        # 以免影响 _turn_start_idx；Ctrl+C 展开调用栈时本轮内无修复时机）
        self._repair_dangling_tool_calls()

        # append-only：历史不再重格式化，用户消息作为真实 message 追加到序列末尾
        self.messages.append(Message(role="user", content=user_message))
        # 旁路落库：写入即持久（P0），stream id 挂在消息上供折叠标记
        self.messages[-1]._stream_id = self._persist_stream("user", user_message)

        # 本轮起始索引 = 本轮 user 消息所在下标（折叠时保留「本轮 user + assistant + tool」）
        self._turn_start_idx = len(self.messages) - 1
        # 归档标志在本轮起点复位；若在此之前的早期阶段被中断，标志仍为上轮的
        # True，run() finally 的兜底归档 no-op，不会把上轮消息重复归档
        self._turn_archived = False
        # 轮内折叠状态：分组在压缩时现算，无需在此重置
        # 挂载本轮 transcript 引用（同一 list 对象），供路径 C 折叠时占位清理
        self._active_turn_transcript = _turn_transcript

        # Hook after_prompt_build
        hook_manager.trigger("after_prompt_build", {
            "messages": self.messages,
            "system_prompt": system_prompt,
            "user_message": user_message,
        })

        # 注册工具 schema（供模型参考）
        # tool_schemas = self._get_tool_schemas()
        # 启动跨轮动画（秒数在 _call_model / _execute_tools 期间不再冻结）
        if hasattr(console, 'start_round'):
            console.start_round()
        # 主循环
        is_interrupted = False
        final_msg = ""

        # 将当前 config 注入 contextvar，供工具执行期间 subagent_run 复用（覆盖流式预执行与兜底执行两条路径）
        _config_token = set_agent_config(self.config)

        # 限制 Loop 轮数
        while self.iteration_count < self.config.max_iterations:
            if self._interrupt_requested:
                break

            self.iteration_count += 1

            # 发送前预检：总字符超阈值先折叠前缀，避免带着超长序列发请求后
            # 一步跨过真实模型窗口导致 API 报错（把检查点从「迭代末」提前到「发请求前」）。
            # preserve_current=True：本轮尚未产出回复，绝不能把用户真实问题一并摘要化。
            # 与迭代末的检查同为幂等操作：未超阈值时 no-op。
            self._maybe_compact_prefix(preserve_current=True)

            # 模型推理（流式）
            # printer = threading.Thread(target=console.stream_printer,
            #                              daemon=True)
            # printer.start()

            # Hook llm_input
            hook_manager.trigger("llm_input", {
                "messages": self.messages,
                "iteration": self.iteration_count,
            })

            # 传递上轮思考内容给 API（思考模型需要 reasoning_content 传回）
            try:
                response = self._call_model(self.messages, tool_schemas, console.last_reasoning_content)
            except Exception as e:
                # 完整堆栈只在 debug 级别留档，默认只给一行友好提示
                logger.debug("API call failed at iteration %d", self.iteration_count, exc_info=True)
                # 紧急压缩：上下文溢出 → 压缩后重试。额度按轮累计（run() 开始复位），
                # 单轮内耗尽说明路径 C 折叠已无法收敛，继续重试无意义。
                if (self._emergency_count < self.config.emergency_compress_limit
                        and self._looks_like_context_overflow(e)):
                    self._emergency_count += 1
                    self._notify("[上下文溢出] 正在调用模型压缩上下文...")
                    self._emergency_compress(user_message)
                    continue
                self._last_api_error = str(e)
                tool_calls = []
                friendly = format_api_error(e)
                text_content = friendly
                # 单行红色提示：CLI 直接打印，WebUI 走 error 消息
                try:
                    console.report_api_error(friendly)
                except Exception:
                    pass
                event_bus.put(UIEvent("ERROR", friendly))
                break

            # event_bus.put(UIEvent("THINK_START","Jify Think ..."))
            # 流式消费 + 组装完整响应（v2: 流式工具执行，返回预执行结果）
            complete_text, tool_call_chunks, finish_reason, pre_results = \
                console.consume_stream(response, self._interrupt_event)

            self._active_stream = None  # 流已消费完毕或被中断，清除引用

            # 等待打印线程结束
            # printer.join(timeout=1)

            # 组装 tool_calls 并解析文本
            tool_calls = self._assemble_tool_calls(tool_call_chunks)
            text_content = self._extract_text_from_content(complete_text)

            # 流读取错误（stall / 服务端断流）被 consume_stream 吞掉后会返回
            # 空或不完整的 tool_calls。判据必须看 finish_reason 而不是「有没有
            # tool_calls」：流中途断掉时 tool_calls 可能已吐出大半，arguments 却是
            # 截断的 json（解析降级为 {}），拿去执行等于用空参数调用工具。
            # 只有 finish_reason 表明模型正常结束，才能认为 tool_calls 完整。
            _stream_error = getattr(console, "last_stream_error", None)
            _stream_complete = finish_reason in (
                "tool_calls", "stop", "length", "content_filter", "pause_turn")
            if (_stream_error is not None and not _stream_complete
                    and not self._interrupt_requested):
                if (self._emergency_count < self.config.emergency_compress_limit
                        and self._looks_like_context_overflow(_stream_error)):
                    self._emergency_count += 1
                    self._notify("[上下文溢出] 正在调用模型压缩上下文...")
                    self._emergency_compress(user_message)
                    continue
                self._last_api_error = str(_stream_error)
                friendly = format_api_error(_stream_error)
                # 半截文本不能当最终回复（会误导为正常产出），清空走错误收尾
                text_content = ""
                try:
                    console.report_api_error(friendly)
                except Exception:
                    pass
                event_bus.put(UIEvent("ERROR", friendly))
                break

            # 无 tool_calls → 结束
            if not tool_calls:
                # 纯对话轮：同样把 assistant 回复落回 messages，供下轮携带。
                # 否则历史里会堆积连续的 user 消息，模型会把上一轮问题再答一遍。
                self.messages.append(Message(
                    role="assistant",
                    content=text_content or "",
                    reasoning_content=console.last_reasoning_content,
                    thinking_blocks=console.last_thinking_blocks or None,
                ))
                self.messages[-1]._stream_id = self._persist_stream(
                    "assistant", text_content or "")
                # 与工具分支对齐：本轮流已消费完毕，清空思考原文，
                # 避免下一轮 _call_model 把它当成 extra_body.reasoning_content 回传
                console.last_reasoning_content = ""

                # Hook llm_output
                hook_manager.trigger("llm_output", {
                    "final_response": text_content or "",
                    "iterations": self.iteration_count,
                })

                # 归档本轮记录（并且停止状态栏）
                console.stop_live()

                console.flush_stream(is_final=True)

                _turn_transcript.append({"role": "assistant", "content": text_content or ""})
                # 上下文超长检测（纯对话轮无工具分支，需在此单独检查，否则折叠永不触发）
                # 放在 end_turn 之前：本轮尚未入 pending，折叠不会把本轮同时写进摘要
                # 又在尾部重复出现。
                self._maybe_compact_prefix()
                self.ctx.end_turn(raw_user_message, text_content,
                                  transcript=_turn_transcript,
                                  start_idx=self._turn_start_idx)

                # console.Nanswer()
                # event_bus.put(UIEvent("MESSAGE", "----------"))
                # event_bus.put(UIEvent("DONE"))
                # time.sleep(0.2)
                # event_bus.put(UIEvent("MESSAGE", text_content))
                # event_bus.put(UIEvent("MESSAGE",
                #                               "----------\r\n" + "used ctx str length (Not token len): " + str(
                #                                   len(self.ctx.get_session_summary())) + "\r\n"))
                console.stream_end()
                # 持久化本轮完整消息（只追加本轮新增，避免重复）
                self._archive_current_turn()
                # append-only：messages 序列保留，供下轮继续追加（前缀缓存友好）

                reset_agent_config(_config_token)
                elapsed = time.time() - run_start
                total_tokens = console.total_tokens_sent + console.total_tokens_recv
                return {
                    "final_response": text_content or "",
                    "messages": self.messages,
                    "iterations": self.iteration_count,
                    "completed": True,
                    "elapsed": elapsed,
                    "total_tokens": total_tokens,
                }

            # 审批 break 检查
            from tools.approval import break_requested
            if break_requested.is_set():
                break_requested.clear()
                self._notify("[审批] 用户终止当前任务。")
                # 审批打断不设 is_interrupted，避免产生 TaskCheckpoint 快照
                final_msg = "任务已被用户中断 (break)。"
                break

            # 中间轮：动画运行中渲染本轮输出 模型执行意图的吐出
            console.flush_stream()

            # 追加 assistant 消息
            self.messages.append(Message(
                role="assistant",
                content=text_content or "",
                reasoning_content=console.last_reasoning_content,
                thinking_blocks=console.last_thinking_blocks or None,
                tool_calls=tool_calls,
            ))
            # 旁路落库：assistant 文本 + tool_calls 文本化描述
            _stream_content = text_content or ""
            if tool_calls:
                _tc_text = self._format_tool_calls_text(tool_calls)
                _stream_content = (_stream_content + "\n\n" + _tc_text).strip() if _stream_content else _tc_text
            self.messages[-1]._stream_id = self._persist_stream(
                "assistant", _stream_content)

            # 统一约定：事件里传原始字符数，显示层统一做一次 //2 换算 token
            event_bus.put(UIEvent("Token_Send", len(text_content or "")))

            # tool_calls 的 arguments 会随 assistant 消息在后续每轮重发给模型，需计入 sent。
            # 一般工具参数很小可忽略，但 write_file/patch_file 的 arguments 是全文，占比极大。
            _args_chars = sum(
                len(str(tc.get("function", {}).get("arguments", "")))
                for tc in (tool_calls or [])
            )
            if _args_chars:
                event_bus.put(UIEvent("Token_Send", _args_chars))

            # 清空 reasoning_content，下轮 consume_stream 会重新填充
            console.last_reasoning_content = ""

            # 执行工具（传入预执行结果，已完成的直接跳过） 兜底策略，防止fire漏掉
            results = self._execute_tools(tool_calls, pre_results)

            # 注入工具结果到 messages
            for tc_result in results:
                raw_content = tc_result.result or ""
                _tool_name = getattr(tc_result, "name", "") or ""
                # 级 0 摄入侧闸门：单条超限即落盘，上下文只留 preview，
                # 避免巨型结果「先进上下文再被压缩」——那种路径下压缩请求
                # 自身就要处理整段巨型文本，且压缩失败会退化到行级截断。
                content = persist_large_result(
                    self._session_id or "unknown",
                    _tool_name,
                    tc_result.id,
                    raw_content,
                    self.config.tool_result_max_chars,
                    self.config.tool_result_preview_chars,
                )
                self.messages.append(Message(
                    role="tool",
                    content=content,
                    tool_call_id=tc_result.id,
                ))
                # 旁路落库：存**原始全文**（stream 表是 FTS 可检索的完整存档，
                # 落盘文件不可检索）。上下文的 preview 与存档的全文互不影响。
                self.messages[-1]._stream_id = self._persist_stream(
                    "tool", raw_content, tool_name=_tool_name)

                # 工具执行结果也会随 messages 发送给模型，估算 token 数计入 sent，其余 toolcallid 上方塞入
                if content:
                    event_bus.put(UIEvent("Token_Send", len(content)))

                # 单独领出来可以考虑放弃掉某些tool的结果，减少token的效果，不过效果会下降
                _turn_transcript.append({
                    "role": "tool",
                    "name": tc_result.name,
                    "tool_call_id": tc_result.id,
                    "result": content,
                })

            # 工具间延迟（可选）
            if self.config.tool_delay > 0:
                time.sleep(self.config.tool_delay)

            # 上下文超长检测
            # 旁路摘要注入：把历史折叠成一条合成摘要消息置于序列前端（一次性前缀替换），
            # 之后所有新轮次继续 append，两次折叠之间全程命中远程前缀缓存。
            self._maybe_compact_prefix()

        is_interrupted = self._interrupt_requested or is_interrupted
        if self._last_api_error:
            final_msg = text_content if text_content else f"[API 调用失败] {self._last_api_error}"
        elif is_interrupted:
            final_msg = final_msg or "任务被中断"
        elif not final_msg:
            final_msg = "达到最大迭代次数"
        # 异常收尾（中断 / API 报错 / 达最大迭代 / 审批 break）未经过无 tool_calls 分支的
        # end_turn，在本轮退出点统一归档。否则该轮内容既不进摘要、又会被后续折叠丢弃。
        # 正常收尾已在分支内 end_turn 并 return，此处不会重复。
        self.ctx.end_turn(raw_user_message, final_msg,
                         transcript=_turn_transcript,
                         start_idx=self._turn_start_idx)
        self._notify(final_msg)
        # 持久化本轮完整消息（只追加本轮新增，避免重复）
        self._archive_current_turn()
        # append-only：messages 序列保留
        reset_agent_config(_config_token)
        elapsed = time.time() - run_start
        total_tokens = console.total_tokens_sent + console.total_tokens_recv
        return {
            "final_response": final_msg,
            "messages": self.messages,
            "iterations": self.iteration_count,
            "completed": False,
            "interrupted": is_interrupted,
            "elapsed": elapsed,
            "total_tokens": total_tokens,
        }


    # 工具执行
    def _dispatch_with_config(self, name: str, args: Dict[str, Any]) -> str:
        """在 worker 线程中注入当前 config 后执行工具，供 subagent_run 复用主 Agent 的 config。"""
        token = set_agent_config(self.config)
        try:
            return registry.dispatch(name, args)
        finally:
            reset_agent_config(token)

    def _execute_tools(self, tool_calls: List[Dict[str, Any]],
                       pre_results: Dict[str, str] = None) -> List[ToolCall]:
        """
        执行一组 tool_calls，自动选择并行或串行。
        如果 pre_results 中已有结果，直接使用（流式预执行），不再重复调用。
        """
        pre_results = pre_results or {}
        # 按原始 tool_calls 下标占位，保证返回顺序与模型给出的顺序一致
        slots: List[Optional[ToolCall]] = [None] * len(tool_calls)
        enhanced: List[Tuple[int, ToolCall]] = []

        for idx, tc in enumerate(tool_calls):
            tcall = ToolCall(
                id=tc["id"],
                name=tc["function"]["name"],
                args=json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"] else {},
            )
            # 流式预执行命中（按 name:json(args) 匹配）
            _sig = f"{tcall.name}:{json.dumps(tcall.args, sort_keys=True)}"
            if _sig in pre_results:
                tcall.result = pre_results[_sig]
                tcall.duration = 0
                tcall.error = _result_is_error(tcall.result)
                slots[idx] = tcall
            else:
                enhanced.append((idx, tcall))

        # 全部预执行完成 → 直接返回
        if not enhanced:
            return [tc for tc in slots if tc is not None]

        # 剩余工具按策略执行
        pending = [tc for _, tc in enhanced]
        if should_parallel(pending):
            done = self._execute_parallel(pending)
        else:
            done = self._execute_sequential(pending)

        for (idx, _), tc_result in zip(enhanced, done):
            slots[idx] = tc_result

        return [tc for tc in slots if tc is not None]

    def _execute_parallel(self, tool_calls: List[ToolCall]) -> List[ToolCall]:
        """并行执行：每个工具独立超时，超时后不再等待后台线程。

        关键点：ThreadPoolExecutor 退出 with 时会 shutdown(wait=True) join 住 worker，
        把已检测到的超时原样抵消（表现为「超时形同虚设、永久 hang 卡死」）；
        即便手动 shutdown(wait=False)，worker 仍被 concurrent.futures.thread 注册进
        atexit，永久 hang 的线程会让进程退不出去。故改用 daemon 线程（_submit_daemon）。

        线程无法强杀，工具可能仍在后台跑完，故超时结果里提示副作用可能已发生。
        """
        from tools.approval import break_requested

        results: List[Optional[ToolCall]] = [None] * len(tool_calls)
        # 已放弃的调用：超时后由外层写入超时结果，工具线程的晚到回写必须被丢弃，
        # 否则同一调用会出现「先报超时、后变成工具自定义内容」的不一致记录
        abandoned: set = set()
        state_lock = threading.Lock()

        def _worker(index: int, tc: ToolCall):
            if self._interrupt_requested or break_requested.is_set():
                tc.result = "[Skipped — user interrupt]"
                tc.error = True
                tc.duration = 0
                with state_lock:
                    if index not in abandoned:
                        results[index] = tc
                return

            start = time.time()
            try:
                result = self._dispatch_with_config(tc.name, tc.args)
            except Exception as e:
                result = json.dumps({"error": str(e)})
            tc.result = result
            tc.duration = time.time() - start
            tc.error = _result_is_error(result)

            with state_lock:
                if index not in abandoned:
                    results[index] = tc

        workers = min(len(tool_calls), self.config.max_workers, _MAX_WORKERS)
        # 批内并发上限：槽位被永久 hang 的工具占满时，其余工具等不到槽位，
        # 由下方 deadline 逻辑判超时返回，而不是无限排队
        slots = threading.BoundedSemaphore(workers)

        def _guarded(index: int, tc: ToolCall, wait_budget: float):
            if not slots.acquire(timeout=max(wait_budget, 0)):
                return  # 未拿到槽位，results[index] 保持 None，由外层按 deadline 判超时
            try:
                _worker(index, tc)
            finally:
                slots.release()

        # 每个工具各自 deadline（从提交时刻起算），避免一个慢工具拖住整批
        # 参数里的 timeout 参与计算，否则模型请求的长任务会被注册值提前掐断
        submit_at = time.time()
        deadlines = [
            submit_at + _resolve_call_timeout(registry.get(tc.name), self.config, tc.args)
            for tc in tool_calls
        ]

        submitted = [
            _submit_daemon(_guarded, i, tc, deadlines[i] - submit_at)
            for i, tc in enumerate(tool_calls)
        ]
        futures = [f for f, _ in submitted]
        ctxs = [c for _, c in submitted]

        aborted = False
        try:
            while True:
                pending = [i for i, f in enumerate(futures) if not f.done()]
                if not pending:
                    break
                if self._interrupt_requested or break_requested.is_set():
                    aborted = True
                    for f in futures:
                        f.cancel()
                    break
                now = time.time()
                # 尚未完成的工具全部超时，才结束等待
                if all(now >= deadlines[i] for i in pending):
                    break
                wait_for = min(deadlines[i] - now for i in pending)
                concurrent.futures.wait(futures, timeout=min(0.2, max(wait_for, 0.01)))
        except BaseException:
            # Ctrl+C 会从这里的等待直接抛出，栈展开后走不到下方的 cancel 逻辑，
            # 未完成工具的线程（subagent / team）会在后台继续跑完。
            # 必须在逃逸前把取消信号 + killer 发出去。
            for i, f in enumerate(futures):
                if not f.done():
                    f.cancel()
                    ctxs[i].cancel()
            raise

        # 快照：此刻仍未完成的调用才是真正被放弃的（之后线程可能因资源被杀而返回）
        finished = {i for i, f in enumerate(futures) if f.done()}

        # 先标记放弃，阻止工具线程晚到回写；再释放锁做清理（kill 可能耗时数秒）
        with state_lock:
            abandoned.update(i for i in range(len(futures)) if i not in finished)

        # 放弃等待的调用必须终止其登记的外部资源（子进程 / 容器），否则进程残留。
        # 线程本身杀不掉，但 cancel() 会同时置位取消信号，让支持协作式退出的工具
        # （subagent / team）在下个检查点自行返回——这类工具没有可杀的资源。
        for i in sorted(abandoned):
            ctxs[i].cancel()

        # 兜底：超时 / 被取消的调用统一填充，调用方绝不能拿到 None
        with state_lock:
            for i, tc in enumerate(tool_calls):
                if results[i] is not None:
                    continue
                if aborted:
                    # 同样换独立对象，避免 worker 稍后回写造成竞态
                    results[i] = ToolCall(
                        id=tc.id,
                        name=tc.name,
                        args=tc.args,
                        result="[Skipped — user interrupt]",
                        duration=time.time() - submit_at,
                        error=True,
                    )
                else:
                    results[i] = _timed_out_call(
                        tc, deadlines[i] - submit_at, time.time() - submit_at, ctxs[i]
                    )

        return results

    def _execute_sequential(self, tool_calls: List[ToolCall]) -> List[ToolCall]:
        """串行执行：每次用一个独立线程包装，支持超时 + interrupt + 审批 break"""
        from tools.approval import break_requested

        for i, tc in enumerate(tool_calls):
            if self._interrupt_requested or break_requested.is_set():
                for remaining in tool_calls[i:]:
                    remaining.result = "[Skipped — user interrupt]"
                    remaining.error = True
                break

            td = registry.get(tc.name)
            # 参数里的 timeout 参与计算，与并行路径保持一致
            timeout = _resolve_call_timeout(td, self.config, tc.args)

            start = time.time()
            # 用 daemon 线程而非 ThreadPoolExecutor：后者 worker 会被注册进 atexit，
            # 永久 hang 时进程退不出去（详见 _submit_daemon）
            future, call_ctx = _submit_daemon(self._dispatch_with_config, tc.name, tc.args)
            try:
                result = future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                # 线程无法强杀，但可终止其登记的外部资源；kill 不掉的部分靠取消信号
                call_ctx.cancel()
                result = _tool_timeout_result(timeout, call_ctx)
                tc.error = True
            except Exception as e:
                result = json.dumps({"error": str(e)})
                tc.error = True
            except BaseException:
                # Ctrl+C 等中断：subagent / team 没登记可杀资源，只能靠取消信号
                # 让它在下一个检查点退出；不 cancel 就会在后台跑完全程。
                # 必须排在 except Exception 之后：BaseException 是 Exception 的父类，
                # 放前面会把所有业务异常一并吞掉并向上抛，导致工具错误无法回传给模型。
                call_ctx.cancel()
                raise
            else:
                tc.error = False
            finally:
                future.cancel()
            tc.result = result
            tc.duration = time.time() - start
            if not tc.error:
                tc.error = _result_is_error(result)

        return tool_calls


    # 模型接口
    def _call_model(self, messages: List[Message],
                    tool_schemas: List[Dict],
                    reasoning_content: str = "") -> Any:
        """
        调用模型。流式返回生成器，由 _consume_stream 消费。
        """
        if self._injected_client is not None or self.config.provider:
            api_messages = []
            length_ = 0
            for msg in messages:
                m = {"role": msg.role, "content": msg.content}
                if msg.reasoning_content:  # 新增
                    m["reasoning_content"] = msg.reasoning_content
                # thinking 块仅 Anthropic 有意义，其他 provider 收到未知字段会报错
                if self.config.provider in ("anthropic", "claude"):
                    if msg.thinking_blocks:
                        m["thinking_blocks"] = msg.thinking_blocks
                if msg.tool_calls:
                    m["tool_calls"] = msg.tool_calls
                if msg.tool_call_id:
                    m["tool_call_id"] = msg.tool_call_id
                api_messages.append(m)
                length_ = length_ + len(str(m))

            # 预检：上下文过大时提前警告（粗估：1 token ≈ 2 chars，128K ≈ 256K chars）
            # if length_ > 500000:
            #     event_bus.put(UIEvent("MESSAGE",
            #         f"⚠ 上下文较大 ({length_} chars, ~{length_ // 2} tokens)，可能导致连接中断。"))

            extra_body = self.config.extra_body.copy()
            extra_body.update(_get_provider_extra_body(self.config))
            # reasoning_content 仅对 OpenAI 兼容供应商有意义；Anthropic 请求体没有该字段，
            # 透传会被当作非法参数拒绝。
            if reasoning_content and self.config.provider not in ("anthropic", "claude"):
                extra_body["reasoning_content"] = reasoning_content

            # Hook before_api_call
            hook_manager.trigger("before_api_call", {
                "api_messages": api_messages,
                "model": self.config.model,
                "tool_schemas": tool_schemas,
            })

            response = self.model_client.chat(
                messages=api_messages,
                tool_schemas=tool_schemas,
                model=self.config.model,
                stream=True,
                extra_body=extra_body
            )

            # 保存活跃流引用，供 interrupt() 关闭底层连接
            self._active_stream = response

            # Hook after_api_call
            hook_manager.trigger("after_api_call", {
                "api_messages": api_messages,
                "model": self.config.model,
                "response": response,
            })

            return response

    # 响应解析
    def _assemble_tool_calls(self, tool_call_chunks: Dict[str, Dict]) -> List[Dict[str, Any]]:
        """从 tool_call_chunks dict 组装 tool_calls 列表"""
        if not tool_call_chunks:
            return []

        tool_calls = []
        for idx in sorted(tool_call_chunks.keys()):
            tc_data = tool_call_chunks[idx]
            fn = tc_data.get("function", {})
            name = fn.get("name", "") or ""
            args_str = fn.get("arguments", "{}") or "{}"

            # 解析 arguments 为 dict，再序列化为 JSON string
            try:
                args = json.loads(args_str)
            except Exception:
                args = {}
            tool_calls.append({
                "id": tc_data.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args)
                }
            })
        return tool_calls

    def _extract_text_from_content(self, text: str) -> str:
        """从文本内容中提取最终回复（去掉 <thinking> 标签）"""
        if not text:
            return ""
        # 去掉 <thinking> 标签内容
        outside_think = re.sub(r'<thinking>.*?</thinking>', '', text,
                               flags=re.DOTALL).strip() or ""
        return outside_think

    _SUMMARY_FAIL_LIMIT = 2       # 连续失败次数达此值触发熔断
    # 冷却时长按连续失败次数指数退避，封顶 60s。固定 60s 的代价是 LLM 短抖动
    # （连续 2 次超时）后，后续数十次折叠全部降级为规则截断；退避让抖动后
    # 2s 即可恢复试探，持续不可用才逐步拉长到 60s。
    _SUMMARY_COOLDOWN_STEPS = (2.0, 8.0, 30.0, 60.0)

    def _summary_cooldown_sec(self, streak: int) -> float:
        """按连续失败次数取冷却时长：streak 从 FAIL_LIMIT 起算，逐级退避封顶。"""
        idx = max(0, min(streak - self._SUMMARY_FAIL_LIMIT,
                         len(self._SUMMARY_COOLDOWN_STEPS) - 1))
        return self._SUMMARY_COOLDOWN_STEPS[idx]

    def _record_summary_failure(self) -> None:
        """记录一次摘要失败；达上限则按失败次数开启指数退避冷却窗口。"""
        with self._summary_lock:
            self._summary_fail_streak += 1
            if self._summary_fail_streak >= self._SUMMARY_FAIL_LIMIT:
                self._summary_cooldown_until = (
                    time.time() + self._summary_cooldown_sec(self._summary_fail_streak))

    def _summarize(self, prompt: str, count_failure: bool = True) -> str:
        """调用 LLM 做摘要压缩（优先用独立 summary_model，未配置则用主模型）

        连续失败达上限后进入冷却期：期间直接返回空串，由调用方走规则兜底，
        避免长任务里每次折叠都等一次超时。冷却结束自动放行一次探活。

        Args:
            count_failure: 失败是否计入熔断。单次整段压缩属于「可降级试探」
                （失败后还有分块压缩兜底），传 False 可避免一次探路失败就
                消耗熔断额度、把后续分块降级路径一并短路。

        可中断性：摘要调用 stream=False，不写 self._active_stream，interrupt()
        的 close() 对它无效——长任务里上下文一超阈值就会同步调它，若服务端
        不返回，主线程会卡死在压缩阶段（表现为「loop 停住、没有请求发出、
        Ctrl+C 也退不出来」，只有 /clear 清空消息后才恢复）。故改为在后台
        线程发起请求，主线程只做可中断的等待：中断信号一来立即返回空串，
        由调用方走规则兜底（质量降级但任务不死）。
        """
        if self._summary_fail_streak >= self._SUMMARY_FAIL_LIMIT \
                and time.time() < self._summary_cooldown_until:
            return ""
        # 软中断：摘要属于旁路辅助调用，用户请求打断时不再发起新的模型请求
        if self._interrupt_event.is_set():
            return ""

        box: Dict[str, Any] = {}

        def _call():
            try:
                box["resp"] = self.model_client.chat(
                    messages=[{"role": "user", "content": prompt}],
                    tool_schemas=[],
                    model=self.config.summary_model or self.config.model,
                    stream=False,
                    # 摘要有规则兜底 + 熔断，超时按配置（默认 2 × 90s）：
                    # 原先 3 × 240s 意味着单次失败最多占用 12 分钟主线程，
                    # 用户侧表现为彻底卡死
                    retry_timeout_seq=(float(self.config.summary_timeout),) * 2,
                )
            except BaseException as e:  # noqa: BLE001 - 后台线程：任何异常都不能逃逸
                box["err"] = e

        worker = threading.Thread(target=_call, daemon=True)
        worker.start()
        # 可中断等待：轮询中断信号，避免 Ctrl+C 后仍被摘要请求挂住
        while worker.is_alive():
            if self._interrupt_event.is_set():
                if count_failure:
                    self._record_summary_failure()
                return ""
            worker.join(timeout=0.2)

        # Ctrl+C 可能直接打断 start/join 之间的等待，此时 worker 仍活着
        if "err" in box:
            err = box["err"]
            if count_failure:
                self._record_summary_failure()
            # 键盘中断属于用户主动行为，计失败（触发熔断避免反复卡）后原样上抛
            if isinstance(err, KeyboardInterrupt):
                raise err
            return ""
        resp = box.get("resp")
        text = getattr(resp, "content", "") or ""
        if not text:
            if count_failure:
                self._record_summary_failure()
            return ""
        with self._summary_lock:
            self._summary_fail_streak = 0
            self._summary_cooldown_until = 0.0
        return text

    _SUMMARY_CHUNK_INSTRUCTION = (
        "请压缩总结以下对话片段，重点保留：用户意图、关键结果、文件路径、"
        "函数/类名、已尝试且失败的方案、正在进行还未完成的工作：\n\n"
    )

    def _summarize_long(self, text: str, instruction: str = None,
                        allow_rule_fallback: bool = True) -> str:
        """压缩超长文本：单次整段优先，失败才降级为分块（map-reduce）。

        默认认为模型窗口足够容纳整段折叠文本，故先整段一次压缩——分段会把
        一条连续的因果链切在两块各自独立的摘要里（map 阶段互不知全局任务），
        跨块的「改这里 / 还是不行」类指代会被各自提炼成互相矛盾的结论。
        整段压缩保住完整上下文，也省掉 map 的并行等待与 reduce 的同步调用。

        整段失败（超窗口/超时/空返回）时才降级分块：块边界优先在 '\\n['
        消息头处切开，逐块摘要后 reduce 合并一次。降级路径的失败正常计入
        熔断；整段试探本身不计（见 _summarize 的 count_failure）。全部失败
        时退化为规则式截断（_rule_based_fold），确定性产出非空结果。

        Args:
            text: 待压缩正文
            instruction: 自定义压缩指令前缀（默认通用对话摘要指令，
                工具结果压缩可传入专用指令）
            allow_rule_fallback: LLM 失败时是否允许规则截断兜底。历史折叠
                必须收敛故为 True；工具结果压缩应为 False——LLM 失败时返回
                空串，由调用方降级为硬截断（保留原文头尾，优于行级规则截断）。
        """
        chunk = max(self.config.summary_chunk_chars, 1000)
        prefix = instruction if instruction is not None else self._SUMMARY_CHUNK_INSTRUCTION

        # 整段单次压缩（默认路径）：失败不计熔断，留出降级空间
        r = self._summarize(prefix + text, count_failure=False)
        if r:
            return r

        if len(text) <= chunk:
            return self._rule_based_fold(text) if allow_rule_fallback else ""

        # 分块
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + chunk, len(text))
            if end < len(text):
                cut = text.rfind("\n[", start, end)
                if cut > start + chunk // 2:
                    end = cut + 1
            chunks.append(text[start:end])
            start = end

        partials = [p for p in self._map_summarize(chunks, prefix) if p]
        if not partials:
            return self._rule_based_fold(text) if allow_rule_fallback else ""

        merged = "\n\n".join(partials)
        if len(merged) <= chunk:
            # reduce 是一次无进度的同步 LLM 调用，耗时与 map 单块同量级；
            # 不报进度时用户看到「摘要进度 N/N」后会误以为已完成并卡死。
            self._notify(f"[compact] 正在合并 {len(partials)} 段分段摘要"
                         f"（同步调用模型，可能需要数十秒）...")
            final = self._summarize(
                "以下是同一对话按时间先后切分的分段摘要，请按时间顺序理顺因果关系，"
                "合并为一份连贯摘要，保留全部关键信息（用户意图、关键结果、文件路径、"
                "函数名、已失败的尝试、未完成工作）：\n\n" + merged)
            if final:
                return final
        # reduce 失败或合并后仍超长：分段摘要本身已是压缩态，直接使用
        return merged

    def _map_summarize(self, chunks: List[str], prefix: str) -> List[str]:
        """并行压缩各分块，返回与 chunks 顺序一致的摘要列表（失败块为空串）。

        map 阶段的各块彼此独立，串行会在同一次折叠里叠加 N 倍单次延迟（实测
        387K 折叠 7 块即 7 次串行等待）。并发度由 summary_parallel_workers 控制，
        为 1 时退回串行。_summarize 内部已吞掉异常并返回空串，故并发不引入
        map 侧的异常传播；熔断计数由 _summary_lock 串行化，行为与串行一致。

        每完成一块通过 event_bus 报进度：大上下文折叠是同步 LLM 调用（单块
        可达分钟级），无进度反馈时用户无法区分「正在压缩」与「卡死」。
        """
        workers = min(max(int(self.config.summary_parallel_workers or 1), 1), len(chunks))
        if workers <= 1:
            out = []
            for i, c in enumerate(chunks, 1):
                out.append(self._summarize(prefix + c))
                self._notify(f"[compact] 摘要进度 {i}/{len(chunks)}")
            return out
        # 预热 client：lru_cache 不做 single-flight，并发首次创建会重复构造客户端
        self.model_client
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._summarize, prefix + c): i
                       for i, c in enumerate(chunks)}
            out: List[str] = [""] * len(chunks)
            done = 0
            for fut in concurrent.futures.as_completed(futures):
                out[futures[fut]] = fut.result()
                done += 1
                self._notify(f"[compact] 摘要进度 {done}/{len(chunks)}")
            return out

    def _summary_total_cap(self) -> int:
        """摘要总量上限：与压缩阈值挂钩（阈值的 1/2，夹在 [2K, 120K]）。"""
        return max(2000, min(self.config.context_compress_threshold // 2, 120000))

    def _rule_based_fold(self, text: str) -> str:
        """规则式截断兜底：LLM 摘要不可用时保证折叠仍可进行（质量降级但不失败）。

        按行处理：'[role] ...' 消息行保留前 500 字符，其余行保留前 200 字符；
        总量超上限时尾部优先保留（最新迭代信息价值更高），且上限与压缩阈值
        挂钩，确保产出大小相对阈值有界、后续折叠可收敛。
        """
        kept = []
        for line in text.split("\n"):
            if not line:
                continue
            kept.append(line[:500] if line.startswith("[") else line[:200])
        result = "\n".join(kept)
        cap = max(min(self.config.summary_chunk_chars,
                      self.config.context_compress_threshold // 4), 1000)
        if len(result) > cap:
            result = "[...前文截断...]\n" + result[-cap:]
        return result or "[空轨迹]"

    def _summarize_messages(self, messages: List[Message]) -> str:
        """对一组消息做一次全量 LLM 压缩，返回摘要文本（阶段4 兜底通道）。

        与旁路每 N 轮的增量压缩相互独立：把整段正文交给 LLM 一次性总结，
        供无可用摘要时直接替换正文上下文使用。非 system 消息为空时返回空串。
        """
        non_system = [m for m in messages if m.role != "system"]
        if not non_system:
            return ""
        full_context = "\n\n".join(
            f"[{m.role}] {m.content}" for m in non_system
        )
        # 超长文本走分段压缩（防摘要请求自身超窗）；LLM 失败时内部有规则兜底，
        # 保证返回非空——折叠必须成功，否则上下文无法收敛
        return self._summarize_long(full_context)

    @staticmethod
    def _format_tool_calls_text(tool_calls: List[Dict]) -> str:
        """将 tool_calls 列表转为可读文本描述，用于保存对话时替代原始 tool_calls 字段。

        输出格式:
            [工具调用] read_file
            [工具调用] exec (15s)
            [工具调用] patch_file
        """
        lines = []
        for tc in tool_calls:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            name = fn.get("name", "unknown")
            lines.append(f"[工具调用] {name}")
        return "\n".join(lines)

    def _notify(self, message: str) -> None:
        """运行期进度提示（压缩等同步长耗时操作）。

        走 console.notify 而非直接 event_bus：CLI 在 run 期间没有 event_bus
        消费者，事件会积压到下一轮流开始才批量刷出，用户全程看不到进度，
        只能感受到「卡住不动」。
        """
        console = getattr(self, "_console", None)
        if console is not None and hasattr(console, "notify"):
            try:
                console.notify(message)
                return
            except Exception:
                pass
        event_bus.put(UIEvent("MESSAGE", message))

    def _set_phase(self, phase: str) -> None:
        """同步状态栏 phase（如 compressing）；console 不支持时静默跳过。

        CLIConsole 经 property 转发到 OutputEngine.model_phase，动画线程每帧
        读取渲染；ws_console 为同名实例属性。压缩是同步阻塞操作（LLM 摘要
        可达数十秒），期间不设 phase 会一直停留在 idle（显示 offline），
        用户无法区分「正在压缩」与「卡死」。
        """
        console = getattr(self, "_console", None)
        if console is None:
            return
        try:
            setattr(console, "_model_phase", phase)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 摘要消息构造（含召回补偿）
    # ------------------------------------------------------------------

    _RECALL_HINT_HEADER = SUMMARY_HEADER
    # 低于此阈值时不附带召回提示：提示本身约 200 字符，在小阈值配置下
    # 足以把刚压下去的上下文重新顶回阈值以上，压缩元信息的价值不抵成本。
    _RECALL_HINT_MIN_THRESHOLD = 10000

    def _summary_message_text(self, summary: str) -> str:
        """把摘要包装成注入序列的合成消息文本，附带召回补偿信息。

        设计理由：摘要必然会磨平细节（路径、函数名、报错原文），
        所以摘要消息里必须给出「原文在哪、怎么取回」，否则模型只能重跑一遍
        已经做过的探查。Jify 的 stream 表是 append-only 的完整存档，
        session_search 可直接检索，这是比重新读文件成本更低的召回通道。

        附带「压缩前最近读取过的文件」清单：这些路径是模型自己确认过有价
        值的，直接在提示里列出可以显著减少折叠后的重复 read_file。

        提示本身受长度预算约束（压缩阈值的 1/10）：压缩阈值很小说明上下文
        处于紧凑状态，此时再塞长提示反而会把刚压下去的上下文顶回阈值以上。
        预算不足时按优先级裁剪——检索指引优先，路径清单按最近优先逐个加。
        """
        text = self._RECALL_HINT_HEADER + "\n" + summary
        if self.config.context_compress_threshold < self._RECALL_HINT_MIN_THRESHOLD:
            return text
        budget = int(self.config.context_compress_threshold * 0.1) - len(text)
        if budget <= 0:
            return text

        hints = []
        if self._session_id:
            hint = (
                f"完整历史原文已存档，可用 session_search 按关键词检索"
                f"（session_id={self._session_id}）。摘要会丢失精确的行号、"
                f"报错原文与代码片段，需要这些细节时优先检索而非重跑工具。"
            )
            if len(hint) <= budget:
                hints.append(hint)
                budget -= len(hint)

        if self._recent_read_paths and budget > 40:
            header = "压缩前最近读取过的文件（可直接 read_file 复读，无需重新搜索）："
            picked = []
            left = budget - len(header)
            # 最近优先：deque 尾部是最新
            for p in reversed(self._recent_read_paths):
                line = f"- {p}"
                if len(line) + 1 > left:
                    break
                picked.append(line)
                left -= len(line) + 1
            if picked:
                hints.append(header + "\n" + "\n".join(picked))

        if hints:
            text += "\n\n--- 召回提示 ---\n" + "\n\n".join(hints)
        return text

    def _collect_read_paths(self, msgs: List[Message]) -> None:
        """从消息里收集 read_file 的路径，供折叠后的召回提示使用。

        只在消息即将被折叠时调用（那之后这些消息就不在序列里了），
        用 deque 控制上限，避免长任务里无限累积。
        """
        for m in msgs:
            for tc in (m.tool_calls or []):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                if fn.get("name") != "read_file":
                    continue
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    continue
                path = args.get("path") if isinstance(args, dict) else None
                if path:
                    self._recent_read_paths.append(str(path))

    def _shed_tool_outputs(self, head: List[Message], turn_start_idx: int) -> bool:
        """级 1 减重：把早期工具结果换成占位符（委托 context_shed 公共实现）。

        主 agent 只需提供「保护尾部」边界（本轮 user 下标），其余判定（阈值、
        条数触发、收益门槛、收敛门槛）与 subagent / team worker 共用同一套逻辑，
        避免三处各写一份、行为漂移。

        Args:
            head: system 消息列表（公共函数内部自行跳过，此处仅为签名兼容）
            turn_start_idx: 本轮 user 原文下标，其后内容不清理（保留工作现场）

        Returns:
            True 表示确实清空了一批内容。
        """
        try:
            changed, _saved = shed_tool_outputs(
                self.messages,
                self.config,
                notify=self._notify,
                session_id=self._session_id,
                protect_tail_from=turn_start_idx,
            )
            return changed
        except Exception:
            logger.debug("shed tool outputs failed", exc_info=True)
            return False

    def _maybe_compact_prefix(self, preserve_current: bool = False) -> None:
        """上下文超阈值时折叠前缀历史。

        统一入口，纯对话轮与工具轮均需调用，避免折叠检查点被
        `if not tool_calls: return` 提前返回路径绕过。

        Args:
            preserve_current: 发送前预检模式。True 时禁止把「本轮进行中的 user」
                随历史一并压进摘要（会丢失真实问题），只折叠本轮之前的历史。
        """
        _total_chars = sum(len(str(m)) for m in self.messages)
        if _total_chars > self.config.context_compress_threshold:
            # 级 1 先行：清空早期工具结果（零 LLM、零失败风险）。
            # 只有压到水位以下才罢手；否则继续走摘要路径（级 2/3）。
            head = [m for m in self.messages if m.role == "system"]
            try:
                self._shed_tool_outputs(head, self._turn_start_idx)
            except Exception:
                # 减重是旁路优化，任何异常都不能阻断主流程压缩
                logger.debug("shed tool outputs failed", exc_info=True)

            _total_chars = sum(len(str(m)) for m in self.messages)
            if _total_chars <= self.config.context_compress_threshold:
                return  # 仅靠清空工具结果就已收敛，无需摘要

            self._set_phase("compressing")
            try:
                self._compact_prefix(self._turn_start_idx, preserve_current=preserve_current)
            except KeyboardInterrupt:
                # 约定：压缩被中断即放弃本次压缩，上下文原样保留。
                # 只打开熔断 —— 下次触发压缩时 _summarize 直接走本地规则截断
                # （零网络调用），用户重发不会又卡在同一个不响应的请求上。
                self._trip_summary_breaker()
                raise
            finally:
                self._set_phase("idle")

    def _trip_summary_breaker(self) -> None:
        """立即打开摘要熔断：用户已放弃本次压缩，说明摘要通道不可用。

        后续折叠在冷却窗口内直接走规则截断，不再阻塞主线程等一次可能
        永不返回的 LLM 请求。
        """
        with self._summary_lock:
            self._summary_fail_streak = max(
                self._summary_fail_streak, self._SUMMARY_FAIL_LIMIT)
            self._summary_cooldown_until = time.time() + self._summary_cooldown_sec(
                self._SUMMARY_FAIL_LIMIT + 2)

    def _compact_prefix(self, turn_start_idx: int, preserve_current: bool = False) -> None:
        """超阈值折叠前缀，置于序列前端。分两条路径：

        边界 = ctx.get_compress_boundary()：session_summary **已覆盖到**的下标。

        - 路径 A（有可直接替换的旧正文）：`boundary` 之前除摘要消息外仍残留尚未折叠的
          旧轮次原文（effective > 0），直接用 session_summary 替换，【零同步 LLM 调用】。
        - 路径 B（无可直接替换内容）：`boundary` 之前只剩摘要消息本身（effective == 0），
          说明旧正文已全部摘要化，但本轮仍未压缩轮次使整体超阈值。此时把「现有摘要 + 未压缩
          轮次原文」合并压缩成一份新摘要后整体替换（**同步一次 LLM**），立即收敛，不必等攒够
          4 轮的定时周期。

        两条路径均保留 system、摘要消息与本轮进行中的尾部（turn_start_idx 之后），
        确保本轮上下文连续。

        Args:
            preserve_current: 发送前预检模式。无摘要兜底分支中，仅压缩「本轮之前的历史」并
                保留本轮 user 原文；无历史可压（如第一轮）时不动作，避免把用户真实问题摘要化。
        """
        head = [m for m in self.messages if m.role == "system"]
        summary = self.ctx.get_session_summary()

        if not summary:
            if preserve_current:
                # 发送前预检、且尚无旁路摘要：绝不能压「含本轮」的全部内容，
                # 否则用户本轮真实问题会被摘要化后发给模型。
                if turn_start_idx <= len(head):
                    # 第一轮无跨轮历史可压：按 API 轮次折叠本轮已完成的迭代
                    self._fold_api_rounds(head, turn_start_idx)
                    return
                # 只压本轮之前的历史，保留本轮 user 原文
                history = self.messages[len(head):turn_start_idx]
                self._collect_read_paths(history)  # 折叠后这些消息不再存在，先记下读过的文件
                self._notify(f"[compact] 正在压缩历史上下文（{len(history)} 条消息，"
                             "同步调用模型，可能需要数十秒）...")
                folded = self._summarize_messages(history)
                if not folded:
                    return
                self.ctx.session_summary = folded
                self.ctx._pending_compress.clear()
                head_msgs = head + [
                    Message(role="user", content=self._summary_message_text(folded)),
                ]
                self._replace_messages(head_msgs + self.messages[turn_start_idx:])
                self._turn_start_idx = len(head_msgs)
                return

            # 兜底：尚无旁路摘要（如不足一轮压缩周期就已超阈值）。
            # 先按 API 轮次折叠：保留 compact_keep_chars 预算内的工作现场，
            # 优于把一切压成一条摘要。无可压轮次（如单个巨型工具结果一步撑爆）
            # 才退化为全量压缩。
            if self._fold_api_rounds(head, turn_start_idx):
                return
            # 全量压缩「全部上下文（含本轮）」，直接把摘要替换进正文后继续处理。
            # 本轮 user 原文必须原样保留：它是任务指令锚点，摘要化会导致长任务跑偏
            self._notify(f"[compact] 正在全量压缩上下文（{len(self.messages)} 条消息，"
                         "同步调用模型，可能需要数十秒）...")
            self._collect_read_paths(self.messages)
            summary = self._summarize_compaction(self.messages)
            if not summary:
                return
            self.ctx.session_summary = summary
            self.ctx._pending_compress.clear()
            self.ctx._skip_pending_once = True  # 本轮已并入摘要，end_turn 不再重复入 pending
            keep_user = self.messages[turn_start_idx] if turn_start_idx < len(self.messages) else None
            head_msgs = head + [Message(role="user", content=self._summary_message_text(summary))]
            if keep_user is not None:
                head_msgs.append(keep_user)
            self._replace_messages(head_msgs)
            self._turn_start_idx = len(head) + 1
            return

        # 摘要消息若已存在，固定停靠在 head 之后（下标 len(head)）
        has_summary_msg = (
            len(head) < len(self.messages)
            and self.messages[len(head)].role == "user"
            and str(self.messages[len(head)].content or "").startswith(SUMMARY_HEADER)
        )
        boundary = self.ctx.get_compress_boundary(turn_start_idx)
        # 可直接替换的旧正文条数（排除摘要消息本身）
        effective = boundary - len(head) - (1 if has_summary_msg else 0)

        if effective <= 0:
            # 路径 B：无可直接替换的旧正文 → 合并「现有摘要 + 未压缩轮次」重新压缩
            fold_start = len(head) + (1 if has_summary_msg else 0)
            if fold_start >= turn_start_idx:
                # 单轮长程：轮间机制（pending/boundary）不工作，改按 API 轮次折叠
                self._fold_api_rounds(head, turn_start_idx)
                return
            fold_msgs = self.messages[fold_start:turn_start_idx]
            self._collect_read_paths(fold_msgs)
            self._notify(f"[compact] 正在压缩历史上下文（{len(fold_msgs)} 条消息，"
                         "同步调用模型，可能需要数十秒）...")
            merged = self._merge_summary(summary, fold_msgs)
            if not merged:
                return
            self.ctx.session_summary = merged
            self.ctx._pending_compress.clear()
            head_msgs = head + [
                Message(role="user", content=self._summary_message_text(merged)),
            ]
            tail = self.messages[turn_start_idx:]
            self._replace_messages(head_msgs + tail)
            self._turn_start_idx = len(head_msgs)
            return

        # 路径 A：只替换「已压缩」部分，未压缩（pending）轮次保留原文
        if boundary <= len(head):
            return  # 尚无已压缩轮次，无可替换内容（幂等）

        tail = self.messages[boundary:]
        self._collect_read_paths(self.messages[len(head):boundary])
        new_prefix_len = len(head) + 1
        shift = new_prefix_len - boundary
        self._replace_messages(head + [
            Message(role="user", content=self._summary_message_text(summary)),
        ] + tail)
        # 同步平移未压缩轮次记录的 start_idx，并前移本轮起点，避免索引错位
        self.ctx.rebase(shift)
        self._turn_start_idx = turn_start_idx + shift

    @staticmethod
    def _group_by_api_round(msgs: List[Message]) -> List[List[Message]]:
        """按 API 轮次分组。

        分组边界 = 新 assistant 消息的起点。API 契约保证每个 tool_use 在下一条
        assistant 之前必被解析，因此 [assistant(tool_calls), tool...] 作为一组
        整体进出天然合法，无需再维护任何下标索引（省掉平移与失效维护）。
        """
        groups: List[List[Message]] = []
        cur: List[Message] = []
        for m in msgs:
            if m.role == "assistant" and cur:
                groups.append(cur)
                cur = [m]
            else:
                cur.append(m)
        if cur:
            groups.append(cur)
        return groups

    def _summarize_compaction(self, fold_msgs: List[Message]) -> str:
        """单次 LLM 调用生成压缩摘要。

        输入 = 现有摘要（若有）+ 折叠范围原文。不做二级叠加压缩：旧摘要本身已是
        上一轮压缩的产物，再压一次是双重有损（这正是 _merge_summary 的问题）。
        折叠段超过单次摘要上限时直接放弃，避免先烧掉一个注定超窗的请求。
        """
        fold_text = "\n\n".join(f"[{m.role}] {m.content}" for m in fold_msgs)
        if len(fold_text) > self.config.summary_prompt_cap:
            return ""
        prompt = (
            "以下是一段正在进行的任务的上下文，请压缩成一份摘要，"
            "保留用户意图、关键结果、主要进展、未完成的工作。\n\n"
        )
        prior = self.ctx.get_session_summary()
        if prior:
            prompt += f"【已有摘要，须严格保留其全部关键信息】\n{prior}\n\n"
        prompt += f"【新增的对话原文，需并入上述摘要】\n{fold_text}"
        return self._summarize(prompt)

    def _peel_head_rounds(self, head: List[Message], turn_start_idx: int,
                          groups: List[List[Message]]) -> bool:
        """从头部丢弃最老的若干 API 轮次。

        丢弃量 = max(1, floor(总轮次 * 20%))，且至少保留 1 组。只做截断不做摘要 ——
        连摘要都拿不出来的上下文再压一遍只会更糟。截断后序列仍以本轮 user 原文
        开头，不会出现 assistant-first 的非法序列。
        """
        if len(groups) < 2:
            return False
        drop = min(max(1, int(len(groups) * 0.2)), len(groups) - 1)
        kept = [m for g in groups[drop:] for m in g]
        summary = self.ctx.get_session_summary()
        head_msgs = list(head)
        if summary:
            head_msgs.append(Message(role="user", content=self._summary_message_text(summary)))
        head_msgs.append(self.messages[turn_start_idx])
        self._replace_messages(head_msgs + kept)
        self._turn_start_idx = len(head_msgs) - 1
        logger.warning("summarizer unavailable, peeled %d/%d api rounds", drop, len(groups))
        return True

    def _fold_api_rounds(self, head: List[Message], turn_start_idx: int) -> bool:
        """单轮长程收敛通道：按 API 轮次分组折叠，尾部保留 compact_keep_chars 预算。

        折叠策略：
        - 分组现算（_group_by_api_round），不维护索引
        - 保留量按字符预算而非固定块数，一组都留不下时至少留最后一组
        - 摘要单次 LLM 调用，不做二级叠加压缩
        - 摘要拿不出来则 peel 掉最老的等价轮次，保证一定收敛

        本轮 user 原文永不被摘要化，它是任务指令锚点。

        Returns:
            True 表示已完成折叠/裁剪（messages 已替换）；False 表示无可压轮次。
        """
        groups = self._group_by_api_round(self.messages[turn_start_idx + 1:])
        if len(groups) < 2:
            return False  # 只有进行中的一组，无完整历史轮次可折叠

        # 预算夹在阈值一半以内：否则折叠一次降不下来，等于空转
        budget = max(1000, min(self.config.compact_keep_chars,
                               self.config.context_compress_threshold // 2))
        acc, keep = 0, 0
        for g in reversed(groups):
            acc += sum(len(str(m)) for m in g)
            if acc > budget:
                break
            keep += 1
        keep = max(1, keep)
        fold_count = len(groups) - keep
        if fold_count < 1:
            return False

        fold_msgs = [m for g in groups[:fold_count] for m in g]
        keep_msgs = [m for g in groups[fold_count:] for m in g]
        self._collect_read_paths(fold_msgs)
        self._notify(f"[compact] 折叠 {fold_count}/{len(groups)} 个 API 轮次"
                     f"（{len(fold_msgs)} 条消息，同步调用模型，可能需要数十秒）...")

        summary = self._summarize_compaction(fold_msgs)
        if not summary:
            return self._peel_head_rounds(head, turn_start_idx, groups)

        self.ctx.session_summary = summary
        keep_user = self.messages[turn_start_idx]  # 本轮 user 原文
        head_msgs = head + [
            Message(role="user", content=self._summary_message_text(summary)),
            keep_user,
        ]
        self._replace_messages(head_msgs + keep_msgs)
        self._turn_start_idx = len(head) + 1

        # 已折叠内容从 transcript 占位清理：已被摘要覆盖，防止 end_turn 重复
        # 压缩与长任务内存膨胀（按 tool_call_id 精确匹配）
        folded_ids = {m.tool_call_id for m in fold_msgs if m.role == "tool"}
        transcript = self._active_turn_transcript
        if transcript and folded_ids:
            for i, entry in enumerate(transcript):
                if entry.get("tool_call_id") in folded_ids:
                    transcript[i] = {
                        "role": "tool",
                        "name": entry.get("name", "unknown"),
                        "tool_call_id": entry.get("tool_call_id"),
                        "result": "[已折叠进摘要] " + str(entry.get("result", ""))[:200],
                    }
        return True

    def _merge_summary(self, existing_summary: str, fold_msgs: List[Message]) -> str:
        """把「现有摘要」与「待并入的对话原文」合并压缩成一份新摘要（路径 B/C 共用）。

        严格保留已有摘要信息，仅追加新轮次的用户意图、关键结果、主要进展与未完成工作。
        待并入原文超长时先分段压缩，防止合并请求自身超窗；合并 LLM 失败时
        退化为「现有摘要 + 规则截断追加」，保证返回非空（折叠必须收敛）。
        """
        fold_text = "\n\n".join(
            f"[{m.role}] {m.content}" for m in fold_msgs
        )
        chunk = max(self.config.summary_chunk_chars, 1000)
        if len(fold_text) > chunk:
            fold_text = self._summarize_long(fold_text)
        prompt = (
            "以下是一份已有的会话摘要，请严格保留其全部关键信息：\n\n"
            f"{existing_summary}\n\n"
            "以下是尚未并入摘要的对话原文，请将其中的用户意图、关键结果、主要进展、"
            "正在进行尚未完成的工作追加整合，输出一份合并后的完整新摘要：\n\n"
            f"{fold_text}"
        )
        try:
            result = self._summarize(prompt)
            if result:
                return result
        except Exception:
            pass
        # 合并失败兜底：保留现有摘要 + 追加规则截断文本；总量超限时
        # 尾部优先（最新折叠信息价值更高），保证 LLM 长期不可用时摘要仍有界
        merged = (existing_summary + "\n\n[追加折叠-规则截断]\n"
                  + self._rule_based_fold(fold_text))
        cap = self._summary_total_cap()
        if len(merged) > cap:
            merged = "[...更早摘要截断...]\n" + merged[-cap:]
        return merged

    def _emergency_compress(self, user_message: str) -> None:
        """上下文溢出后的反应式压缩。

        先走常规折叠入口；仍未收敛则按 API 轮次从头部 peel 掉最老的轮次
        （只截断不摘要）—— 溢出说明真实窗口已经不够，这时再发一次可能同样
        超窗的摘要请求没有意义。
        """
        self._set_phase("compressing")
        try:
            self._maybe_compact_prefix()
            if sum(len(str(m)) for m in self.messages) < self.config.context_compress_threshold:
                return
            head = [m for m in self.messages if m.role == "system"]
            groups = self._group_by_api_round(self.messages[self._turn_start_idx + 1:])
            self._peel_head_rounds(head, self._turn_start_idx, groups)
        finally:
            self._set_phase("idle")

    @staticmethod
    def _looks_like_context_overflow(error: Exception) -> bool:
        """判断 API 错误是否由上下文溢出导致。"""
        msg = str(error).lower()
        overflow_keywords = [
            "context length", "context too long", "maximum context",
            "token limit", "too many tokens", "context window",
            "reduce", "truncate", "max_tokens",
        ]
        return any(kw in msg for kw in overflow_keywords)

    def interrupt(self) -> None:
        """请求中断当前循环，优雅打断"""
        self._interrupt_requested = True
        self._interrupt_event.set()  # 触发中断事件
        # 关闭底层 HTTP 流，使阻塞等待首个 chunk 的迭代立即退出
        if self._active_stream is not None:
            try:
                self._active_stream.close()
            except Exception:
                pass
        # 传播中断到 Team 模式：中断正在执行的 Worker 任务（不关闭团队结构）
        try:
            from team import get_leader
            leader = get_leader()
            if leader is not None:
                leader.interrupt()
        except Exception:
            pass


