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
    # thinking 块在该轮 content 中的序号。Anthropic 的 interleaved thinking
    # 一轮可能出现多个 thinking 块，必须按 index 分开累积，否则签名会串块。
    thinking_index: int = -1
    # Anthropic 扩展思考：signature 必须原样回传，否则多轮工具调用会报
    # "Expected thinking or redacted_thinking block"
    thinking_signature: str = ""
    # redacted_thinking block 的 data（不可解密，原样回传）
    thinking_redacted: str = ""
    tool_call_deltas: List[ToolCallDelta] = field(default_factory=list)
    finish_reason: str = ""


class UnifiedResponse:
    """
    统一的非流式响应。

    tool_calls 格式：[{"id": "...", "type": "function", "function": {"name": "...", "arguments": "..."}}]
    thinking_blocks 格式：[{"type": "thinking", "thinking": "...", "signature": "..."} |
                          {"type": "redacted_thinking", "data": "..."}]
    """
    def __init__(self, content="", thinking="", tool_calls=None, model=""):
        self.content = content
        self.thinking = thinking
        self.thinking_blocks: List[Dict] = []
        self.tool_calls = tool_calls or []
        self.model = model


# 流式重连辅助
def _chunk_has_payload(sc: "StreamChunk") -> bool:
    """判断 chunk 是否携带了会展示给用户/影响下游的内容。"""
    return bool(
        sc.content
        or sc.thinking
        or sc.tool_call_deltas
        or sc.finish_reason
        or getattr(sc, "thinking_signature", "")
        or getattr(sc, "thinking_redacted", "")
    )


class _RetryableStreamMixin:
    """流式迭代中断重连。

    只在**尚未产出任何内容**时重连：此时重放请求不会造成重复输出。
    一旦已经 yield 过内容（正文/思考/工具参数），中断只能向上抛——
    重放会重复输出，语义上不可自动恢复。
    """

    def _init_retry(self, reopen, max_attempts: int) -> None:
        self._reopen = reopen
        self._max_attempts = max(1, int(max_attempts or 1))
        self._attempt = 1
        self._emitted = False

    def _mark_emitted(self, sc: "StreamChunk") -> None:
        if not self._emitted and _chunk_has_payload(sc):
            self._emitted = True

    def _reopen_stream(self, exc: Exception) -> bool:
        """判断并执行重连。返回 True 表示已换上新流，应继续迭代。"""
        if self._reopen is None or self._emitted:
            return False
        if self._attempt >= self._max_attempts:
            return False
        if not _is_transient(exc):
            return False
        self._attempt += 1
        wait = 0.5 + random.uniform(0, 0.5)
        logger.warning(
            "stream interrupted before any output (%s), reconnecting %d/%d in %.1fs",
            type(exc).__name__, self._attempt, self._max_attempts, wait,
        )
        time.sleep(wait)
        self._stream = self._reopen()
        return True


# 流式适配器
class OpenAIStreamAdapter(_RetryableStreamMixin):
    """把 OpenAI stream chunks 转为统一的 StreamChunk 迭代器"""

    def __init__(self, openai_stream: Iterator, reopen=None, max_attempts: int = 3):
        self._stream = openai_stream
        self._init_retry(reopen, max_attempts)

    def __iter__(self):
        return self._next()

    def close(self) -> None:
        """关闭底层 HTTP 流，使阻塞的迭代立即退出（用于 ESC 中断）"""
        try:
            self._stream.close()
        except Exception:
            pass

    def _next(self):
        """迭代 chunk 流；尚未产出内容时的瞬时中断会自动重连。"""
        while True:
            try:
                yield from self._iter_once()
                return
            except Exception as exc:
                if not self._reopen_stream(exc):
                    raise

    def _iter_once(self):
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

            self._mark_emitted(sc)
            yield sc


# Anthropic stop_reason → OpenAI 风格 finish_reason。
# pause_turn / refusal 无对等值：refusal 映射为 content_filter，
# pause_turn 原样保留（表示可回传继续，不应被当作正常结束）。
_STOP_REASON_MAP = {
    "end_turn": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "refusal": "content_filter",
    "pause_turn": "pause_turn",
}


