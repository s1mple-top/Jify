# -*- coding: utf-8 -*-
"""
HookManager — Hook 注册中心（单例）

支持的 8 个 Hook 点：

    before_prompt_build   → system prompt 构建前
    after_prompt_build    → messages 组装完毕
    llm_input             → 每轮迭代发送给模型前
    before_api_call       → 实际 API 调用前
    after_api_call        → API 调用后
    before_tool_call      → 每个工具执行前
    after_tool_call       → 每个工具执行后
    llm_output            → 最终输出前（无 tool_calls 时）
"""

import traceback
from typing import Any, Callable, Dict, List

from event_bus import event_bus, UIEvent

# 合法的 hook 点名称集合
_VALID_HOOK_POINTS = frozenset({
    "before_prompt_build",
    "after_prompt_build",
    "llm_input",
    "before_api_call",
    "after_api_call",
    "before_tool_call",
    "after_tool_call",
    "llm_output",
})


class HookManager:
    """单例 Hook 注册 / 触发中心。

    使用方式：
        from plugins.hook_manager import hook_manager

        # 注册
        hook_manager.register("before_api_call", my_func)

        # 触发（上下文可变，hook 可以修改后返回）
        ctx = hook_manager.trigger("before_api_call", {"messages": [...]})
    """

    def __init__(self):
        self._hooks: Dict[str, List[Callable[[dict], dict]]] = {
            k: [] for k in _VALID_HOOK_POINTS
        }

    # 注册

    def register(self, hook_point: str, func: Callable[[dict], dict]) -> bool:
        """注册一个 hook 函数到指定 hook 点。

        Returns:
            True 注册成功，False 注册失败（非法 hook 点或已注册）
        """
        if hook_point not in _VALID_HOOK_POINTS:
            event_bus.put(UIEvent(
                "ERROR",
                f"[ Hook ] unknown hook point: '{hook_point}' "
                f"(valid: {', '.join(sorted(_VALID_HOOK_POINTS))})"
            ))
            return False
        if func in self._hooks[hook_point]:
            return False  # 已注册，跳过
        self._hooks[hook_point].append(func)
        return True

    # 触发
    def trigger(self, hook_point: str, context: dict) -> dict:
        """串行触发指定 hook 点所有注册函数。

        Args:
            hook_point: hook 点名称
            context: 可变上下文 dict（hook 函数可修改后返回）

        Returns:
            经过所有 hook 处理后的 context dict
        """
        for func in self._hooks.get(hook_point, []):
            try:
                context = func(context)
            except Exception as e:
                event_bus.put(UIEvent(
                    "ERROR",
                    f"[ Hook ] {hook_point}: {e}\n{traceback.format_exc()}"
                ))
        return context

    # 自动发现

    def auto_register_from_module(self, module, plugin_name: str = "") -> int:
        """扫描模块属性，将与 hook 点同名的函数自动注册。

        例如 hooks.py 中定义 def before_api_call(ctx): ...，
        会被自动注册到 "before_api_call"。

        Returns:
            成功注册的函数数量
        """
        count = 0
        for hook_point in _VALID_HOOK_POINTS:
            func = getattr(module, hook_point, None)
            if callable(func):
                self.register(hook_point, func)
                count += 1
        # if count:
            # event_bus.put(UIEvent(
            #     "TEXT",
            #     f"[ Hook ] {plugin_name}: registered {count} hook(s)"
            # ))
        return count

    # 查询

    def list_hooks(self) -> Dict[str, int]:
        """返回 {hook_point: registered_count}"""
        return {k: len(v) for k, v in self._hooks.items()}

    def get_hook_count(self, hook_point: str) -> int:
        """获取某个 hook 点的注册数量"""
        return len(self._hooks.get(hook_point, []))


# ── 全局单例 ──────────────────────────────────────────────────────────
hook_manager = HookManager()
