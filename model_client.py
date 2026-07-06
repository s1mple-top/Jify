# -*- coding: utf-8 -*-
"""
Model Client — OpenAI / Anthropic API 客户端，统一结果封装层

单例模式
通过 @lru_cache 或模块级缓存实现，线程安全。
"""

import os
import json
import random
import time
import logging

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, List, Optional, Iterator

logger = logging.getLogger(__name__)


# 统一响应类型（跨 provider）
@dataclass
class ToolCallDelta:
    """流式工具调用增量"""
    index: int = 0
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class StreamChunk:
    """统一的流式 chunk，适配 OpenAI / Anthropic 原生协议"""
    content: str = ""
    thinking: str = ""
    tool_call_deltas: List[ToolCallDelta] = field(default_factory=list)
    finish_reason: str = ""


class UnifiedResponse:
    """
    统一的非流式响应。

    tool_calls 格式：[{"id": "...", "type": "function", "function": {"name": "...", "arguments": "..."}}]
    """
    def __init__(self, content="", thinking="", tool_calls=None, model=""):
        self.content = content
        self.thinking = thinking
        self.tool_calls = tool_calls or []
        self.model = model


# 流式适配器
class OpenAIStreamAdapter:
    """把 OpenAI stream chunks 转为统一的 StreamChunk 迭代器"""

    def __init__(self, openai_stream: Iterator):
        self._stream = openai_stream

    def __iter__(self):
        return self._next()

    def close(self) -> None:
        """关闭底层 HTTP 流，使阻塞的迭代立即退出（用于 ESC 中断）"""
        try:
            self._stream.close()
        except Exception:
            pass

    def _next(self):
        for chunk in self._stream:
            sc = StreamChunk()

            if not hasattr(chunk, "choices") or not chunk.choices:
                yield sc
                continue

            delta = chunk.choices[0].delta

            if hasattr(delta, "content") and delta.content:
                sc.content = delta.content

            rc = (getattr(delta, "reasoning_content", None) or
                  getattr(delta, "reasoning", None))
            if not rc and hasattr(delta, "model_extra") and delta.model_extra:
                rc = delta.model_extra.get("reasoning_content") or delta.model_extra.get("reasoning")
            if rc:
                sc.thinking = rc

            if hasattr(delta, "tool_calls") and delta.tool_calls:
                for tc in delta.tool_calls:
                    tcd = ToolCallDelta(
                        index=tc.index if hasattr(tc, "index") else 0,
                        id=tc.id if hasattr(tc, "id") and tc.id else "",
                    )
                    if hasattr(tc, "function") and tc.function:
                        if hasattr(tc.function, "name") and tc.function.name:
                            tcd.name = tc.function.name
                        if hasattr(tc.function, "arguments") and tc.function.arguments:
                            tcd.arguments = tc.function.arguments
                    sc.tool_call_deltas.append(tcd)

            if hasattr(chunk.choices[0], "finish_reason") and chunk.choices[0].finish_reason:
                sc.finish_reason = chunk.choices[0].finish_reason

            yield sc


class AnthropicStreamAdapter:
    """把 Anthropic SSE 事件流转为统一的 StreamChunk 迭代器"""

    def __init__(self, anthropic_stream: Iterator):
        self._stream = anthropic_stream

    def __iter__(self):
        return self._next()

    def close(self) -> None:
        """关闭底层 HTTP 流，使阻塞的迭代立即退出（用于 ESC 中断）"""
        try:
            self._stream.close()
        except Exception:
            pass

    def _next(self):
        for event in self._stream:
            sc = StreamChunk()
            event_type = getattr(event, "type", "")

            if event_type == "content_block_start":
                cb = getattr(event, "content_block", None)
                if cb and getattr(cb, "type", "") == "text":
                    sc.content = getattr(cb, "text", "") or ""
                elif cb and getattr(cb, "type", "") == "tool_use":
                    sc.tool_call_deltas.append(ToolCallDelta(
                        index=getattr(event, "index", 0),
                        id=getattr(cb, "id", "") or "",
                        name=getattr(cb, "name", "") or "",
                    ))
                elif cb and getattr(cb, "type", "") == "thinking":
                    sc.thinking = getattr(cb, "thinking", "") or ""

            elif event_type == "content_block_delta":
                delta = getattr(event, "delta", None)
                if delta:
                    dt = getattr(delta, "type", "")
                    if dt == "text_delta":
                        sc.content = getattr(delta, "text", "") or ""
                    elif dt == "input_json_delta":
                        sc.tool_call_deltas.append(ToolCallDelta(
                            index=getattr(event, "index", 0),
                            arguments=getattr(delta, "partial_json", "") or "",
                        ))
                    elif dt == "thinking_delta":
                        sc.thinking = getattr(delta, "thinking", "") or ""
                    elif dt == "signature_delta":
                        pass

            elif event_type == "message_delta":
                delta = getattr(event, "delta", None)
                if delta:
                    stop = getattr(delta, "stop_reason", "") or ""
                    stop_map = {
                        "end_turn": "stop",
                        "tool_use": "tool_calls",
                        "max_tokens": "length",
                        "stop_sequence": "stop",
                    }
                    sc.finish_reason = stop_map.get(stop, "stop")

            yield sc



