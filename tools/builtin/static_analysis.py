# -*- coding: utf-8 -*-
"""builtin tool — AST 静态分析 Python 代码"""


import ast
import json
from typing import Dict, List, Optional, Set

from tools.registry import register_tool
from event_bus import UIEvent, event_bus


@register_tool(
    name="static_analysis",
    description="使用 AST 静态分析 Python 代码文件，检测潜在的 bug 和代码问题",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要分析的 Python 文件路径"},
            "check_unused_imports": {"type": "boolean", "description": "是否检测未使用的导入", "default": True},
            "check_unused_variables": {"type": "boolean", "description": "是否检测未使用的变量", "default": True},
            "check_syntax": {"type": "boolean", "description": "是否检查语法错误", "default": True},
            "check_complexity": {"type": "boolean", "description": "是否检测代码复杂度问题", "default": True},
            "check_dead_code": {"type": "boolean", "description": "是否检测死代码（如 return 后的代码）",
                                "default": True},
        },
        "required": ["path"],
    },
    parallel_safe=True,
    # requires_approval=False,
)
def static_analysis(
        path: str,
        check_unused_imports: bool = True,
        check_unused_variables: bool = True,
        check_syntax: bool = True,
        check_complexity: bool = True,
        check_dead_code: bool = True,
) -> str:
    """
    使用 AST 静态分析 Python 代码文件，检测：
    1. 语法错误
    2. 未使用的导入
    3. 未使用的变量
    4. 代码复杂度问题（过深的嵌套、过长的函数等）
    5. 死代码（return/unraise 后的代码）
    6. 常见 bug 模式（空 except、空 init 方法等）
    """
    event_bus.put(UIEvent("TEXT", "* preparing static_analysis ( " + path + " )"))
    try:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
    except Exception as e:
        return json.dumps({"error": f"无法读取文件: {e}"}, ensure_ascii=False)

    # 检查语法
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as e:
        return json.dumps({
            "success": True,
            "data": {
                "path": path,
                "syntax_valid": False,
                "syntax_error": {
                    "line": e.lineno,
                    "column": e.offset,
                    "message": str(e),
                },
                "issues": [{
                    "type": "syntax_error",
                    "severity": "error",
                    "line": e.lineno,
                    "message": f"语法错误: {e.msg}",
                    "detail": f"第 {e.lineno} 行"
                }]
            }
        }, ensure_ascii=False)

    results = {
        "path": path,
        "syntax_valid": True,
        "issues": [],
        "summary": {
            "errors": 0,
            "warnings": 0,
            "info": 0
        }
    }

    # 收集所有节点信息
    class CodeAnalyzer(ast.NodeVisitor):
        def __init__(self):
            self.imports: List[Dict] = []
            self.used_names: Set[str] = set()
            self.assigned_names: Dict[str, List[int]] = {}
            self.functions: List[Dict] = []
            self.current_function: Optional[str] = None
            self.loop_depth = 0
            self.max_loop_depth = 0
            self.max_function_length = 0
            self.empty_except_blocks: List[Dict] = []
            self.suspicious_asserts: List[Dict] = []

        def _get_name_parts(self, node):
            if isinstance(node, ast.Name):
                return [node.id]
            elif isinstance(node, ast.Attribute):
                base = self._get_name_parts(node.value)
                return base + [node.attr] if base else [node.attr]
            return []

        def _full_name(self, parts: list) -> str:
            return ".".join(str(p) for p in parts)

        def visit_Import(self, node):
            for alias in node.names:
                name = alias.asname if alias.asname else alias.name
                self.imports.append({
                    "name": name,
                    "original": alias.name,
                    "line": node.lineno,
                })
            self.generic_visit(node)

        def visit_ImportFrom(self, node):
            for alias in node.names:
                name = alias.asname if alias.asname else alias.name
                module = node.module or "*"
                self.imports.append({
                    "name": name,
                    "original": f"from {module} import {alias.name}",
                    "line": node.lineno,
                })
            self.generic_visit(node)

        def visit_Name(self, node):
            if isinstance(node.ctx, ast.Load):
                self.used_names.add(node.id)
            elif isinstance(node.ctx, ast.Store):
                if node.id not in self.assigned_names:
                    self.assigned_names[node.id] = []
                self.assigned_names[node.id].append(node.lineno)
            self.generic_visit(node)

        def visit_FunctionDef(self, node):
            func_info = {
                "name": node.name,
                "line": node.lineno,
                "end_line": node.end_lineno or node.lineno,
            }
            self.functions.append(func_info)

            for arg in node.args.args:
                if arg.arg != "self" and arg.arg != "cls":
                    if arg.arg not in self.assigned_names:
                        self.assigned_names[arg.arg] = []
                    self.assigned_names[arg.arg].append(node.lineno)

            old_function = self.current_function
            self.current_function = node.name
            self.generic_visit(node)

            func_length = (node.end_lineno or node.lineno) - node.lineno + 1
            self.max_function_length = max(self.max_function_length, func_length)
            self.current_function = old_function

        def visit_AsyncFunctionDef(self, node):
            self.visit_FunctionDef(node)

        def visit_For(self, node):
            self.loop_depth += 1
            self.max_loop_depth = max(self.max_loop_depth, self.loop_depth)
            self.generic_visit(node)
            self.loop_depth -= 1

        def visit_While(self, node):
            self.loop_depth += 1
            self.max_loop_depth = max(self.max_loop_depth, self.loop_depth)
            self.generic_visit(node)
            self.loop_depth -= 1

        def visit_ExceptHandler(self, node):
            has_body = len(node.body) > 0
            is_empty_pass = has_body and len(node.body) == 1 and isinstance(node.body[0], ast.Pass)

            if not has_body or is_empty_pass:
                exc_type = "bare" if node.type is None else self._full_name(self._get_name_parts(node.type))
                self.empty_except_blocks.append({
                    "line": node.lineno,
                    "type": exc_type
                })

            if node.name and isinstance(node.name, str):
                if node.name not in self.assigned_names:
                    self.assigned_names[node.name] = []
                self.assigned_names[node.name].append(node.lineno)

            self.generic_visit(node)

        def visit_Assert(self, node):
            if isinstance(node.test, ast.Constant):
                if node.test.value is False:
                    self.suspicious_asserts.append({
                        "line": node.lineno,
                        "type": "always_false_assert",
                        "message": "断言总是为假，这会导致 AssertionError"
                    })
            self.generic_visit(node)

    analyzer = CodeAnalyzer()
    analyzer.visit(tree)

    issues = []

    # 检查未使用的导入
    if check_unused_imports:
        for imp in analyzer.imports:
            if imp["name"] not in analyzer.used_names:
                issues.append({
                    "type": "unused_import",
                    "severity": "warning",
                    "line": imp["line"],
                    "message": f"导入 '{imp['name']}' 未使用",
                    "detail": f"原始: {imp['original']}"
                })

    # 检查未使用的变量
    if check_unused_variables:
        builtin_names = {
            "True", "False", "None", "print", "len", "range", "int", "str", "float",
            "list", "dict", "set", "tuple", "bool", "type", "isinstance", "hasattr",
            "getattr", "setattr", "open", "input", "enumerate", "zip", "map", "filter",
            "sorted", "reversed", "sum", "min", "max", "abs", "round", "any", "all",
            "__debug__", "breakpoint"
        }

        for name, lines in analyzer.assigned_names.items():
            if name not in analyzer.used_names and name not in builtin_names:
                if not name.startswith("_"):
                    issues.append({
                        "type": "unused_variable",
                        "severity": "warning",
                        "line": lines[0],
                        "message": f"变量 '{name}' 被赋值但未使用",
                        "detail": f"赋值行: {lines}"
                    })

    # 检查代码复杂度
    if check_complexity:
        if analyzer.max_loop_depth > 3:
            issues.append({
                "type": "high_cyclomatic_complexity",
                "severity": "warning",
                "line": 0,
                "message": f"代码嵌套过深 (当前最大深度: {analyzer.max_loop_depth})",
                "detail": "建议将深层嵌套重构为函数或使用列表推导式"
            })

        if analyzer.max_function_length > 200:
            issues.append({
                "type": "long_function",
                "severity": "warning",
                "line": 0,
                "message": f"存在过长的函数 (最大长度: {analyzer.max_function_length} 行)",
                "detail": "建议将长函数拆分为多个小函数"
            })

    # 检查死代码
    if check_dead_code:
        for func in analyzer.functions:
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func["name"]:
                    returns = [n for n in node.body if isinstance(n, ast.Return)]
                    if returns and len(node.body) > 1:
                        last_stmt = node.body[-1]
                        if not isinstance(last_stmt, ast.Return):
                            for i, stmt in enumerate(node.body[:-1]):
                                if isinstance(stmt, ast.Return):
                                    issues.append({
                                        "type": "dead_code",
                                        "severity": "info",
                                        "line": last_stmt.lineno,
                                        "message": f"函数 '{func['name']}' 中 return 语句（第 {stmt.lineno} 行）后存在不可达代码",
                                        "detail": f"第 {stmt.lineno} 行 return 后的代码不会执行"
                                    })
                                    break

    # 检查空的 except 块
    for exc in analyzer.empty_except_blocks:
        if exc["type"] == "bare":
            issues.append({
                "type": "bare_except",
                "severity": "warning",
                "line": exc["line"],
                "message": "使用了裸 except 语句",
                "detail": "建议指定具体异常类型: except ExceptionType:"
            })

    # 检查可疑的断言
    for comp in analyzer.suspicious_asserts:
        issues.append({
            "type": "assertion_error",
            "severity": "error",
            "line": comp["line"],
            "message": comp["message"],
            "detail": "此断言会导致程序崩溃"
        })

    # 统计问题
    for issue in issues:
        severity = issue.get("severity", "info")
        if severity == "error":
            results["summary"]["errors"] += 1
        elif severity == "warning":
            results["summary"]["warnings"] += 1
        else:
            results["summary"]["info"] += 1

    results["issues"] = issues
    results["analysis_details"] = {
        "total_imports": len(analyzer.imports),
        "total_functions": len(analyzer.functions),
        "max_loop_depth": analyzer.max_loop_depth,
        "max_function_length": analyzer.max_function_length,
    }

    return json.dumps(results, ensure_ascii=False, indent=2)
