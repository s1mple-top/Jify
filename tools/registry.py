# -*- coding: utf-8 -*-
"""工具注册中心 — 单例模式，支持文件变更钩子"""

import contextvars
import json
import threading
from typing import Any, Callable, Dict, List, Optional

from event_bus import event_bus, UIEvent
from tools.models import ToolDef


# Subagent 白名单 (contextvars，协程级隔离)
_subagent_whitelist: 'contextvars.ContextVar[Optional[set]]' = \
    contextvars.ContextVar('subagent_whitelist', default=None)


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
            return json.dumps({"error": f"工具 '{name}' 不在 subagent 可用范围内"})

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
            output = json.dumps({"error": f"Unknown tool: {name}"})
            error_flag = True

        elif block:
            output = json.dumps({"error": f"Tool '{name}' execution blocked, reason: {result.get('reason')}"})
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
                    })
                if not approved:
                    return json.dumps({
                        "error": f"Tool '{name}' 被用户拒绝执行，请调整方案。",
                        "approval_denied": True,
                    })

            error_flag = False
            try:
                result = tool.handler(**args)
                # handler 返回 dict（MCP 工具）、str、或有 to_dict() 的对象
                if isinstance(result, dict):
                    output = json.dumps({"success": True, "data": result})
                elif isinstance(result, str):
                    output = json.dumps({"success": True, "data": result})
                elif hasattr(result, 'to_dict'):
                    output = json.dumps(result.to_dict())
                else:
                    output = json.dumps({"success": True, "data": str(result)})
            except Exception as e:
                output = json.dumps({"error": f"Tool '{name}' execution failed: {e}"})
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
                  preview_handler: Optional[Callable] = None):
    """装饰器：注册工具"""

    def decorator(func: Callable):
        registry.register(
            name=name,
            description=description or func.__doc__ or "",
            parameters=parameters or {"type": "object", "properties": {}},
            handler=func,
            parallel_safe=parallel_safe,
            requires_approval=requires_approval,
            preview_handler=preview_handler,
        )
        return func

    return decorator