def _make_cache_key(provider: str, api_key: str, base_url: str) -> str:
    """生成缓存 key"""
    return f"{provider}::{api_key}::{base_url}"


_RETRYABLE_PATTERNS = (
    "RateLimitError",
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
    "ServiceUnavailableError",
    "ConnectionError",
    "Timeout",
    "TimeoutError",
    "RemoteDisconnected",
    "IncompleteRead",
    "ChunkedEncodingError",
    "ServerError",
    "ProxyError",
    "RetryError",
)


def _is_transient(exc: Exception) -> bool:
    """判断异常是否为瞬时错误（值得重试）。"""
    exc_name = type(exc).__name__
    for name in _RETRYABLE_PATTERNS:
        if name in exc_name or name in str(type(exc)):
            return True
    # 递归检查异常链（from/__cause__）
    if exc.__cause__ is not None:
        return _is_transient(exc.__cause__)
    if exc.__context__ is not None and exc.__context__ is not exc:
        return _is_transient(exc.__context__)
    return False


def _retry(func, max_attempts: int = 3, backoff_base: float = 3.0,
           timeout_seq: Optional[List[float]] = None):
    """对可调用对象 func 进行重试，指数退避。

    仅对瞬时错误重试（网络、超时、限流、服务端 5xx）。
    永久性错误（认证、参数错误）直接抛出。

    Args:
        func: 可调用对象，当 timeout_seq 不为 None 时需接受 timeout 关键字参数
        max_attempts: 最大尝试次数（含首次）
        backoff_base: 退避底数，等待时间 = backoff_base ** (attempt-1) 秒。
                      当传入 timeout_seq 时此参数无效，改为固定小抖动。
        timeout_seq: 每次尝试的 HTTP 超时（秒），长度应等于 max_attempts。
                     例如 [10,10,10,10,10,15,15,15,15,15] 表示前 5 次 10s 超时、
                     后 5 次 15s 超时。

    Raises:
        最后一次尝试的异常
    """
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            if timeout_seq:
                return func(timeout=timeout_seq[attempt - 1])
            return func()
        except Exception as exc:
            last_exc = exc
            if not _is_transient(exc):
                raise
            if attempt == max_attempts:
                raise
            if timeout_seq:
                wait = 0.5 + random.uniform(0, 0.5)  # 固定小抖动，超时本身已足够
            else:
                wait = backoff_base ** (attempt - 1) + random.uniform(0, 1)
            logger.debug(
                "Chat attempt %d/%d failed (transient): %s. Retrying in %.1fs...",
                attempt, max_attempts, exc, wait,
            )
            time.sleep(wait)
    raise last_exc  # type: ignore[misc]



# OpenAI 客户端
@lru_cache(maxsize=8)
def get_openai_client(api_key: Optional[str] = None,
                      base_url: Optional[str] = None,
                      timeout: float = 10.0) -> "OpenAIClient":
    """
    获取 OpenAI 客户端实例（自动缓存，同一参数不重复创建）。

    环境变量：
        OPENAI_API_KEY
        OPENAI_BASE_URL
    """
    return OpenAIClient(api_key=api_key, base_url=base_url, timeout=timeout)


