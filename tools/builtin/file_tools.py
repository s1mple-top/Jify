# -*- coding: utf-8 -*-
"""builtin tool — read_file / write_file"""


from pathlib import Path

import agent_loop
from tools.registry import register_tool
from tools.symbol_index import build_symbol_index
# from ChatConsole import UIEvent, console


@register_tool(
    name="read_file",
    description="Read contents of a file",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to read"},
            "offset": {"type": "integer", "description": "Line number to start reading from (0-based)", "default": 0},
            "limit": {"type": "integer", "description": "Max lines to read", "default": 5000},
        },
        "required": ["path"],
    },
    parallel_safe=True,
    # requires_approval=False,
)
def read_file(path: str, offset: int = 0, limit: int = 5000) -> str:
    """Read a text file"""
    # console.event_bus.put(UIEvent("TEXT", "* preparing read_file ( " + path + " )"))
    try:
        # 单次遍历：统计总行数 + 提取切片
        lines = []
        with open(path, "r") as f:
            for i, line in enumerate(f):
                if offset <= i < offset + limit:
                    lines.append(line)
            total_lines = i + 1

        content = "".join(lines)
        actual_read = len(lines)
        end_line = offset + actual_read

        # 精确截断提示（仅在确实有剩余内容时）
        if end_line < total_lines:
            remaining = total_lines - end_line
            content += (
                f"\n\n"
                f"[Truncated: showing lines {offset + 1}-{end_line} of {total_lines}"
                f" ({remaining} lines remaining)]\n"
                f"[To continue: read_file(path=\"{path}\", offset={end_line})]"
            )

        # 第一次读取无论是否全部读取都建立全文件的符号索引，方便模型二次调用时直接根据索引读取具体的行，精确读取，防止额外的token开销
        content += build_symbol_index(path, total_lines)

        return content
    except Exception as e:
        return f"Error reading {path}: {e}"


@register_tool(
    name="write_file",
    description="Generate a file and write content into it",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to write"},
            "content": {"type": "string", "description": "Content to write"},
        },
        "required": ["path", "content"],
    },
    parallel_safe=False,
    requires_approval=True,
)
def write_file(path: str, content: str) -> str:
    """写入文件,在本地落文件"""
    # console.event_bus.put(UIEvent("TEXT", "* preparing write_file ( " + path + " )"))
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w", encoding='utf-8') as f:
            f.write(content)

        msg = f"Written {len(content)} bytes to {path}"
        return msg
    except Exception as e:
        return f"Error writing {path}: {e}"
