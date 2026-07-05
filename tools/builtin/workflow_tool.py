# # -*- coding: utf-8 -*-
# """
# run_workflow 工具 — 让 LLM 可以调用预定义的 workflow
# """
#
# from typing import Optional
#
# from event_bus import event_bus, UIEvent
# from tools.registry import register_tool
# from tools.workflow_engine import WorkflowEngine
#
# # 单例引擎（在 main 启动时调用 discover() 初始化）
# engine = WorkflowEngine()
#
#
# def _get_available_workflows() -> str:
#     """获取可用 workflow 列表的描述"""
#     names = engine.discover()
#     if not names:
#         return "No workflows found"
#     return ", ".join(names)
#
#
# def _on_workflow_progress(event_type: str, data: dict) -> None:
#     """将 workflow 进度事件推送到 event_bus"""
#     try:
#         event_bus.put(UIEvent(type='workflow_step', data={"event": event_type, **data}))
#     except Exception:
#         pass  # event_bus 不可用时静默忽略（如 GUI 模式）
#
#
# @register_tool(
#     name="run_workflow",
#     description=(
#         "执行预定义的 workflow。Workflow 是一组按 YAML 配置编排的步骤序列，"
#         "支持 tool 调用、LLM 推理、串行/并行调度。"
#         "可用 workflows: "
#         + _get_available_workflows()
#     ),
#     parameters={
#         "type": "object",
#         "properties": {
#             "workflow_name": {
#                 "type": "string",
#                 "description": "workflow 名称（对应 workflows/ 目录下的 YAML 文件名，不含 .yaml 后缀）",
#             },
#             "params": {
#                 "type": "object",
#                 "description": "workflow 参数（键值对，根据具体 workflow 的参数定义传入）",
#             },
#         },
#         "required": ["workflow_name"],
#     },
#     parallel_safe=False,
#     # requires_approval=False,
# )
# def run_workflow(workflow_name: str, params: Optional[dict] = None) -> str:
#     """
#     执行指定 workflow。
#
#     Args:
#         workflow_name: workflow 名称
#         params: 输入参数
#
#     Returns:
#         JSON 格式的执行结果
#     """
#     params = params or {}
#     return engine.execute(workflow_name, params, on_progress=_on_workflow_progress)
