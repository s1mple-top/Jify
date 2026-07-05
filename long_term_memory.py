# -*- coding: utf-8 -*-
"""
长期记忆模块 — 基于 Markdown 文件，按 user_id 分片。 提供gateway使用

CLI 模式不挂载。网关模式下，由 gateway.py 按需使用，
在对话结束后主动写入重要信息（不靠 LLM 自动压缩）。

存储结构:
  Memory/{user_id}.md  — 用户的长期记忆

MD 文件格式:
  # 用户记忆 - {user_id}
  ## 关键事实
  - 时间: 摘要
  ## 项目约定
  - 键: 值
"""

import re
import os
from pathlib import Path
from typing import Optional
from datetime import datetime

_MEMORY_DIR = Path(__file__).parent / "Memory"


class LongTermMemory:

    def __init__(self):
        os.makedirs(_MEMORY_DIR, exist_ok=True)

    def _path(self, user_id: str) -> Path:
        safe = re.sub(r'[<>:"/\\|?*]', '_', user_id)
        return _MEMORY_DIR / f"{safe}.md"

    def _read(self, user_id: str) -> str:
        p = self._path(user_id)
        if not p.exists():
            return f"# 用户记忆 - {user_id}\n\n"
        return p.read_text(encoding="utf-8")

    def _write(self, user_id: str, content: str):
        self._path(user_id).write_text(content, encoding="utf-8")

    def _parse_sections(self, content: str) -> dict:
        """解析 MD 文件为 {section_name: [lines]}"""
        sections = {}
        current = "header"
        sections[current] = []
        for line in content.split("\n"):
            if line.startswith("## "):
                current = line[3:].strip()
                sections[current] = []
            elif line.startswith("# ") and current == "header":
                sections[current].append(line)
            else:
                sections[current].append(line)
        return sections

    def put(self, user_id: str, section: str, key: str, value: str) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        content = self._read(user_id)
        sections = self._parse_sections(content)
        entry = f"- {now} | {key}: {value}"

        if section in sections:
            lines = sections[section]
            replaced = False
            for i, line in enumerate(lines):
                if f"| {key}:" in line:
                    lines[i] = entry
                    replaced = True
                    break
            if not replaced:
                lines.append(entry)
        else:
            sections[section] = [entry]

        new_content = "\n".join(sections["header"])
        for sec_name, sec_lines in sections.items():
            if sec_name == "header":
                continue
            new_content += f"\n## {sec_name}\n" + "\n".join(sec_lines) + "\n"
        self._write(user_id, new_content.strip() + "\n")

    def get(self, user_id: str, key: str) -> Optional[str]:
        content = self._read(user_id)
        for line in content.split("\n"):
            if f"| {key}:" in line:
                return line.split(f"| {key}:", 1)[1].strip()
        return None

    def get_section(self, user_id: str, section: str) -> str:
        sections = self._parse_sections(self._read(user_id))
        lines = sections.get(section, [])
        return "\n".join(lines).strip()

    def get_all(self, user_id: str) -> str:
        return self._read(user_id)

    def delete(self, user_id: str, key: str) -> bool:
        content = self._read(user_id)
        new_lines = []
        found = False
        for line in content.split("\n"):
            if f"| {key}:" in line and not found:
                found = True
                continue
            new_lines.append(line)
        if found:
            self._write(user_id, "\n".join(new_lines))
        return found

    def build_context(self, user_id: str, token_budget: int = 2000) -> str:
        """构建注入网关 system prompt 的用户记忆上下文"""
        content = self.get_all(user_id)
        if not content.strip():
            return ""

        char_budget = token_budget * 4
        if len(content) <= char_budget:
            return content

        sections = self._parse_sections(content)
        lines = sections.get("header", [])
        lines.append("")
        for sec_name, sec_lines in sections.items():
            if sec_name == "header":
                continue
            lines.append(f"## {sec_name}")
            kept = 0
            for line in sec_lines:
                if line.strip() and kept < 15:
                    lines.append(line)
                    kept += 1

        result = "\n".join(lines)
        return result[:char_budget]
