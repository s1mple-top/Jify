# -*- coding: utf-8 -*-
import asyncio
import json
import concurrent.futures
import logging
import os
import time
import queue
import threading
import re
from operator import length_hint
from typing import List, Dict, Any, Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import yaml
import uuid

logger = logging.getLogger(__name__)

from context_manager import ContextManager

from model_client import get_model_client
from jify_tool import registry, ToolCall, should_parallel, _MAX_WORKERS
from tools.registry import set_agent_config, reset_agent_config
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


@dataclass
class ModelConfig:
    """单个模型配置 —— 包含 provider / model / base_url / api_key 的完整配置"""
    name: str            # 显示名称，如 "gpt-4o", "claude"
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
    max_iterations: int = 300
    tool_delay: float = 0.0
    max_workers: int = 8
    tool_timeout: float = 120.0  # 单次工具调用超时（秒）
    approval_tool_timeout: float = 180.0  # 审批类工具外层超时（秒），须大于审批等待 120s
    extra_body: dict = field(default_factory=dict)  # 附加到 API 请求体的参数，按 provider 自行配置
    plugins_dir: str = "~/.jify/plugins" # 目录
    SelfEvolutionModel: str= ""
    SelfEvolutionTurn: int = 8  # 每 N 轮做一次画像提取
    enabled_plugins: Optional[List[str]] = None  # None = 全部加载
    context_compress_threshold: int = 700000  # 总字符数超过此阈值时触发上下文压缩
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
        # 按 provider 映射为对应格式（anthropic → reasoning/output_config，openai → thinking/reasoning_effort）。
        effort = (config.extra_body or {}).get("reasoning_effort")
        if effort:
            if config.provider in ("anthropic", "claude"):
                extra["thinking"] = {"type": "disabled" if effort == "none" else "enabled"}
                if effort != "none":
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
    """
    if td and td.timeout is not None:
        return td.timeout
    timeout = config.tool_timeout
    if td and td.requires_approval:
        timeout = max(timeout, config.approval_tool_timeout)
    return timeout


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
        self._context_emergency_compressed: bool = False  # 兜底压缩只做一次，防无限重试
        self._turn_start_idx: int = 0  # 本轮新增消息的起始索引（折叠前缀时保留尾部）

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
        with self._session_messages_lock:
            for m in self._session_messages:
                if m.reasoning_content:
                    m.reasoning_content = None

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

    def run(self, message_id: str, user_message: str, system_prompt: str, console, tool_schemas,
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
        self._interrupt_requested = False
        self._interrupt_event.clear()
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

        # append-only：历史不再重格式化，用户消息作为真实 message 追加到序列末尾
        self.messages.append(Message(role="user", content=user_message))

        # 本轮起始索引 = 本轮 user 消息所在下标（折叠时保留「本轮 user + assistant + tool」）
        self._turn_start_idx = len(self.messages) - 1

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
                logger.exception("API call failed at iteration %d", self.iteration_count)
                # 紧急压缩：上下文溢出 + 未尝试过 → 压缩一次后重试
                # 不再要求「无旁路摘要」：摘要存在≠足够小，若已有摘要但上下文
                # 仍溢出（如当前轮自身超限、摘要自身膨胀），同样需要兜底一次。
                # _context_emergency_compressed 已保证只兜底一次，不会无限重试。
                if (not self._context_emergency_compressed
                        and self._looks_like_context_overflow(e)):
                    self._context_emergency_compressed = True
                    event_bus.put(UIEvent("MESSAGE", "[上下文溢出] 正在调用模型压缩上下文..."))
                    self._emergency_compress(user_message)
                    continue
                self._last_api_error = str(e)
                tool_calls = []
                text_content = f"[API 调用失败] {e}\n请检查网络连接或 API 配置后重试。"
                event_bus.put(UIEvent("MESSAGE", text_content))
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

            # 无 tool_calls → 结束
            if not tool_calls:
                # 纯对话轮：同样把 assistant 回复落回 messages，供下轮携带。
                # 否则历史里会堆积连续的 user 消息，模型会把上一轮问题再答一遍。
                self.messages.append(Message(
                    role="assistant",
                    content=text_content or "",
                    reasoning_content=console.last_reasoning_content,
                ))

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
                # 放在 end_turn 之前：折叠内部 flush_pending_sync 只并入历史轮次，
                # 避免本轮刚入 pending 即被折进摘要、又在尾部重复出现。
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
                with self._session_messages_lock:
                    self._session_messages.extend(
                        [m for m in self.messages[self._turn_start_idx:] if m.role != "system"]
                    )
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
                event_bus.put(UIEvent("MESSAGE", "[审批] 用户终止当前任务。"))
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
                tool_calls=tool_calls,
            ))

            # 统一约定：事件里传原始字符数，显示层统一做一次 //2 换算 token
            event_bus.put(UIEvent("Token_Send", len(text_content or "")))

            # 清空 reasoning_content，下轮 consume_stream 会重新填充
            console.last_reasoning_content = ""

            # 执行工具（传入预执行结果，已完成的直接跳过） 兜底策略，防止fire漏掉
            results = self._execute_tools(tool_calls, pre_results)

            # 注入工具结果到 messages
            for tc_result in results:
                content = tc_result.result or ""
                self.messages.append(Message(
                    role="tool",
                    content=tc_result.result or "",
                    tool_call_id=tc_result.id,
                ))

                # 工具执行结果也会随 messages 发送给模型，估算 token 数计入 sent，其余 toolcallid 上方塞入
                if content:
                    event_bus.put(UIEvent("Token_Send", len(content)))

                # 单独领出来可以考虑放弃掉某些tool的结果，减少token的效果，不过效果会下降
                _turn_transcript.append({
                    "role": "tool",
                    "name": tc_result.name,
                    "tool_call_id": tc_result.id,
                    "result": tc_result.result or "",
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
        event_bus.put(UIEvent("MESSAGE", final_msg))
        # 持久化本轮完整消息（只追加本轮新增，避免重复）
        with self._session_messages_lock:
            self._session_messages.extend(
                [m for m in self.messages[self._turn_start_idx:] if m.role != "system"]
            )
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
        enhanced = []
        pre_done = []

        for tc in tool_calls:
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
                tcall.error = False
                try:
                    parsed = json.loads(tcall.result)
                    if isinstance(parsed, dict) and "error" in parsed:
                        tcall.error = True
                except Exception:
                    pass
                pre_done.append(tcall)
            else:
                enhanced.append(tcall)

        # 全部预执行完成 → 直接返回
        if not enhanced:
            return pre_done

        # 剩余工具按策略执行
        if should_parallel(enhanced):
            results = self._execute_parallel(enhanced)
        else:
            results = self._execute_sequential(enhanced)

        return pre_done + results

    def _execute_parallel(self, tool_calls: List[ToolCall]) -> List[ToolCall]:
        """并行执行：ThreadPoolExecutor，支持 interrupt + 审批 break + 超时"""
        from tools.approval import break_requested

        results = [None] * len(tool_calls)
        interrupted = False

        def _worker(index: int, tc: ToolCall):
            if self._interrupt_requested or break_requested.is_set():
                tc.result = "[Skipped — user interrupt]"
                tc.error = True
                tc.duration = 0
                results[index] = tc
                return

            start = time.time()
            try:
                result = self._dispatch_with_config(tc.name, tc.args)
            except Exception as e:
                result = json.dumps({"error": str(e)})
            tc.result = result
            tc.duration = time.time() - start
            tc.error = False
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict) and "error" in parsed:
                    tc.error = True
            except Exception:
                pass

            results[index] = tc

        workers = min(len(tool_calls), self.config.max_workers, _MAX_WORKERS)

        timeout = max(
            _resolve_tool_timeout(registry.get(tc.name), self.config)
            for tc in tool_calls
        )
        deadline = time.time() + timeout

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(_worker, i, tc)
                for i, tc in enumerate(tool_calls)
            ]
            while True:
                done, _ = concurrent.futures.wait(futures, timeout=0.2)
                if len(done) == len(futures):
                    break
                if self._interrupt_requested or break_requested.is_set():
                    interrupted = True
                    for f in futures:
                        f.cancel()
                    break
                if time.time() >= deadline:
                    interrupted = True
                    break

        timeout_text = f"[超时 — 超过 {timeout:.0f}s]"
        for i, tc in enumerate(tool_calls):
            if results[i] is None:
                tc.result = timeout_text
                tc.error = True
                tc.duration = timeout

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
            timeout = _resolve_tool_timeout(td, self.config)

            start = time.time()
            try:
                # 方便超时断链
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(self._dispatch_with_config, tc.name, tc.args)
                    result = future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                result = json.dumps({"error": f"Tool timeout after {timeout:.0f}s"})
                tc.error = True
            except Exception as e:
                result = json.dumps({"error": str(e)})
                tc.error = True
            else:
                tc.error = False
            tc.result = result
            tc.duration = time.time() - start
            if not tc.error:
                try:
                    parsed = json.loads(result)
                    if isinstance(parsed, dict) and "error" in parsed:
                        tc.error = True
                except Exception:
                    pass

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
            if reasoning_content:
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

    def _summarize(self, prompt: str) -> str:
        """调用 LLM 做摘要压缩"""
        try:
            msgs = [{"role": "user", "content": prompt}]
            resp = self.model_client.chat(
                messages=msgs,
                tool_schemas=[],
                model=self.config.model,
                stream=False,
            )
            return resp.content or ""
        except Exception:
            return ""

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
        prompt = (
            "请对以下对话进行压缩总结，总结压缩成为模型可参考的历史对话信息，"
            "要着重保留用户意图、当前进度、关键结果、已经解决的问题，"
            "以及正在进行还未完成的工作，历史对话为：\n\n"
            f"{full_context}"
        )
        try:
            return self._summarize(prompt) or ""
        except Exception:
            return ""

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
            self._compact_prefix(self._turn_start_idx, preserve_current=preserve_current)

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
                    # 第一轮无历史可压：本轮 user 自身撑大上下文，无法安全折叠。
                    # 不动作，交由模型窗口/紧急压缩兜底（阈值 << 窗口时应不触发）。
                    return
                # 只压本轮之前的历史，保留本轮 user 原文
                history = self.messages[len(head):turn_start_idx]
                folded = self._summarize_messages(history)
                if not folded:
                    return
                self.ctx.session_summary = folded
                self.ctx._pending_compress.clear()
                head_msgs = head + [
                    Message(role="user", content="=== 历史摘要 ===\n" + folded),
                ]
                self.messages = head_msgs + self.messages[turn_start_idx:]
                self._turn_start_idx = len(head_msgs)
                return

            # 兜底：尚无旁路摘要（如不足一轮压缩周期就已超阈值）。
            # 全量压缩「全部上下文（含本轮）」，直接把摘要替换进正文后继续处理。
            summary = self._summarize_messages(self.messages)
            if not summary:
                return
            self.ctx.session_summary = summary
            self.ctx._pending_compress.clear()
            self.ctx._skip_pending_once = True  # 本轮已并入摘要，end_turn 不再重复入 pending
            self.messages = head + [
                Message(role="user", content="=== 历史摘要 ===\n" + summary),
            ]
            self._turn_start_idx = len(head) + 1
            return

        # 摘要消息若已存在，固定停靠在 head 之后（下标 len(head)）
        has_summary_msg = (
            len(head) < len(self.messages)
            and self.messages[len(head)].role == "user"
            and str(self.messages[len(head)].content or "").startswith("=== 历史摘要 ===")
        )
        boundary = self.ctx.get_compress_boundary(turn_start_idx)
        # 可直接替换的旧正文条数（排除摘要消息本身）
        effective = boundary - len(head) - (1 if has_summary_msg else 0)

        if effective <= 0:
            # 路径 B：无可直接替换的旧正文 → 合并「现有摘要 + 未压缩轮次」重新压缩
            fold_start = len(head) + (1 if has_summary_msg else 0)
            if fold_start >= turn_start_idx:
                return  # 无新内容可折叠，避免重复 LLM 调用（留给 emergency 兜底）
            fold_msgs = self.messages[fold_start:turn_start_idx]
            merged = self._merge_summary(summary, fold_msgs)
            if not merged:
                return
            self.ctx.session_summary = merged
            self.ctx._pending_compress.clear()
            head_msgs = head + [
                Message(role="user", content="=== 历史摘要 ===\n" + merged),
            ]
            tail = self.messages[turn_start_idx:]
            self.messages = head_msgs + tail
            self._turn_start_idx = len(head_msgs)
            return

        # 路径 A：只替换「已压缩」部分，未压缩（pending）轮次保留原文
        if boundary <= len(head):
            return  # 尚无已压缩轮次，无可替换内容（幂等）

        tail = self.messages[boundary:]
        new_prefix_len = len(head) + 1
        shift = new_prefix_len - boundary
        self.messages = head + [
            Message(role="user", content="=== 历史摘要 ===\n" + summary),
        ] + tail
        # 同步平移未压缩轮次记录的 start_idx，并前移本轮起点，避免索引错位
        self.ctx.rebase(shift)
        self._turn_start_idx = turn_start_idx + shift

    def _merge_summary(self, existing_summary: str, fold_msgs: List[Message]) -> str:
        """把「现有摘要」与「待并入的对话原文」合并压缩成一份新摘要（路径 B 专用）。

        严格保留已有摘要信息，仅追加新轮次的用户意图、关键结果、主要进展与未完成工作。
        失败或为空时返回空串，由调用方保持原状。
        """
        fold_text = "\n\n".join(
            f"[{m.role}] {m.content}" for m in fold_msgs
        )
        prompt = (
            "以下是一份已有的会话摘要，请严格保留其全部关键信息：\n\n"
            f"{existing_summary}\n\n"
            "以下是尚未并入摘要的对话原文，请将其中的用户意图、关键结果、主要进展、"
            "正在进行尚未完成的工作追加整合，输出一份合并后的完整新摘要：\n\n"
            f"{fold_text}"
        )
        try:
            return self._summarize(prompt) or ""
        except Exception:
            return ""

    def _emergency_compress(self, user_message: str) -> None:
        """紧急压缩：API 上下文溢出时，用 LLM 做一次全量压缩兜底。

        压缩覆盖本轮起始点之前的全部历史，仅保留本轮新增尾部，避免丢掉当前进度。
        """
        turn_start_idx = self._turn_start_idx

        result = self._summarize_messages(self.messages[:turn_start_idx])

        if result:
            self.ctx.session_summary = result
            # 全量压缩已覆盖历史，清空待压缩缓冲避免重复折叠
            self.ctx._pending_compress.clear()
            self._compact_prefix(turn_start_idx)

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


