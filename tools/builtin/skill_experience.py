# -*- coding: utf-8 -*-
"""Skill 经验库 — 自增长实战经验沉淀

将 skill 拆成两层：
  - 方法论层（SKILL.md）：静态、可横移，由 skill_create 管理
  - 经验库层（experience.json）：随真实审计自增长，增量追加

经验库解决三个结构性空白：
  1. 单次高价值发现（不依赖 skill_detector 的 frequency 触发）
  2. 增量追加粒度（append 而非全量重写 SKILL.md）
  3. 验证结果落点（vuln_verify confirmed 后由 LLM 显式回填）

存储位置：~/.jify/skills/{name}/experience.json（用户级，不污染项目）
读取位置：优先 ~/.jify/skills，回退到项目自带 skill 目录。
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from tools.registry import register_tool
from event_bus import UIEvent, event_bus

EXPERIENCE_FILE_NAME = "experience.json"
SCHEMA_VERSION = 1

# 去重主键字段：同一 skill 下，vuln_type + source_pattern 相同视为重复条目
_DEDUP_KEYS = ("vuln_type", "source_pattern")

# 有效漏洞类型（与 vuln_verify 保持一致，允许空值表示非漏洞类经验）
VALID_VULN_TYPES = {
    "rce", "sqli", "xss", "ssrf", "path_traversal",
    "deserialization", "csrf", "idor", "auth_bypass",
    "info_disclosure", "other", "attack_surface", "bypass_technique",
}


def resolve_skill_dir(skill_name: str) -> Optional[Path]:
    """解析 skill 目录，优先级与 load_skill 保持一致：local > ~/.jify/skills > OpenClaw。"""
    project_dir = Path(__file__).parent.parent.parent
    candidates = [
        project_dir / "skills" / skill_name,
        Path.home() / ".jify" / "skills" / skill_name,
        Path.home() / ".openclaw" / "workspace" / "skills" / skill_name,
    ]
    for p in candidates:
        if p.exists() and p.is_dir():
            return p
    return None


def _experience_write_path(skill_name: str) -> Path:
    """经验库写入路径（用户级，固定）。"""
    return Path.home() / ".jify" / "skills" / skill_name / EXPERIENCE_FILE_NAME


def _experience_read_paths(skill_name: str) -> List[Path]:
    """经验库读取候选路径（写入点优先，回退项目自带）。"""
    paths = [_experience_write_path(skill_name)]
    d = resolve_skill_dir(skill_name)
    if d is not None:
        p = d / EXPERIENCE_FILE_NAME
        if p not in paths:
            paths.append(p)
    return paths


def load_entries(skill_name: str) -> List[dict]:
    """读取某 skill 的经验条目（合并所有候选路径，去重）。"""
    seen = set()
    entries: List[dict] = []
    for p in _experience_read_paths(skill_name):
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, IOError):
            continue
        for e in data.get("entries", []):
            if not isinstance(e, dict):
                continue
            key = _dedup_key(e)
            if key in seen:
                continue
            seen.add(key)
            entries.append(e)
    entries.sort(key=lambda e: e.get("added_at", ""), reverse=True)
    return entries


def _dedup_key(entry: dict) -> tuple:
    return tuple(str(entry.get(k, "")).strip().lower() for k in _DEDUP_KEYS)


def _load_full(skill_name: str) -> dict:
    """读取完整 JSON（含 version 字段），不存在则返回空结构。"""
    p = _experience_write_path(skill_name)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("entries"), list):
                return data
        except (json.JSONDecodeError, IOError):
            pass
    return {"version": SCHEMA_VERSION, "entries": []}


def append_entry(skill_name: str, entry: dict) -> dict:
    """增量追加一条经验，去重后落盘到用户级经验库。

    Returns:
        {"success": bool, "added": bool, "skipped_reason": str, "count": int}
    """
    normalized = {
        "vuln_type": str(entry.get("vuln_type", "")).strip(),
        "source_pattern": str(entry.get("source_pattern", "")).strip(),
        "payload": str(entry.get("payload", "")).strip(),
        "evidence": str(entry.get("evidence", "")).strip(),
        "framework_hint": str(entry.get("framework_hint", "")).strip(),
        "approach": str(entry.get("approach", "")).strip(),
        "note": str(entry.get("note", "")).strip(),
        "added_at": entry.get("added_at") or datetime.now().isoformat(timespec="seconds"),
    }

    # 至少有一个可沉淀字段，否则视为空条目
    meaningful = any(normalized[k] for k in
                     ("source_pattern", "payload", "evidence", "approach", "note"))
    if not meaningful:
        return {"success": False, "added": False, "skipped_reason": "空条目：至少需要 source_pattern/payload/evidence/note 之一", "count": 0}

    data = _load_full(skill_name)
    new_key = _dedup_key(normalized)
    if new_key[0] or new_key[1]:
        existing_keys = {_dedup_key(e) for e in data["entries"]}
        if new_key in existing_keys:
            return {"success": True, "added": False, "skipped_reason": "重复条目（vuln_type + source_pattern 已存在）", "count": len(data["entries"])}

    data["entries"].append(normalized)

    p = _experience_write_path(skill_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"success": True, "added": True, "skipped_reason": "", "count": len(data["entries"])}


def render_for_prompt(skill_name: str, max_entries: int = 50) -> str:
    """把经验库渲染为可注入上下文的文本（供 load_skill 附加返回）。"""
    entries = load_entries(skill_name)
    if not entries:
        return ""
    lines = ["", "---", "## 实战经验库（自增长沉淀）", ""]
    for i, e in enumerate(entries[:max_entries], 1):
        lines.append(f"### 经验 {i}")
        if e.get("vuln_type"):
            lines.append(f"- 类型: {e['vuln_type']}")
        if e.get("source_pattern"):
            lines.append(f"- 源码特征: {e['source_pattern']}")
        if e.get("payload"):
            lines.append(f"- Payload: {e['payload']}")
        if e.get("evidence"):
            lines.append(f"- 验证证据: {e['evidence']}")
        if e.get("framework_hint"):
            lines.append(f"- 框架/栈提示: {e['framework_hint']}")
        if e.get("approach"):
            lines.append(f"- 挖洞思路: {e['approach']}")
        if e.get("note"):
            lines.append(f"- 备注: {e['note']}")
        lines.append("")
    return "\n".join(lines)


# 工具入口：供 LLM 在审计收尾时主动沉淀经验
@register_tool(
    name="skill_experience_add",
    description=(
        "向某个 skill 的实战经验库追加一条经验与挖洞思路（自增长沉淀，不覆盖 SKILL.md 方法论）。"
        "用于把单次审计/挖洞中确认的高价值发现、绕过技巧、攻击面等结构化沉淀下来，"
        "下次 load_skill 时经验库会随方法论一起加载进上下文。"
        "重要：仅当漏洞经 vuln_verify 黑盒实测确认为 confirmed，或用户已明确确认该漏洞正确时，"
        "才可调用本工具落盘经验与思路；仅静态线索、not confirmed、误报、用户否定或未表态的，一律不要落盘。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill_name": {"type": "string", "description": "目标 skill 名称（如 security-audit）"},
            "vuln_type": {"type": "string", "description": "漏洞/经验类型（如 sqli、ssrf、bypass_technique、attack_surface，可空）"},
            "source_pattern": {"type": "string", "description": "源码特征/根因（如 f-string 直接拼接 SQL，参数来自用户输入）"},
            "payload": {"type": "string", "description": "验证 Payload（如 1' AND '1'='1）"},
            "evidence": {"type": "string", "description": "验证证据（如布尔盲注响应差异、命令回显）"},
            "framework_hint": {"type": "string", "description": "框架/技术栈提示（如 FastAPI + 原生 sqlite3）"},
            "approach": {"type": "string", "description": "本次挖洞思路/定位方法/绕过技巧（如何定位到这个漏洞、用了什么探测手法）"},
            "note": {"type": "string", "description": "通用备注（兜底字段，非漏洞类经验可用）"},
        },
        "required": ["skill_name"],
    },
    parallel_safe=False,
    requires_approval=True,
)
def skill_experience_add(
    skill_name: str,
    vuln_type: str = "",
    source_pattern: str = "",
    payload: str = "",
    evidence: str = "",
    framework_hint: str = "",
    approach: str = "",
    note: str = "",
) -> str:
    """追加一条经验到 skill 经验库。"""
    event_bus.put(UIEvent("TEXT", f"* skill_experience_add ( {skill_name} )"))

    if not skill_name or not re.match(r"^[a-zA-Z0-9_.\-]+$", skill_name):
        return json.dumps({"success": False, "error": f"无效的 skill_name '{skill_name}'"})

    if vuln_type and vuln_type not in VALID_VULN_TYPES:
        return json.dumps({"success": False, "error": f"无效的 vuln_type '{vuln_type}'，可选: {sorted(VALID_VULN_TYPES)}"})

    result = append_entry(skill_name, {
        "vuln_type": vuln_type,
        "source_pattern": source_pattern,
        "payload": payload,
        "evidence": evidence,
        "framework_hint": framework_hint,
        "approach": approach,
        "note": note,
    })

    if not result["success"]:
        return json.dumps({"success": False, "error": result["skipped_reason"]}, ensure_ascii=False)
    if not result["added"]:
        return json.dumps({"success": True, "added": False, "message": result["skipped_reason"], "count": result["count"]}, ensure_ascii=False)

    return json.dumps({
        "success": True,
        "added": True,
        "message": f"已追加经验到 '{skill_name}' 经验库（当前 {result['count']} 条）",
        "path": str(_experience_write_path(skill_name)),
    }, ensure_ascii=False)