class AnthropicStreamAdapter(_RetryableStreamMixin):
    """把 Anthropic SSE 事件流转换为统一的 StreamChunk 迭代器"""

    def __init__(self, anthropic_stream: Iterator, reopen=None, max_attempts: int = 3):
        self._stream = anthropic_stream
        self._init_retry(reopen, max_attempts)

    def __iter__(self):
        return self._next()

    def close(self) -> None:
        """关闭底层 HTTP 流，使阻塞的迭代立即退出（用于 ESC 中断）"""
        try:
            self._stream.close()
        except Exception:
            pass

    def _next(self):
        """迭代 SSE 事件流；尚未产出内容时的瞬时中断会自动重连。"""
        while True:
            try:
                yield from self._iter_once()
                return
            except Exception as exc:
                if not self._reopen_stream(exc):
                    raise

    def _iter_once(self):
        for event in self._stream:
            sc = StreamChunk()
            event_type = getattr(event, "type", "")
            if not event_type and isinstance(event, dict):
                event_type = event.get("type", "")

            if event_type == "error":
                # 服务端在流中途下发的错误（overloaded_error 等）必须向上抛，
                # 否则会被当作空 chunk 吞掉，上层误以为正常结束。
                raise _parse_stream_error_event(event)

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
                    sc.thinking_index = getattr(event, "index", -1)
                elif cb and getattr(cb, "type", "") == "redacted_thinking":
                    sc.thinking_redacted = getattr(cb, "data", "") or ""
                    sc.thinking_index = getattr(event, "index", -1)

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
                        sc.thinking_index = getattr(event, "index", -1)
                    elif dt == "signature_delta":
                        sc.thinking_signature = getattr(delta, "signature", "") or ""
                        sc.thinking_index = getattr(event, "index", -1)

            elif event_type == "message_delta":
                delta = getattr(event, "delta", None)
                if delta is None and isinstance(event, dict):
                    delta = event.get("delta")
                if delta is not None:
                    stop = (getattr(delta, "stop_reason", None)
                            if not isinstance(delta, dict)
                            else delta.get("stop_reason")) or ""
                    if stop:
                        # 未知值原样透传，不猜测为 "stop" 以免误报正常结束
                        sc.finish_reason = _STOP_REASON_MAP.get(stop, stop)

            self._mark_emitted(sc)
            yield sc



class AnthropicStreamError(Exception):
    """Anthropic SSE 流中途下发的 error 事件（HTTP 200 之后的服务端错误）。

    SDK 只在 ``event: error`` 时抛错；部分网关会把它写成普通 data 消息，
    此时由适配器自行识别，避免被当成空 chunk 静默吞掉。
    """

    def __init__(self, error_type: str, message: str, retryable: bool = False):
        self.error_type = error_type
        self.retryable = retryable
        super().__init__(f"Anthropic stream error [{error_type}]: {message}")


# 可重试的服务端错误（限流 / 过载 / 网关超时 / 内部错误）
_STREAM_RETRYABLE_ERRORS = {
    "rate_limit_error",
    "overloaded_error",
    "api_error",
    "gateway_timeout_error",
    "timeout_error",
}


def _parse_stream_error_event(event) -> AnthropicStreamError:
    """从 error 事件中解析出错误类型与消息。"""
    err = getattr(event, "error", None)
    if err is None and isinstance(event, dict):
        err = event.get("error")

    if isinstance(err, dict):
        etype = str(err.get("type") or "unknown_error")
        emsg = str(err.get("message") or err)
    elif err is not None:
        etype = str(getattr(err, "type", "") or "unknown_error")
        emsg = str(getattr(err, "message", "") or err)
    else:
        etype = "unknown_error"
        emsg = str(event)

    return AnthropicStreamError(
        etype,
        emsg,
        retryable=etype in _STREAM_RETRYABLE_ERRORS,
    )


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


