# # -*- coding: utf-8 -*-
# """
# WorkflowEngine — YAML 驱动的 DAG 编排引擎
#
# 设计原则：
# - 纯配置驱动，不写 Python 代码即可定义流程
# - 复用现有 ToolRegistry.dispatch()
# - DAG 拓扑排序 → 并行组调度
# - 变量模板插值（{{ parameters.X }} / {{ steps.Y.output }}）
# """
#
# import json
# import os
# import re
# import threading
# import time
# from concurrent.futures import ThreadPoolExecutor, as_completed
# from dataclasses import dataclass, field
# from pathlib import Path
# from typing import Any, Dict, List, Optional
#
# import yaml
#
# # from tools.builtin.workflow_tool import engine
#
#
# # 数据模型
#
#
# @dataclass
# class StepDef:
#     """单个步骤定义"""
#     id: str
#     tool: Optional[str] = None          # 工具名 → registry.dispatch()
#     args: Dict[str, Any] = field(default_factory=dict)
#     use_llm: bool = False               # 是否调用 LLM
#     prompt: str = ""                    # LLM prompt（模板）
#     model: str = ""                     # 覆盖默认模型
#     depends_on: List[str] = field(default_factory=list)
#     on_failure: str = "abort"           # abort | skip | continue
#     retry: int = 0
#     timeout: int = 0
#     condition: str = ""                 # Jinja2 表达式（简单版）
#     run_always: bool = False
#     output_key: str = ""                # 自定义输出变量名
#     children: List['StepDef'] = field(default_factory=list)  # parallel 子步骤
#
#
# @dataclass
# class StepResult:
#     """步骤执行结果"""
#     id: str
#     success: bool
#     output: str = ""
#     parsed: Any = None
#     duration: float = 0.0
#     error: str = ""
#
#
# @dataclass
# class WorkflowDef:
#     """完整 workflow 定义"""
#     name: str
#     version: str
#     description: str = ""
#     parameters: List[Dict[str, Any]] = field(default_factory=list)
#     setup: List[Dict[str, Any]] = field(default_factory=list)
#     steps: List[StepDef] = field(default_factory=list)
#     output: Dict[str, str] = field(default_factory=dict)
#
#
# # 模板引擎（轻量，无 Jinja2 依赖）
#
# _VAR_RE = re.compile(r"\{\{\s*([^}]+)\s*\}\}")
#
#
# def _resolve_template(template: str, ctx: Dict[str, Any]) -> str:
#     """解析 {{ key }} 模板变量"""
#
#     def _replace(match):
#         expr = match.group(1).strip()
#         # 处理管道过滤器
#         parts = expr.split("|")
#         key = parts[0].strip()
#         filters = [p.strip() for p in parts[1:]]
#
#         value = _ctx_get(ctx, key)
#
#         # 应用过滤器
#         for f in filters:
#             if f == "basename" and isinstance(value, str):
#                 value = os.path.basename(value)
#             elif f == "trim" and isinstance(value, str):
#                 value = value.strip()
#             elif f == "from_json" and isinstance(value, str):
#                 try:
#                     value = json.loads(value)
#                 except (json.JSONDecodeError, TypeError):
#                     pass
#             elif f == "to_json":
#                 value = json.dumps(value, ensure_ascii=False)
#         return str(value) if value is not None else ""
#
#     return _VAR_RE.sub(_replace, template)
#
#
# def _ctx_get(ctx: Dict[str, Any], key: str) -> Any:
#     """从上下文中获取值，支持点号路径"""
#     keys = key.split(".")
#     val = ctx
#     for k in keys:
#         if isinstance(val, dict):
#             val = val.get(k)
#         elif hasattr(val, k):
#             val = getattr(val, k)
#         else:
#             return None
#     return val
#
#
# def _resolve_dict(d: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
#     """递归解析字典中所有模板变量"""
#     result = {}
#     for k, v in d.items():
#         if isinstance(v, str):
#             result[k] = _resolve_template(v, ctx)
#         elif isinstance(v, dict):
#             result[k] = _resolve_dict(v, ctx)
#         elif isinstance(v, list):
#             result[k] = [_resolve_template(x, ctx) if isinstance(x, str) else x for x in v]
#         else:
#             result[k] = v
#     return result
#
#
#
# class WorkflowEngine:
#     """YAML 驱动的 workflow 执行引擎"""
#
#     def __init__(self, workflow_dir: str = "workflows", max_workers: int = 8):
#         self.workflow_dir = Path(workflow_dir)
#         self.max_workers = max_workers
#         self._workflows: Dict[str, WorkflowDef] = {}
#         self._lock = threading.Lock()
#
#         # 确保 workflow_dir 是绝对路径
#         # 使用 __file__ 而非 Path.cwd()，因为 CWD 在 debug 时可能是 tools/ 目录
#         if not self.workflow_dir.is_absolute():
#             self.workflow_dir = (Path(__file__).resolve().parent.parent / self.workflow_dir).resolve()
#
#
#     def discover(self) -> List[str]:
#         """扫描 workflows/*.yaml，返回所有 workflow 名称列表"""
#         if not self.workflow_dir.exists():
#             return []
#
#         names = []
#         for entry in sorted(self.workflow_dir.iterdir()):
#             if entry.is_file() and entry.suffix in (".yaml", ".yml"):
#                 names.append(entry.stem)
#         return names
#
#     def load_all(self) -> Dict[str, WorkflowDef]:
#         """加载所有 workflow 配置"""
#         with self._lock:
#             for name in self.discover():
#                 if name not in self._workflows:
#                     try:
#                         self._workflows[name] = self._load_one(name)
#                     except Exception as e:
#                         print(f"[WorkflowEngine] Failed to load '{name}': {e}")
#         return dict(self._workflows)
#
#     def reload(self, name: str) -> WorkflowDef:
#         """重新加载单个 workflow"""
#         with self._lock:
#             wf = self._load_one(name)
#             self._workflows[name] = wf
#             return wf
#
#     def _load_one(self, name: str) -> WorkflowDef:
#         """解析单个 YAML → WorkflowDef"""
#         filepath = self.workflow_dir / f"{name}.yaml"
#         if not filepath.exists():
#             filepath = self.workflow_dir / f"{name}.yml"
#         if not filepath.exists():
#             raise FileNotFoundError(f"Workflow '{name}' not found at {self.workflow_dir}")
#
#         raw = yaml.safe_load(filepath.read_text(encoding="utf-8")) or {}
#         return self._parse_def(raw)
#
#     def _parse_def(self, raw: dict) -> WorkflowDef:
#         """解析 YAML 字典 → WorkflowDef"""
#         steps_raw = raw.get("steps", [])
#         steps = [self._parse_step(s) for s in steps_raw]
#
#         return WorkflowDef(
#             name=raw.get("name", ""),
#             version=raw.get("version", "0.1"),
#             description=raw.get("description", ""),
#             parameters=raw.get("parameters", []),
#             setup=raw.get("setup", []),
#             steps=steps,
#             output=raw.get("output", {}),
#         )
#
#     def _parse_step(self, raw: dict) -> StepDef:
#         """解析单步 → StepDef"""
#         # 处理 parallel 块
#         if "parallel" in raw:
#             children = [self._parse_step(s) for s in raw["parallel"]]
#             return StepDef(
#                 id=raw.get("id", "parallel"),
#                 children=children,
#                 depends_on=raw.get("depends_on", []),
#                 on_failure=raw.get("on_failure", "abort"),
#             )
#
#         return StepDef(
#             id=raw["id"],
#             tool=raw.get("tool"),
#             args=raw.get("args", {}),
#             use_llm=raw.get("use") == "llm",
#             prompt=raw.get("prompt", ""),
#             model=raw.get("model", ""),
#             depends_on=raw.get("depends_on", []),
#             on_failure=raw.get("on_failure", "abort"),
#             retry=raw.get("retry", 0),
#             timeout=raw.get("timeout", 0),
#             condition=raw.get("condition", ""),
#             run_always=raw.get("run_always", False),
#             output_key=raw.get("output_key", ""),
#         )
#
#     # DAG 拓扑排序
#
#     @staticmethod
#     def resolve_dag(steps: List[StepDef]) -> List[List[StepDef]]:
#         """
#         拓扑排序 → 并行组列表
#         返回: [[并行组1], [并行组2], ...]
#         """
#         # 构建依赖图
#         in_degree: Dict[str, int] = {}
#         graph: Dict[str, List[str]] = {}  # id → 依赖了哪些后续节点
#
#         for step in steps:
#             in_degree[step.id] = len(step.depends_on)
#             graph.setdefault(step.id, [])
#             for dep in step.depends_on:
#                 graph.setdefault(dep, []).append(step.id)
#
#         # 确保所有被引用的 dep 都在图中
#         for step in steps:
#             for dep in step.depends_on:
#                 if dep not in in_degree:
#                     in_degree[dep] = 0
#                     graph.setdefault(dep, [])
#
#         # Kahn 算法
#         queue = [sid for sid, deg in in_degree.items() if deg == 0]
#         id_to_step = {s.id: s for s in steps}
#         groups: List[List[StepDef]] = []
#         visited = set()
#
#         while queue:
#             group = [id_to_step[sid] for sid in queue if sid in id_to_step]
#             if group:
#                 groups.append(group)
#             next_queue = []
#             for sid in queue:
#                 visited.add(sid)
#                 for target in graph.get(sid, []):
#                     if target in in_degree:
#                         in_degree[target] -= 1
#                         if in_degree[target] == 0 and target not in visited:
#                             next_queue.append(target)
#             queue = next_queue
#
#         return groups
#
#     # 核心执行
#
#     def execute(self, name: str, params: Optional[Dict[str, Any]] = None,
#                 on_progress=None) -> str:
#         """
#         执行 workflow，返回 JSON 结果字符串。
#
#         Args:
#             name: workflow 名称
#             params: 输入参数字典
#             on_progress: 可选进度回调 on_progress(event_type, data)
#                          event_type: "init" | "step" | "done"
#
#         Returns:
#             JSON 格式结果
#         """
#         params = params or {}
#
#         # 1. 加载
#         with self._lock:
#             if name not in self._workflows:
#                 self._workflows[name] = self._load_one(name)
#             wf = self._workflows[name]
#
#         # 2. 校验参数
#         for p in wf.parameters:
#             if p.get("required") and p["name"] not in params:
#                 return json.dumps({
#                     "error": f"Missing required parameter: {p['name']}",
#                     "workflow": name,
#                 })
#             if p["name"] not in params and "default" in p:
#                 params[p["name"]] = p["default"]
#
#         # 3. 构建执行上下文
#         ctx = {
#             "parameters": params,
#             "steps": {},
#             "env": dict(os.environ),
#             "workflow": {"name": wf.name, "version": wf.version},
#             "_on_progress": on_progress,
#             "_workflow_name": name,
#         }
#
#         # 3.1. 发送 init 事件（所有步骤 pending）
#         if on_progress and wf.steps:
#             steps_info = []
#             for step in wf.steps:
#                 info = {"id": step.id, "state": "pending"}
#                 if step.use_llm:
#                     info["tool"] = "llm"
#                 elif step.children:
#                     info["tool"] = "parallel"
#                 else:
#                     info["tool"] = step.tool or "?"
#                 steps_info.append(info)
#             on_progress("init", {"workflow_name": name, "steps": steps_info})
#
#         # 4. 临时切换 CWD 到 workflow_dir，确保所有相对路径（如 write_file）
#         #    从 workflow 目录解析，而不是依赖外部 CWD
#         old_cwd = os.getcwd()
#         os.chdir(str(self.workflow_dir))
#         try:
#             # 4.1 执行 setup（前置校验）
#             if wf.setup:
#                 from tools.registry import registry
#                 for setup_step in wf.setup:
#                     tool_name = setup_step.get("tool", "")
#                     tool_args = _resolve_dict(setup_step.get("args", {}), ctx)
#                     result_json = registry.dispatch(tool_name, tool_args)
#                     result = json.loads(result_json)
#                     if "error" in result:
#                         return json.dumps({
#                             "error": f"Setup failed at '{tool_name}': {result['error']}",
#                             "workflow": name,
#                         })
#
#             # 5. DAG 调度执行
#             if not wf.steps:
#                 return json.dumps({"success": True, "data": "No steps defined", "workflow": name})
#
#             groups = self.resolve_dag(wf.steps)
#
#             for group in groups:
#                 if len(group) == 1:
#                     # 串行
#                     step = group[0]
#                     result = self._execute_step(step, ctx)
#                     if result:
#                         ctx["steps"][step.id] = result
#                 else:
#                     # 并行
#                     with ThreadPoolExecutor(max_workers=min(len(group), self.max_workers)) as pool:
#                         futures = {pool.submit(self._execute_step, s, ctx): s for s in group}
#                         for future in as_completed(futures):
#                             step = futures[future]
#                             try:
#                                 result = future.result()
#                             except Exception as e:
#                                 result = StepResult(
#                                     id=step.id, success=False,
#                                     error=str(e), duration=0.0,
#                                 )
#                             if result:
#                                 ctx["steps"][step.id] = result
#
#             # 5.1. 发送 done 事件
#             if on_progress:
#                 on_progress("done", {"workflow_name": name})
#         finally:
#             os.chdir(old_cwd)
#
#         # 6. 构建最终输出
#         if wf.output:
#             output = _resolve_dict(wf.output, ctx)
#         else:
#             # 默认输出：最后一步的结果
#             last_step = wf.steps[-1]
#             last = ctx["steps"].get(last_step.id)
#             output = {
#                 "result": last.output if last else "",
#                 "success": last.success if last else False,
#             }
#
#         return json.dumps({
#             "success": True,
#             "data": output,
#             "workflow": name,
#             "steps": {sid: {
#                 "success": r.success,
#                 "output": r.output[:500] if r.success else r.error,
#                 "duration": r.duration,
#             } for sid, r in ctx["steps"].items()},
#         })
#
#
#     # 步骤执行
#     def _execute_step(self, step: StepDef, ctx: Dict[str, Any]) -> Optional[StepResult]:
#         """执行单个步骤"""
#
#         # 条件判断
#         if step.condition:
#             cond_result = _resolve_template(step.condition, ctx)
#             if cond_result.lower() in ("false", "0", "", "none", "null"):
#                 return StepResult(
#                     id=step.id, success=True,
#                     output="skipped (condition not met)", duration=0.0,
#                 )
#
#         # parallel 子步骤
#         if step.children:
#             return self._execute_parallel_block(step, ctx)
#
#         # LLM 步骤
#         if step.use_llm:
#             return self._execute_llm_step(step, ctx)
#
#         # tool 步骤
#         return self._execute_tool_step(step, ctx)
#
#     def _execute_tool_step(self, step: StepDef, ctx: Dict[str, Any]) -> StepResult:
#         """执行 tool 类型步骤"""
#         from tools.registry import registry
#
#         on_progress = ctx.get("_on_progress")
#         wf_name = ctx.get("_workflow_name", "")
#
#         if on_progress:
#             on_progress("step", {"workflow_name": wf_name, "step_id": step.id,
#                                  "state": "running", "tool": step.tool})
#
#         resolved_args = _resolve_dict(step.args, ctx)
#         attempts = max(1, step.retry + 1)
#         last_error = ""
#
#         for attempt in range(attempts):
#             t0 = time.time()
#             try:
#                 result_json = registry.dispatch(step.tool, resolved_args)
#                 duration = time.time() - t0
#
#                 result = json.loads(result_json)
#                 if "error" in result:
#                     last_error = result["error"]
#                     if attempt < attempts - 1:
#                         time.sleep(1)  # 重试前等1秒
#                         continue
#                     if on_progress:
#                         on_progress("step", {"workflow_name": wf_name, "step_id": step.id,
#                                              "state": "error", "tool": step.tool})
#                     return StepResult(
#                         id=step.id, success=False,
#                         error=last_error, duration=duration,
#                     )
#
#                 output = result.get("data", result_json)
#                 if isinstance(output, dict):
#                     output = json.dumps(output, ensure_ascii=False)
#
#                 if on_progress:
#                     on_progress("step", {"workflow_name": wf_name, "step_id": step.id,
#                                          "state": "done", "tool": step.tool})
#                 return StepResult(
#                     id=step.id, success=True,
#                     output=output, duration=duration,
#                 )
#             except Exception as e:
#                 last_error = str(e)
#                 if attempt < attempts - 1:
#                     time.sleep(1)
#                     continue
#
#         if on_progress:
#             on_progress("step", {"workflow_name": wf_name, "step_id": step.id,
#                                  "state": "error", "tool": step.tool})
#         return StepResult(
#             id=step.id, success=False,
#             error=last_error, duration=time.time() - t0,
#         )
#
#     def _execute_llm_step(self, step: StepDef, ctx: Dict[str, Any]) -> StepResult:
#         """执行 LLM 类型步骤"""
#         from model_client import get_model_client
#
#         on_progress = ctx.get("_on_progress")
#         wf_name = ctx.get("_workflow_name", "")
#
#         if on_progress:
#             on_progress("step", {"workflow_name": wf_name, "step_id": step.id,
#                                  "state": "running", "tool": "llm"})
#
#         t0 = time.time()
#         try:
#             prompt = _resolve_template(step.prompt, ctx)
#
#             messages = [{"role": "user", "content": prompt}]
#
#             provider = os.getenv("JIFY_PROVIDER") or "openai"
#             model = step.model or os.getenv("JIFY_MODEL") or "deepseek-v4-pro"
#
#             client = get_model_client(provider)
#             response = client.chat(messages=messages, tool_schemas=[], model=model, stream=False)
#             content = response.content
#             duration = time.time() - t0
#
#             if on_progress:
#                 on_progress("step", {"workflow_name": wf_name, "step_id": step.id,
#                                      "state": "done", "tool": "llm"})
#             return StepResult(
#                 id=step.id, success=True,
#                 output=content, duration=duration,
#             )
#         except Exception as e:
#             if on_progress:
#                 on_progress("step", {"workflow_name": wf_name, "step_id": step.id,
#                                      "state": "error", "tool": "llm"})
#             return StepResult(
#                 id=step.id, success=False,
#                 error=str(e), duration=time.time() - t0,
#             )
#
#     def _execute_parallel_block(self, step: StepDef, ctx: Dict[str, Any]) -> StepResult:
#         """执行 parallel 块"""
#         on_progress = ctx.get("_on_progress")
#         wf_name = ctx.get("_workflow_name", "")
#
#         if on_progress:
#             on_progress("step", {"workflow_name": wf_name, "step_id": step.id,
#                                  "state": "running", "tool": "parallel"})
#
#         t0 = time.time()
#         results: Dict[str, StepResult] = {}
#         all_success = True
#
#         with ThreadPoolExecutor(max_workers=min(len(step.children), self.max_workers)) as pool:
#             futures = {pool.submit(self._execute_step, child, ctx): child for child in step.children}
#             for future in as_completed(futures):
#                 child = futures[future]
#                 try:
#                     result = future.result()
#                 except Exception as e:
#                     result = StepResult(
#                         id=child.id, success=False,
#                         error=str(e), duration=time.time() - t0,
#                     )
#                 if result:
#                     results[child.id] = result
#                     ctx["steps"][child.id] = result
#                     if not result.success:
#                         all_success = False
#
#         # 合并输出
#         combined = json.dumps({
#             sid: {"success": r.success, "output": r.output[:300] if r.success else r.error}
#             for sid, r in results.items()
#         }, ensure_ascii=False)
#
#         if on_progress:
#             state = "done" if all_success else "error"
#             on_progress("step", {"workflow_name": wf_name, "step_id": step.id,
#                                  "state": state, "tool": "parallel"})
#         return StepResult(
#             id=step.id, success=all_success,
#             output=combined, duration=time.time() - t0,
#         )
#
# if __name__ == "__main__":
#     engine = WorkflowEngine()
#     engine.discover()
#     engine.load_all()
#     engine.execute("code_audit",{"target_file":"jify.py"})
#     print(f"Loaded workflows: {list(engine._workflows.keys())}")