class OpenAIClient:
    """
    OpenAI 兼容客户端（OpenAI / Groq / Together / LMDeploy / vLLM / Ollama 等）。

    兼容旧版（< 1.0）和新版（>= 1.0）openai SDK：
    - 旧版：openai.ChatCompletion.create()
    - 新版：OpenAI().chat.completions.create()

    所有参数均可通过环境变量覆盖：
        OPENAI_API_KEY
        OPENAI_BASE_URL
    """

    def __init__(self, api_key: Optional[str] = None,
                 base_url: Optional[str] = None,
                 timeout: float = 10.0):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY") or "dummy"
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL") or ""
        self.timeout = timeout
        self._legacy = False
        self._client_v1 = None

        # 尝试新版 SDK (>= 1.0)
        try:
            from openai import OpenAI as _OpenAI
            extra_kwargs = {"timeout": timeout}
            if self.base_url:
                extra_kwargs["base_url"] = self.base_url
            self._client_v1 = _OpenAI(api_key=self.api_key, **extra_kwargs)
        except ImportError:
            # 回退到旧版 SDK (< 1.0)
            import openai
            openai.api_key = self.api_key
            if self.base_url:
                openai.api_base = self.base_url
            self._legacy = True

    def chat(self, messages: List[Dict], tool_schemas: List[Dict],
             model: str, **kwargs) -> Any:
        """
        调用 OpenAI Chat Completions API。

        - 非流式：返回 UnifiedResponse
        - 流式 (stream=True)：返回 OpenAIStreamAdapter，迭代产出 StreamChunk
        """
        extra = {}
        for k in ("temperature", "max_tokens", "top_p", "stream", "extra_body"):
            if k in kwargs:
                extra[k] = kwargs[k]

        _timeout_seq = [10.0] * 5 + [15.0] * 5

        if self._legacy:
            import openai
            params = {"model": model, "messages": messages, **extra}
            if tool_schemas is not None and len(tool_schemas) > 0:
                params["functions"] = [f["function"] for f in tool_schemas]
                params["function_call"] = "auto"
            raw = _retry(
                lambda timeout: openai.ChatCompletion.create(**params, request_timeout=timeout),
                max_attempts=10,
                timeout_seq=_timeout_seq,
            )
            if extra.get("stream"):
                return OpenAIStreamAdapter(raw)
            return _openai_raw_to_unified(raw)
        else:
            chat_kwargs = {"model": model, "messages": messages, **extra}
            if tool_schemas is not None and len(tool_schemas) > 0:
                chat_kwargs["tools"] = tool_schemas
                chat_kwargs["tool_choice"] = "auto"
            raw = _retry(
                lambda timeout: self._client_v1.chat.completions.create(**chat_kwargs, timeout=timeout),
                max_attempts=10,
                timeout_seq=_timeout_seq,
            )
            if extra.get("stream"):
                return OpenAIStreamAdapter(raw)
            return _openai_raw_to_unified(raw)



# Anthropic 客户端
@lru_cache(maxsize=8)
def get_anthropic_client(api_key: Optional[str] = None,
                         base_url: Optional[str] = None,
                         timeout: float = 10.0) -> "AnthropicClient":
    """
    获取 Anthropic 客户端实例。

    环境变量：
        ANTHROPIC_API_KEY
        ANTHROPIC_BASE_URL
    """
    return AnthropicClient(api_key=api_key, base_url=base_url, timeout=timeout)


class AnthropicClient:
    """
    Anthropic Claude API 客户端。

    所有参数均可通过环境变量覆盖，优先从配置拿：
        ANTHROPIC_API_KEY
    """

    def __init__(self, api_key: Optional[str] = None,
                 base_url: Optional[str] = None,
                 timeout: float = 10.0):
        try:
            from anthropic import Anthropic
        except ImportError:
            raise ImportError("anthropic package not installed. Run: pip install anthropic")

        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY not set")
        self.base_url = base_url or os.getenv("ANTHROPIC_BASE_URL")
        self.timeout = timeout
        extra_kwargs = {"timeout": timeout}
        if self.base_url:
            extra_kwargs["base_url"] = self.base_url
        self._client = Anthropic(api_key=self.api_key, **extra_kwargs)

    def chat(self, messages: List[Dict], tool_schemas: List[Dict],
             model: str, **kwargs) -> Any:
        """
        调用 Anthropic Messages API。

        格式差异：
        - Anthropic 的 system 消息不在 messages 数组中，需单独提取
        - 工具结果通过 content 中的 tool_result block 表达，不是 role=tool

        - 非流式：返回 UnifiedResponse
        - 流式 (stream=True)：返回 AnthropicStreamAdapter，迭代产出 StreamChunk
        """
        system_content = ""
        filtered_messages = []
        for msg in messages:
            if msg.get("role") == "system":
                system_content = msg.get("content", "")
            else:
                filtered_messages.append(msg)

        anthropic_messages = []
        _pending_tool_results = []
        for msg in filtered_messages:
            role = msg.get("role")
            if role == "tool":
                _pending_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": msg.get("content", ""),
                })
            else:
                if _pending_tool_results:
                    anthropic_messages.append({
                        "role": "user",
                        "content": _pending_tool_results,
                    })
                    _pending_tool_results = []
                if role == "assistant" and msg.get("tool_calls"):
                    anthropic_messages.append(
                        _assistant_to_anthropic(msg)
                    )
                else:
                    anthropic_messages.append(msg)
        if _pending_tool_results:
            anthropic_messages.append({
                "role": "user",
                "content": _pending_tool_results,
            })

        create_kwargs = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": kwargs.get("max_tokens", 20480),
        }
        if system_content:
            create_kwargs["system"] = system_content
        if tool_schemas:
            create_kwargs["tools"] = _openai_tools_to_anthropic(tool_schemas)
        for k in ("temperature", "top_p"):
            if k in kwargs:
                create_kwargs[k] = kwargs[k]

        is_stream = kwargs.get("stream", False)
        if is_stream:
            create_kwargs["stream"] = True

        _timeout_seq = [10.0] * 5 + [15.0] * 5
        raw = _retry(
            lambda timeout: self._client.messages.create(**create_kwargs, timeout=timeout),
            max_attempts=10,
            timeout_seq=_timeout_seq,
        )

        if is_stream:
            return AnthropicStreamAdapter(raw)
        return _anthropic_raw_to_unified(raw)