_QUOTA_PATTERNS = (
    "insufficient_quota",
    "insufficient_balance",
    "insufficientbalance",
    "quota_exceeded",
    "quotaexceeded",
    "allocationquota",
    "freetieronly",
    "arrearage",
    "overdue",
    "billing",
    "credit balance",
    "balance is low",
    "exceeded your current quota",
    "余额不足",
    "额度不足",
    "额度已用尽",
    "欠费",
)


def _exception_texts(exc: Exception) -> List[str]:
    """收集异常自身及 body 中可用于关键词匹配的文本。"""
    texts = [str(exc)]
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        def _walk(node):
            if isinstance(node, dict):
                for value in node.values():
                    _walk(value)
            elif isinstance(node, (list, tuple)):
                for value in node:
                    _walk(value)
            elif isinstance(node, str):
                texts.append(node)
        _walk(body)
    elif isinstance(body, str):
        texts.append(body)
    return texts


def _is_quota_error(exc: Exception) -> bool:
    """判断是否为余额/额度耗尽类错误（不可重试）。

    各家状态码不统一：OpenAI 用 429 insufficient_quota，DeepSeek 用 402，
    阿里云百炼多为 400/403 且错误码形如 Arrearage / AllocationQuotaExceeded，
    因此只能综合状态码、错误码和消息关键词判断。
    """
    status = _extract_http_status(exc)
    if status == 402:  # Payment Required，语义明确
        return True
    blob = " ".join(_exception_texts(exc)).lower()
    if any(p in blob for p in _QUOTA_PATTERNS):
        return True
    code = _extract_error_code(exc).lower()
    if code and any(p in code for p in _QUOTA_PATTERNS):
        return True
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return _is_quota_error(cause)
    return False


def _is_transient(exc: Exception) -> bool:
    """判断异常是否为瞬时错误（值得重试）。"""
    # 余额/额度问题重试无意义，直接排除（优先于流错误自带的可重试判定）
    if _is_quota_error(exc):
        return False
    if isinstance(exc, AnthropicStreamError):
        # 服务端在流中下发的错误自带可重试判定
        return exc.retryable
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


_ERROR_HINTS = {
    401: "API Key 无效或已过期，请检查配置",
    403: "无访问权限，或该模型未开通",
    404: "接口地址或模型名不存在，请检查 base_url / model",
    429: "请求过于频繁，请稍后重试",
    500: "服务端内部错误，请稍后重试",
    502: "网关错误，请稍后重试",
    503: "服务暂不可用，请稍后重试",
    504: "服务端响应超时，请稍后重试",
}


def _extract_http_status(exc: Exception) -> Optional[int]:
    """从各 SDK 异常里尽力取出 HTTP 状态码。"""
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return _extract_http_status(cause)
    return None


def _extract_error_code(exc: Exception) -> str:
    """取出 provider 返回的业务错误码，如 InvalidApiKey。"""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        for key in ("code", "type", "error"):
            value = body.get(key)
            if isinstance(value, dict):
                value = value.get("code") or value.get("type")
            if isinstance(value, str) and value:
                return value
    if isinstance(exc, AnthropicStreamError):
        return exc.error_type or ""
    return ""


