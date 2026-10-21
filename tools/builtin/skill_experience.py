"""Skill 经验库 —— 漏洞挖掘思路与方法论的自增长沉淀

沉淀的是「挖洞方法论」而非「漏洞个案」：
  - 方法论层（SKILL.md）：静态、可平移，由 skill_create 管理
  - 方法库层（vul.json）：随真实审计自增长，按「方法」聚合，个案只作佐证

数据结构（v2）：
{
  "version": 2,
  "skills": {
    "security-audit": {
      "methods": [
        {
          "id": "file-upload-audit",
          "title": "任意文件上传：三查后缀可控 / 落点可执行 / 访问可达",
          "category": "file_upload",
          "applies_when": "看到 MultipartFile、上传接口、StorageService.generateKey",
          "steps": ["后缀是否透传原始文件名", "物理落点是否在 web 根", "..."],
          "pitfalls": "落点不在 web 根 → 最多存储型 XSS",
          "cases": [{"project": "litemall", "payload": "...", "evidence": "..."}],
          "confidence": 3,
          "added_at": "...",
          "updated_at": "..."
        }
      ]
    }
  }
}

存储位置：~/.jify/vul_experience/vul.json（用户级，不污染项目）
说明：这是「漏洞挖掘方法论」；「踩坑经验」另存于 ~/.jify/self_evolution/experiences.json。
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from tools.registry import register_tool
from tools.skill_paths import canonical_skill_name, resolve_skill
from event_bus import UIEvent, event_bus

EXPERIENCE_FILE_NAME = "experience.json"
SCHEMA_VERSION = 2

# 漏洞挖掘方法库（用户级单文件）。与 self_evolution 下的「踩坑经验」是两套独立数据。
VUL_DB_PATH = Path.home() / ".jify" / "vul_experience" / "vul.json"

# 有效漏洞类型（与 vuln_verify 保持一致，允许空值表示非漏洞类经验）
VALID_VULN_TYPES = {
    "rce", "sqli", "xss", "ssrf", "path_traversal",
    "deserialization", "csrf", "idor", "auth_bypass",
    "info_disclosure", "other", "attack_surface", "bypass_technique",
}

# 单个方法在 prompt 中的渲染上限
MAX_METHODS = 20


def resolve_skill_dir(skill_name: str) -> Optional[Path]:
    """解析 skill 目录，与 load_skill 共用同一套解析逻辑。"""
    return resolve_skill(skill_name)


def _slugify(text: str) -> str:
    """把标题转成稳定的方法 ID。"""
    s = re.sub(r"[^\w\s-]", "", text.strip().lower())
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s[:64] or "method"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_db() -> dict:
    """读取整个方法库，返回 {"version": int, "skills": {...}}。"""
    p = VUL_DB_PATH
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("skills"), dict):
                return data
        except (json.JSONDecodeError, IOError):
            pass
    return {"version": SCHEMA_VERSION, "skills": {}}


def _write_db(db: dict) -> None:
    VUL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    VUL_DB_PATH.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")


def _legacy_experience_paths(skill_name: str) -> List[Path]:
    """旧版按 skill 分文件的经验库位置，仅用于存量兼容读取。"""
    paths = []
    d = resolve_skill_dir(skill_name)
    if d is not None:
        paths.append(d / EXPERIENCE_FILE_NAME)
    legacy_user = Path.home() / ".jify" / "skills" / skill_name / EXPERIENCE_FILE_NAME
    if legacy_user not in paths:
        paths.append(legacy_user)
    return paths


def _legacy_entry_to_method(entry: dict) -> dict:
    """把 v1 的个案条目降级为 v2 的方法（单个 case）。"""
    title = (entry.get("approach") or entry.get("source_pattern") or "未命名方法").strip()
    case = {
        "project": (entry.get("framework_hint") or "").strip(),
        "payload": (entry.get("payload") or "").strip(),
        "evidence": (entry.get("evidence") or "").strip(),
    }
    return {
        "id": _slugify(title),
        "title": title[:120],
        "category": (entry.get("vuln_type") or "").strip(),
        "applies_when": (entry.get("source_pattern") or "").strip(),
        "steps": [s.strip() for s in (entry.get("approach") or "").split("\n") if s.strip()],
        "pitfalls": (entry.get("note") or "").strip(),
        "cases": [case] if any(case.values()) else [],
        "confidence": 1 if any(case.values()) else 0,
        "added_at": entry.get("added_at") or _now(),
        "updated_at": entry.get("added_at") or _now(),
    }


def _normalize_case(raw: dict) -> dict:
    case = {
        "project": str(raw.get("project", "")).strip(),
        "payload": str(raw.get("payload", "")).strip(),
        "evidence": str(raw.get("evidence", "")).strip(),
    }
    return case if any(case.values()) else {}


def _merge_case(method: dict, case: dict) -> bool:
    """把 case 合并进方法（同 project+payload 视为重复）。返回是否新增。"""
    if not case:
        return False
    for c in method.get("cases", []):
        if c.get("project", "").lower() == case["project"].lower() and \
           c.get("payload", "").strip() == case["payload"]:
            return False
    method.setdefault("cases", []).append(case)
    method["confidence"] = len(method["cases"])
    method["updated_at"] = _now()
    return True


def load_methods(skill_name: str) -> List[dict]:
    """读取某 skill 的方法列表（按置信度降序）。当前库 + 旧版遗留文件合并。"""
    section = _read_db()["skills"].get(skill_name)
    methods: List[dict] = []
    if isinstance(section, dict):
        if isinstance(section.get("methods"), list):
            methods = [m for m in section["methods"] if isinstance(m, dict)]
        elif isinstance(section.get("entries"), list):
            # v1 兼容
            methods = [_legacy_entry_to_method(e) for e in section["entries"] if isinstance(e, dict)]

    seen = {m.get("id") for m in methods}
    for p in _legacy_experience_paths(skill_name):
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, IOError):
            continue
        for e in data.get("entries", []):
            if not isinstance(e, dict):
                continue
            m = _legacy_entry_to_method(e)
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            methods.append(m)

    methods.sort(key=lambda m: (m.get("confidence", 0), m.get("updated_at", "")), reverse=True)
    return methods


def append_method(skill_name: str, method: dict, case: dict = None) -> dict:
    """新增或合并一个挖洞方法。

    - 同 id 已存在：只补充空字段并合并 case，不新建
    - 不存在：按传入字段新建

    Returns:
        {"success": bool, "added": bool, "merged": bool, "skipped_reason": str, "count": int, "id": str}
    """
    title = str(method.get("title", "")).strip()
    if not title:
        return {"success": False, "added": False, "merged": False,
                "skipped_reason": "缺少 title：方法必须有标题", "count": 0, "id": ""}

    method_id = _slugify(str(method.get("id") or title))
    db = _read_db()
    section = db["skills"].get(skill_name)
    if not isinstance(section, dict) or not isinstance(section.get("methods"), list):
        section = {"version": SCHEMA_VERSION, "methods": []}

    existing = next((m for m in section["methods"] if m.get("id") == method_id), None)
    case = _normalize_case(case or {})

    if existing is not None:
        # 补齐空字段，不覆盖已有内容
        filled = False
        for key in ("category", "applies_when", "pitfalls"):
            val = str(method.get(key, "")).strip()
            if val and not existing.get(key):
                existing[key] = val
                filled = True
        steps = [s.strip() for s in method.get("steps", []) if str(s).strip()]
        if steps and not existing.get("steps"):
            existing["steps"] = steps
            filled = True
        case_added = _merge_case(existing, case)
        if filled or case_added:
            existing["updated_at"] = _now()
            db["skills"][skill_name] = section
            _write_db(db)
        return {
            "success": True, "added": False, "merged": filled or case_added,
            "skipped_reason": "" if (filled or case_added) else "方法已存在且无新增信息",
            "count": len(section["methods"]), "id": method_id,
        }

    steps = [str(s).strip() for s in method.get("steps", []) if str(s).strip()]
    new_method = {
        "id": method_id,
        "title": title,
        "category": str(method.get("category", "")).strip(),
        "applies_when": str(method.get("applies_when", "")).strip(),
        "steps": steps,
        "pitfalls": str(method.get("pitfalls", "")).strip(),
        "cases": [case] if case else [],
        "confidence": 1 if case else 0,
        "added_at": _now(),
        "updated_at": _now(),
    }
    section["methods"].append(new_method)
    db["skills"][skill_name] = section
    _write_db(db)
    return {"success": True, "added": True, "merged": False, "skipped_reason": "",
            "count": len(section["methods"]), "id": method_id}


def render_for_prompt(skill_name: str, max_methods: int = MAX_METHODS) -> str:
    """把方法库渲染为可注入上下文的文本（供 load_skill 附加返回）。

    只渲染方法论骨架（何时用 / 步骤 / 反向边界），个案压成一行佐证。
    """
    methods = load_methods(skill_name)
    if not methods:
        return ""

    lines = ["", "---", "## 漏洞挖掘方法库（自增长沉淀）", ""]
    for i, m in enumerate(methods[:max_methods], 1):
        conf = m.get("confidence", 0)
        lines.append(f"### 方法 {i}: {m.get('title', '未命名')}" + (f"  （{conf} 例佐证）" if conf else ""))
        if m.get("category"):
            lines.append(f"- 类别: {m['category']}")
        if m.get("applies_when"):
            lines.append(f"- 何时适用: {m['applies_when']}")
        steps = m.get("steps") or []
        if steps:
            lines.append("- 检查步骤:")
            for j, s in enumerate(steps, 1):
                lines.append(f"  {j}) {s}")
        if m.get("pitfalls"):
            lines.append(f"- 反向边界/易误判: {m['pitfalls']}")
        for c in m.get("cases", [])[:3]:
            bits = [x for x in (c.get("project"), c.get("payload"), c.get("evidence")) if x]
            if bits:
                lines.append(f"- 佐证: {' | '.join(bits)}")
        lines.append("")

    if len(methods) > max_methods:
        lines.append(f"（另有 {len(methods) - max_methods} 个方法未展开，见 {VUL_DB_PATH}）")
    return "\n".join(lines)


# 工具入口：供 LLM 在审计收尾时主动沉淀方法
@register_tool(
    name="skill_experience_add",
    description=(
        "向某个 skill 的挖洞方法库沉淀一条「方法论」（不是个案记录）。"
        "把本次确认有效的挖洞思路抽象成可平移的方法：何时适用、检查步骤、反向边界/易误判，"
        "个案（project/payload/evidence）只作为佐证挂到 cases 上。"
        "同 id 的方法已存在时只补充空字段并追加 case，不会重复建档。"
        "下次 load_skill 时方法库会随 SKILL.md 一起加载进上下文。"
        "重要：仅当漏洞经 vuln_verify 黑盒实测确认为 confirmed，或用户已明确确认该漏洞正确时，"
        "才可调用本工具落盘；仅静态线索、not confirmed、误报、用户否定或未表态的，一律不要落盘。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill_name": {"type": "string", "description": "目标 skill 名称（如 security-audit）"},
            "title": {"type": "string", "description": "方法标题，一句话讲清「查什么/怎么判」，会据此生成稳定 id"},
            "category": {"type": "string", "description": "方法类别（如 file_upload、authz、ssrf、info_disclosure，可空）"},
            "applies_when": {"type": "string", "description": "何时适用：触发这条方法的源码特征/接口形态（可空）"},
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "检查步骤，按顺序列出可执行的判定动作（可空）",
            },
            "pitfalls": {"type": "string", "description": "反向边界/易误判：什么情况下不成立、容易判错的地方（可空）"},
            "project": {"type": "string", "description": "个案：项目/技术栈（可空，仅作佐证）"},
            "payload": {"type": "string", "description": "个案：验证 Payload（可空）"},
            "evidence": {"type": "string", "description": "个案：验证证据（可空）"},
        },
        "required": ["skill_name", "title"],
    },
    parallel_safe=False,
    requires_approval=True,
)
def skill_experience_add(
    skill_name: str,
    title: str,
    category: str = "",
    applies_when: str = "",
    steps: list = None,
    pitfalls: str = "",
    project: str = "",
    payload: str = "",
    evidence: str = "",
) -> str:
    """沉淀一个挖洞方法（可附带一条个案佐证）到 skill 方法库。"""
    event_bus.put(UIEvent("TEXT", f"* skill_experience_add ( {skill_name} )"))

    if not skill_name or not re.match(r"^[a-zA-Z0-9_.\-]+$", skill_name):
        return json.dumps({"success": False, "error": f"无效的 skill_name '{skill_name}'"})

    # 归一为规范名，避免同一 skill 因大小写/别名在方法库里分裂成多段
    _dir = resolve_skill_dir(skill_name)
    if _dir is not None:
        skill_name = canonical_skill_name(_dir)

    if category and category not in VALID_VULN_TYPES:
        return json.dumps({"success": False, "error": f"无效的 category '{category}'，可选: {sorted(VALID_VULN_TYPES)}"})

    result = append_method(
        skill_name,
        {
            "title": title,
            "category": category,
            "applies_when": applies_when,
            "steps": steps or [],
            "pitfalls": pitfalls,
        },
        {"project": project, "payload": payload, "evidence": evidence},
    )

    if not result["success"]:
        return json.dumps({"success": False, "error": result["skipped_reason"]}, ensure_ascii=False)

    if result["added"]:
        msg = f"已新建方法 '{result['id']}'（该 skill 当前 {result['count']} 个方法）"
    elif result["merged"]:
        msg = f"已合并进已有方法 '{result['id']}'（该 skill 当前 {result['count']} 个方法）"
    else:
        msg = f"方法 '{result['id']}' 已存在且无新增信息，未重复记录"

    return json.dumps({
        "success": True,
        "added": result["added"],
        "merged": result["merged"],
        "message": msg,
        "method_id": result["id"],
        "count": result["count"],
        "path": str(VUL_DB_PATH),
    }, ensure_ascii=False)
