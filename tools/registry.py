# -*- coding: utf-8 -*-
"""工具注册中心 — 单例模式，支持文件变更钩子"""

import contextvars
import inspect
import json
import threading
from functools import lru_cache
from typing import Any, Callable, Dict, List, Optional

from event_bus import event_bus, UIEvent
from tools.models import ToolDef


@lru_cache(maxsize=256)
def accepts_timeout_param(handler) -> bool:
    """判断 handler 是否接受 timeout 参数。

    显式声明 timeout，或声明 **kwargs，都算接受。dispatch 用它剔除模型误传的
    timeout（否则 handler(**args) 直接 TypeError），agent_loop 用它决定要不要
    把参数里的 timeout 当作外层超时依据 —— 两处必须同一套判定。

    签名无法解析时返回 False：此时传 timeout 大概率也会失败，按「不接受」处理更安全。
    """
    if handler is None:
        return False
    try:
        params = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        return False
    if "timeout" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _parse_json_object(text: str) -> Optional[dict]:
    """若字符串是 JSON 对象则返回解析结果，否则 None（dispatch 据此决定是否透传）。

    先看首字符再解析：exec 的输出常是上百 KB 的普通文本，无谓的 json.loads
    既慢又必然失败；只有以 '{' 开头才可能是对象。

    仅认 dict：JSON 数组/标量仍走包装，避免改变既有的结果形状。
    """
    if not text or text.lstrip()[:1] != "{":
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


# Subagent 白名单 (contextvars，协程级隔离)
_subagent_whitelist: 'contextvars.ContextVar[Optional[set]]' = \
    contextvars.ContextVar('subagent_whitelist', default=None)


# 主 Agent 运行时 config（contextvars，供 subagent_run 复用主 Agent 的 config，而非重读磁盘）
_agent_config: 'contextvars.ContextVar[Optional[Any]]' = \
    contextvars.ContextVar('agent_config', default=None)


def set_agent_config(config: Any):
    """在主 Agent 执行工具期间设置当前 config，供 subagent_run 复用。返回 reset token。"""
    return _agent_config.set(config)


def reset_agent_config(token) -> None:
    """复位 set_agent_config 设置的 config。"""
    _agent_config.reset(token)


def get_agent_config() -> Optional[Any]:
    """获取当前主 Agent 的 config；若未设置则返回 None。"""
    return _agent_config.get()