def format_api_error(exc: Exception) -> str:
    """把各类 API 异常压缩成一行人类可读的提示。

    例：``API 调用失败：API Key 无效或已过期，请检查配置（401 InvalidApiKey）``
    """
    status = _extract_http_status(exc)
    code = _extract_error_code(exc)
    exc_name = type(exc).__name__

    if _is_quota_error(exc):
        hint = "账户余额不足或额度已用尽，请充值后重试"
    elif isinstance(exc, AnthropicStreamError):
        hint = str(exc)
        code = ""  # 错误类型已包含在 str 里，避免重复
    elif status is not None:
        hint = _ERROR_HINTS.get(status) or _ERROR_HINTS.get(status // 100 * 100) or "请求失败"
    elif "Timeout" in exc_name or "Timeout" in str(type(exc)) or "TimeoutError" in exc_name:
        hint = "请求超时，请检查网络或稍后重试"
    elif "Connection" in exc_name or "ProxyError" in exc_name or "RemoteDisconnected" in exc_name:
        hint = "无法连接到 API 服务，请检查网络或 base_url"
    else:
        # 兜底：取首行、去掉超长内容
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else exc_name
        hint = first_line[:160]

    suffix = ""
    if status is not None or code:
        suffix = f"（{' '.join(str(x) for x in (status, code) if x)}）"
    return f"API 调用失败：{hint}{suffix}"


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
                # 迭代中若在产出任何内容前中断，用同一组参数重开流
                return OpenAIStreamAdapter(
                    raw,
                    reopen=lambda: openai.ChatCompletion.create(
                        **params, request_timeout=_timeout_seq[0]
                    ),
                    max_attempts=3,
                )
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
                # 迭代中若在产出任何内容前中断，用同一组参数重开流
                return OpenAIStreamAdapter(
                    raw,
                    reopen=lambda: self._client_v1.chat.completions.create(
                        **chat_kwargs, timeout=_timeout_seq[0]
                    ),
                    max_attempts=3,
                )
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
        system_parts: List[Dict] = []
        filtered_messages = []
        for msg in messages:
            if msg.get("role") != "system":
                filtered_messages.append(msg)
                continue
            # 多条 system 按顺序合并；content 可能是字符串或 block 数组
            # （block 数组用于 cache_control，必须原样保留）
            c = msg.get("content")
            if isinstance(c, str):
                if c.strip():
                    system_parts.append({"type": "text", "text": c})
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, str):
                        if b.strip():
                            system_parts.append({"type": "text", "text": b})
                    elif isinstance(b, dict) and b.get("type") == "text":
                        if (b.get("text") or "").strip():
                            system_parts.append(b)

        # max_tokens 收敛到模型上限：超限会被 Anthropic 直接 400
        max_tokens = _normalize_max_tokens(model, kwargs.get("max_tokens"))

        # thinking 是 Messages API 的顶层参数：从 extra_body 中提取后交给 SDK 序列化，
        # 同时规范化 budget_tokens（Anthropic 要求必填且 1024 <= budget < max_tokens）。
        extra_body = kwargs.get("extra_body")
        extra_body = dict(extra_body) if isinstance(extra_body, dict) else {}
        thinking_cfg = None
        raw_thinking = extra_body.pop("thinking", None)
        if isinstance(raw_thinking, dict):
            thinking_cfg = _normalize_thinking(raw_thinking, max_tokens)
        thinking_enabled = thinking_cfg is not None and thinking_cfg.get("type") == "enabled"

        anthropic_messages = []
        _pending_tool_results = []
        for msg in filtered_messages:
            role = msg.get("role")
            if role == "tool":
                tool_use_id = msg.get("tool_call_id") or ""
                if not tool_use_id:
                    # Anthropic 要求 tool_result 必须带 tool_use_id，缺失会被 400 拒绝
                    logger.warning("drop tool message without tool_call_id")
                    continue
                _pending_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": msg.get("content", ""),
                    "is_error": bool(msg.get("is_error", False)),
                })
            else:
                if _pending_tool_results:
                    anthropic_messages.append({
                        "role": "user",
                        "content": _pending_tool_results,
                    })
                    _pending_tool_results = []
                if role == "assistant":
                    converted = _assistant_to_anthropic(msg, thinking_enabled=thinking_enabled)
                    if converted is not None:
                        anthropic_messages.append(converted)
                else:
                    # 只保留 role/content，丢弃 reasoning_content、tool_call_id 等
                    # OpenAI/内部私有字段（SDK 对未知字段原样透传，Anthropic 会 400）
                    sanitized = _sanitize_anthropic_message(msg)
                    if sanitized is not None:
                        anthropic_messages.append(sanitized)
        if _pending_tool_results:
            anthropic_messages.append({
                "role": "user",
                "content": _pending_tool_results,
            })

        create_kwargs = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
        }
        if system_parts:
            # 单段且无 cache_control 时用字符串，其余用 block 数组
            if len(system_parts) == 1 and "cache_control" not in system_parts[0]:
                create_kwargs["system"] = system_parts[0]["text"]
            else:
                create_kwargs["system"] = system_parts
        if tool_schemas:
            create_kwargs["tools"] = _openai_tools_to_anthropic(tool_schemas)

        if thinking_cfg is not None:
            create_kwargs["thinking"] = thinking_cfg

        # 开启 thinking 时 Anthropic 只接受默认采样参数，传非默认值会 400
        for k in ("temperature", "top_p"):
            if k not in kwargs:
                continue
            if thinking_enabled:
                if k == "temperature" and kwargs[k] != 1:
                    logger.debug("drop temperature=%s because thinking is enabled", kwargs[k])
                elif k == "temperature":
                    create_kwargs[k] = 1
                else:
                    logger.debug("drop top_p=%s because thinking is enabled", kwargs[k])
                continue
            create_kwargs[k] = kwargs[k]

        is_stream = kwargs.get("stream", False)
        if is_stream:
            create_kwargs["stream"] = True

        # 透传剩余 extra_body（如 DeepSeek 的 output_config 等扩展参数）。
        # 必须通过 SDK 的 extra_body 参数传给请求体，不能展开成顶层 kwarg，
        # 否则 reasoning_effort 等扩展字段会被当作 create() 的非法关键字参数。

        _timeout_seq = [10.0] * 5 + [15.0] * 5
        raw = _retry(
            lambda timeout: self._client.messages.create(**create_kwargs, extra_body=extra_body, timeout=timeout),
            max_attempts=10,
            timeout_seq=_timeout_seq,
        )

        if is_stream:
            # 迭代中若在产出任何内容前中断，用同一组参数重开流
            return AnthropicStreamAdapter(
                raw,
                reopen=lambda: self._client.messages.create(
                    **create_kwargs, extra_body=extra_body, timeout=_timeout_seq[0]
                ),
                max_attempts=3,
            )
        return _anthropic_raw_to_unified(raw)