def _openai_tools_to_anthropic(tool_schemas: List[Dict]) -> List[Dict]:
    """OpenAI 工具格式 → Anthropic 工具格式"""
    result = []
    for ts in tool_schemas:
        fn = ts.get("function", ts)
        anthro = {
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", fn.get("input_schema", {})),
        }
        result.append(anthro)
    return result


def _assistant_to_anthropic(msg: Dict) -> Dict:
    """把 OpenAI 格式的 assistant 消息转为 Anthropic content block 格式"""
    content = msg.get("content", "") or ""
    tool_calls = msg.get("tool_calls") or []
    blocks = []
    if content:
        blocks.append({"type": "text", "text": content})
    for tc in tool_calls:
        fn = tc.get("function", {})
        args_str = fn.get("arguments", "{}")
        try:
            args = json.loads(args_str) if isinstance(args_str, str) else args_str
        except (json.JSONDecodeError, TypeError):
            args = {}
        blocks.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": fn.get("name", ""),
            "input": args,
        })
    return {"role": "assistant", "content": blocks}



# 原始响应 → UnifiedResponse 转换
def _openai_raw_to_unified(raw) -> UnifiedResponse:
    content = ""
    tool_calls = []
    thinking = ""
    if hasattr(raw, "choices") and raw.choices:
        choice = raw.choices[0]
        content = choice.message.content or ""
        thinking = (getattr(choice.message, "reasoning_content", None) or
                    getattr(choice.message, "reasoning", None) or "")
        if not thinking:
            msg_extra = getattr(choice.message, "model_extra", None) or {}
            thinking = msg_extra.get("reasoning_content") or msg_extra.get("reasoning") or ""
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                })
    return UnifiedResponse(
        content=content,
        thinking=thinking,
        tool_calls=tool_calls,
        model=getattr(raw, "model", ""),
    )


def _anthropic_raw_to_unified(raw) -> UnifiedResponse:
    content_parts = []
    tool_calls = []
    thinking = ""
    for block in getattr(raw, "content", []) or []:
        t = getattr(block, "type", "")
        if t == "text":
            content_parts.append(getattr(block, "text", "") or "")
        elif t == "tool_use":
            args = getattr(block, "input", {}) or {}
            tool_calls.append({
                "id": getattr(block, "id", "") or "",
                "type": "function",
                "function": {
                    "name": getattr(block, "name", "") or "",
                    "arguments": json.dumps(args) if not isinstance(args, str) else args,
                },
            })
        elif t == "thinking":
            thinking = getattr(block, "thinking", "") or ""
    return UnifiedResponse(
        content="\n".join(content_parts),
        thinking=thinking,
        tool_calls=tool_calls,
        model=getattr(raw, "model", ""),
    )



# 工厂函数（统一入口，线程安全）
@lru_cache(maxsize=4)
def get_model_client(provider: str,
                     api_key: Optional[str] = None,
                     base_url: Optional[str] = None,
                     timeout: float = 10.0) -> Any:
    """
    统一工厂函数，根据 provider 返回对应客户端实例。

    Args:
        provider: "openai" | "anthropic" | "groq" | "together" | "vllm" 等
                  OpenAI 兼容的都传 "openai"
        api_key: API Key，可通过环境变量覆盖
        base_url: 自定义端点（仅 OpenAI 路径）
        timeout: 请求超时（秒）

    Returns:
        客户端实例，支持 .chat() 方法
    """
    provider = provider.lower().strip()

    if provider in ("openai", "groq", "together", "vllm", "lmdeploy", "ollama", "local"):
        return get_openai_client(api_key=api_key, base_url=base_url, timeout=timeout)
    elif provider in ("anthropic", "claude"):
        return get_anthropic_client(api_key=api_key, base_url=base_url, timeout=timeout)
    else:
        raise ValueError(f"Unknown provider: {provider!r}. "
                         f"Supported: openai, anthropic, groq, together, vllm, lmdeploy, ollama, local")