# ToolRegistry
class ToolRegistry:
    """
    工具注册表
    用锁-单例模式（线程安全）
    """

    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._tools: Dict[str, ToolDef] = {}
        return cls._instance

    def register(
            self,
            name: str,
            description: str,
            parameters: Dict[str, Any],
            handler: Callable,
            parallel_safe: bool = False,
            timeout: Optional[float] = None,
            requires_approval: bool = False,
            preview_handler: Optional[Callable] = None,
    ) -> None:
        self._tools[name] = ToolDef(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
            parallel_safe=parallel_safe,
            timeout=timeout,
            requires_approval=requires_approval,
            preview_handler=preview_handler,
        )

    def get(self, name: str) -> Optional[ToolDef]:
        return self._tools.get(name)

    def get_all_names(self) -> List[str]:
        return list(self._tools.keys())

    def unregister(self, name: str) -> bool:
        """移除已注册的工具，返回是否成功"""
        return self._tools.pop(name, None) is not None

    def dispatch(self, name: str, args: Dict[str, Any]) -> str:
        """分派工具调用，返回 JSON 字符串

        这是所有工具执行的唯一入口点 —— hook (before_tool_call / after_tool_call)
        调用，hook 都能正确触发。
        """
        # Subagent 白名单过滤
        whitelist = _subagent_whitelist.get()
        if whitelist is not None and name not in whitelist:
            return json.dumps({"error": f"工具 '{name}' 不在 subagent 可用范围内"}, ensure_ascii=False)

        from plugins.hook_manager import hook_manager

        # Hook before_tool_call
        '''
        result {"block":True/False,"reason":""}
        '''
        result = hook_manager.trigger("before_tool_call", {
            "tool_name": name,
            "tool_args": args,
        })

        if not isinstance(result, dict):
            result = {}

        block = result.get("block")
        tool = self.get(name)
        if tool is None:
            output = json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False)
            error_flag = True

        elif block:
            output = json.dumps({"error": f"Tool '{name}' execution blocked, reason: {result.get('reason')}"}, ensure_ascii=False)
            error_flag = True

        else:
            # 审批检查
            if tool.requires_approval:
                from tools.approval import request_approval, ApprovalBreak
                preview = None
                if tool.preview_handler:
                    try:
                        preview = tool.preview_handler(**args)
                    except Exception:
                        pass
                try:
                    approved = request_approval(name, args, preview=preview)
                except ApprovalBreak:
                    return json.dumps({
                        "error": f"Tool '{name}' 被用户中断 (break)。",
                        "approval_break": True,
                    }, ensure_ascii=False)
                if not approved:
                    return json.dumps({
                        "error": f"Tool '{name}' 被用户拒绝执行，请调整方案。",
                        "approval_denied": True,
                    }, ensure_ascii=False)

            error_flag = False
            try:
                # 只剔除 timeout：模型可能给不支持该参数的工具传 timeout，
                # handler(**args) 会直接 TypeError，把一次正常调用变成报错。
                # 用 key 存在判定而非值非空，覆盖 timeout=null 这种 JSON 写法。
                call_args = args
                if "timeout" in args and not accepts_timeout_param(tool.handler):
                    call_args = {k: v for k, v in args.items() if k != "timeout"}
                result = tool.handler(**call_args)
                # handler 返回 dict（MCP 工具）、str、或有 to_dict() 的对象。
                # dict 与 to_dict() 同为「结构化结果」，一律直接序列化，不再包一层
                # success/data —— 否则 {"success":true,"data":{"content":[…],"isError":false}}
                # 会把 MCP 的字段埋深一层，且 error 判定失效。
                if isinstance(result, dict):
                    output = json.dumps(result, ensure_ascii=False)
                    # error 按真值判断：部分工具正常返回 {"success": true, "error": null}
                    error_flag = bool(result.get("error"))
                elif isinstance(result, str):
                    # handler 已返回 JSON 对象时直接透传，不再包一层。
                    # 否则超时/错误状态会被埋进字符串：{"success":true,"data":"{\"timed_out\":true}"}，
                    # 模型要多解析一层，且中文会被二次转义（实测膨胀近 2x）。
                    _obj = _parse_json_object(result)
                    if _obj is not None:
                        output = result
                        # 与被包装路径保持一致的 error 语义：让 after_tool_call hook 拿到真实结果
                        error_flag = bool(_obj.get("error"))
                    else:
                        output = json.dumps({"success": True, "data": result}, ensure_ascii=False)
                elif hasattr(result, 'to_dict'):
                    output = json.dumps(result.to_dict(), ensure_ascii=False)
                else:
                    output = json.dumps({"success": True, "data": str(result)}, ensure_ascii=False)
            except Exception as e:
                output = json.dumps({"error": f"Tool '{name}' execution failed: {e}"}, ensure_ascii=False)
                error_flag = True

        # Hook after_tool_call
        hook_manager.trigger("after_tool_call", {
            "tool_name": name,
            "tool_args": args,
            "result": output,
            "error": error_flag,
        })

        return output


# 全局注册表
registry = ToolRegistry()



def register_tool(name: str, description: str = "", parameters: Optional[Dict] = None,
                  parallel_safe: bool = False, requires_approval: bool = False,
                  preview_handler: Optional[Callable] = None,
                  timeout: Optional[float] = None):
    """
装饰器：注册工具"""
    def decorator(func: Callable):
        registry.register(
            name=name,
            description=description or func.__doc__ or "",
            parameters=parameters or {"type": "object", "properties": {}},
            handler=func,
            parallel_safe=parallel_safe,
            requires_approval=requires_approval,
            preview_handler=preview_handler,
            timeout=timeout,
        )
        return func

    return decorator