# 各模型族的输出 token 上限。Anthropic 对超过上限的 max_tokens 直接 400，
# 因此这里统一做 min(请求值, 上限) 收敛。
_MODEL_MAX_OUTPUT = (
    # 长前缀优先匹配
    ("claude-opus-4", 32000),
    ("claude-sonnet-4", 64000),
    ("claude-haiku-4", 32000),
    ("claude-3-7-sonnet", 64000),
    ("claude-3-5-sonnet", 8192),
    ("claude-3-5-haiku", 8192),
    ("claude-3-opus", 4096),
    ("claude-3-sonnet", 4096),
    ("claude-3-haiku", 4096),
)
# 未知模型取保守值，避免撞上 4k 上限的旧模型
_DEFAULT_MAX_OUTPUT = 8192


def _model_max_output(model: str) -> int:
    """按模型名解析最大输出 token 数。"""
    name = (model or "").lower()
    for prefix, limit in _MODEL_MAX_OUTPUT:
        if name.startswith(prefix):
            return limit
    return _DEFAULT_MAX_OUTPUT


def _normalize_max_tokens(model: str, requested) -> int:
    """把 max_tokens 收敛到模型上限内。"""
    limit = _model_max_output(model)
    if not isinstance(requested, int) or requested <= 0:
        return limit
    if requested > limit:
        logger.debug(
            "clamp max_tokens %s -> %s for model %s", requested, limit, model
        )
        return limit
    return requested


def _normalize_thinking(cfg: Dict, max_tokens: int) -> Optional[Dict]:
    """规范化 thinking 配置。

    Anthropic 硬性约束：
    - type=enabled 时 budget_tokens 必填
    - 1024 <= budget_tokens < max_tokens
    不满足时补默认值；max_tokens 过小以致无法满足下限则放弃开启
    （返回 None），避免必然 400 的请求。
    """
    merged = dict(cfg)
    ttype = (merged.get("type") or "").lower()
    if ttype != "enabled":
        return merged or None

    try:
        max_tokens = int(max_tokens)
    except (TypeError, ValueError):
        max_tokens = 20480

    budget = merged.get("budget_tokens")
    if not isinstance(budget, int) or budget <= 0:
        budget = 4096

    # budget 必须严格小于 max_tokens，且不低于 1024
    upper = max_tokens - 1
    if upper < 1024:
        logger.warning(
            "thinking disabled: max_tokens=%s is too small (need > 1024) for budget_tokens",
            max_tokens,
        )
        return None
    budget = max(1024, min(budget, upper))
    merged["budget_tokens"] = budget
    return merged


def _sanitize_anthropic_message(msg: Dict) -> Optional[Dict]:
    """把内部/OpenAI 风格消息裁剪为 Anthropic 合法结构。

    只保留 role 与 content，避免 reasoning_content / tool_calls / tool_call_id
    等私有字段被 SDK 原样透传进请求体（Anthropic 对未知字段返回 400）。
    空内容消息（Anthropic 会以 text block 不能为空拒绝）直接丢弃。
    """
    role = msg.get("role")
    if role not in ("user", "assistant"):
        return None

    content = msg.get("content", "")
    if isinstance(content, str):
        if not content.strip():
            return None
        return {"role": role, "content": [{"type": "text", "text": content}]}
    if isinstance(content, list):
        blocks = [
            b for b in content
            if isinstance(b, dict) and b.get("type")
        ]
        if not blocks:
            return None
        return {"role": role, "content": blocks}
    return None


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


def _thinking_blocks_to_anthropic(msg: Dict) -> List[Dict]:
    """把消息里的 thinking 块列表转为 Anthropic content block。

    支持 interleaved thinking（一轮多个 thinking 块）：
    - thinking_blocks 为有序列表，逐块原样回传
    - 缺 signature 的 thinking 块会被 Anthropic 判为非法，跳过不传
    兼容旧字段 thinking_signature / thinking_redacted（单块）。
    """
    raw_blocks = msg.get("thinking_blocks") or []
    if not raw_blocks:
        # 兼容旧形态
        redacted = msg.get("thinking_redacted") or ""
        signature = msg.get("thinking_signature") or ""
        reasoning = msg.get("reasoning_content")
        if redacted:
            raw_blocks = [{"type": "redacted_thinking", "data": redacted}]
        elif signature and reasoning is not None:
            raw_blocks = [{
                "type": "thinking",
                "thinking": reasoning,
                "signature": signature,
            }]
        else:
            raw_blocks = []

    result = []
    for b in raw_blocks:
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        if btype == "redacted_thinking":
            data = b.get("data") or ""
            if data:
                result.append({"type": "redacted_thinking", "data": data})
        elif btype == "thinking":
            signature = b.get("signature") or ""
            text = b.get("thinking")
            if signature and text is not None:
                result.append({
                    "type": "thinking",
                    "thinking": text,
                    "signature": signature,
                })
    return result


def _assistant_to_anthropic(msg: Dict, thinking_enabled: bool = False) -> Optional[Dict]:
    """把内部/OpenAI 格式的 assistant 消息转为 Anthropic content block 格式。

    开启扩展思考时，thinking / redacted_thinking block 必须原样置顶回传，
    否则带 tool_use 的历史会在下一轮被 Anthropic 以
    "Expected thinking or redacted_thinking block" 拒绝。
    """
    content = msg.get("content", "") or ""
    tool_calls = msg.get("tool_calls") or []
    blocks = []

    if thinking_enabled:
        blocks.extend(_thinking_blocks_to_anthropic(msg))

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
    if not blocks:
        return None
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
    thinking_blocks: List[Dict] = []
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
            text = getattr(block, "thinking", "") or ""
            thinking += text
            thinking_blocks.append({
                "type": "thinking",
                "thinking": text,
                "signature": getattr(block, "signature", "") or "",
            })
        elif t == "redacted_thinking":
            thinking_blocks.append({
                "type": "redacted_thinking",
                "data": getattr(block, "data", "") or "",
            })
    resp = UnifiedResponse(
        content="\n".join(content_parts),
        thinking=thinking,
        tool_calls=tool_calls,
        model=getattr(raw, "model", ""),
    )
    resp.thinking_blocks = thinking_blocks
    return resp



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
