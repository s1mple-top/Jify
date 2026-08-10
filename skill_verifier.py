# -*- coding: utf-8 -*-
"""
Skill 验证引擎 — 旁路验证，不阻塞主 loop。

两级验证策略：
  L1（格式校验）— 适用所有 skill：可发现性（复刻 _discover_skills()）、SKILL.md 可加载、工具引用合法性、是否包含可执行步骤
  L2（回归测试）— 仅 security-audit：加载 skill → 对 benchmark 源代码执行实际分析 → 对比 recall/regression

路由规则：security-audit → L2，其他 → L1
"""

L2_SKILL_NAMES = {"security-audit"}

import json, os, re, threading, traceback
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

SKILLS_DIR = Path(os.path.expanduser("~/.jify/skills"))
VERIFIER_WHITELIST = {"read_file", "exec", "load_skill"}


def verify_skill_async(skill_name: str) -> None:
    """Fire-and-forget：spawn daemon 线程执行 skill 验证。"""
    t = threading.Thread(
        target=_verify_skill, args=(skill_name,),
        daemon=True, name=f"skill-verify-{skill_name}",
    )
    t.start()


def _verify_skill(skill_name: str) -> None:
    """验证路由：security-audit → L2，其他 → L1。"""
    if skill_name in L2_SKILL_NAMES:
        _verify_skill_l2(skill_name)
    else:
        _verify_skill_l1(skill_name)


# ═══════════════════════════════════════════════════════════
# L1 格式校验（纯 Python，不启动 subagent）
# ═══════════════════════════════════════════════════════════

_L1_TOOL_NAMES = {
    "read_file", "write_file", "patch_file", "exec", "static_analysis",
    "load_skill", "skill_create", "subagent_run", "update_todos",
    "mcp_reload", "mcp_list", "team_delegate", "team_broadcast",
    "team_delegate_parallel", "team_status", "team_add_worker", "team_remove_worker",
    "hello_world",
}

_L1_SYSTEM_COMMANDS = {
    "find", "tree", "ls", "grep", "cat", "head", "tail", "sed", "awk",
    "curl", "pip", "python", "git", "ssh", "scp", "docker", "kubectl",
    "tar", "gzip", "wget",
}


def _resolve_skill_dir(skill_name: str) -> Optional[Path]:
    """解析 skill 目录：项目 skills/ > ~/.jify/skills/ > ~/.openclaw/workspace/skills/

    复刻 _discover_skills() 的目录优先级。
    """
    candidates = [
        Path(__file__).parent / "skills" / skill_name,
        SKILLS_DIR / skill_name,
        Path(os.path.expanduser("~/.openclaw/workspace/skills")) / skill_name,
    ]
    for p in candidates:
        if p.exists() and p.is_dir():
            return p
    # 都不存在时，fallback 到 ~/.jify/skills/（skill_create 写入位置）
    return candidates[1]


def _try_discover_name(skill_dir: Path) -> tuple:
    """复刻 _discover_skills() 的名称发现逻辑。

    返回 (discovered_name, source, detail_dict)
    source: "_meta.json" | "skill.json" | "SKILL.md frontmatter" | ""
    """
    detail = {}
    name = ""

    # ① _meta.json → slug
    meta_path = skill_dir / "_meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            name = meta.get("slug", "")
            detail["from__meta_json"] = {"slug": name}
            if name:
                detail["from__meta_json"]["description"] = meta.get("description", "")
                return name, "_meta.json", detail
        except (json.JSONDecodeError, IOError):
            detail["from__meta_json"] = {"error": "parse failed"}

    # ② skill.json → name
    json_path = skill_dir / "skill.json"
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            name = data.get("name", "")
            detail["from_skill_json"] = {"name": name}
            if name:
                detail["from_skill_json"]["description"] = data.get("description", "")
                return name, "skill.json", detail
        except (json.JSONDecodeError, IOError):
            detail["from_skill_json"] = {"error": "parse failed"}

    # ③ SKILL.md YAML frontmatter → name（兼容 OpenClaw 格式）
    skill_md = skill_dir / "SKILL.md"
    if skill_md.exists():
        try:
            content = skill_md.read_text(encoding="utf-8")
            fm_match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
            if fm_match:
                frontmatter = fm_match.group(1)
                nm = re.search(r'^name:\s*(.+)', frontmatter, re.MULTILINE)
                if nm:
                    name = nm.group(1).strip()
                    desc = re.search(r'^description:\s*(.+)', frontmatter, re.MULTILINE)
                    detail["from_frontmatter"] = {
                        "name": name,
                        "description": desc.group(1).strip() if desc else "",
                    }
                    return name, "SKILL.md frontmatter", detail
            detail["from_frontmatter"] = {"error": "no valid YAML frontmatter with name"}
        except (IOError, UnicodeDecodeError):
            detail["from_frontmatter"] = {"error": "read failed"}

    return "", "", detail


def _verify_skill_l1(skill_name: str) -> None:
    """L1 校验：按 Jify 实际加载逻辑检查 skill 是否可被正确发现和加载。

    三类检查（复刻 config/system_prompt.py::_discover_skills()）：
      1. 可发现性 — _meta.json/skill.json/SKILL.md frontmatter 至少一个提供 name
      2. SKILL.md 可加载 — load_skill 工具能否读取
      3. 内容质量 — 工具引用合法性 + 是否包含可执行步骤
    """
    checks = {}
    skill_dir = _resolve_skill_dir(skill_name)

    # ── 1. 可发现性：复刻 _discover_skills() 逻辑 ──
    discovered_name, name_source, discover_detail = _try_discover_name(skill_dir)
    discoverable = bool(discovered_name)

    checks["discoverable"] = {
        "pass": discoverable,
        "source": name_source,
        "discovered_name": discovered_name,
        "matches_dir_name": discovered_name == skill_name,
        "detail": discover_detail,
    }
    if not discoverable:
        # 致命错误：Jify 无法发现此 skill，不会注入 system prompt
        _write_result(skill_name, {
            "verified_at": datetime.now().isoformat(), "level": "L1",
            "status": "error",
            "checks": checks,
        })
        _log(skill_name, "L1 error: skill not discoverable")
        return

    if discovered_name != skill_name:
        checks["discoverable"]["warning"] = (
            f"发现名称 '{discovered_name}' 与目录名 '{skill_name}' 不一致"
        )

    # ── 2. SKILL.md 可加载（复刻 load_skill 工具读取路径）──
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        checks["loadable"] = {
            "pass": False,
            "detail": f"SKILL.md 不存在: {skill_md}",
        }
    else:
        try:
            content = skill_md.read_text(encoding="utf-8")
            checks["loadable"] = {
                "pass": True,
                "detail": f"{len(content)} chars, {len(content.splitlines())} lines",
            }
        except Exception as e:
            checks["loadable"] = {"pass": False, "detail": str(e)}
            _write_result(skill_name, {
                "verified_at": datetime.now().isoformat(), "level": "L1",
                "status": "error",
                "checks": checks,
            })
            _log(skill_name, f"L1 error: SKILL.md unreadable: {e}")
            return

    # ── 3. 工具引用合法性 ──
    tool_names_found = {tn for tn in _L1_TOOL_NAMES if tn in content}

    code_blocks = re.findall(r"```[\s\S]*?```", content)
    code_text = "\n".join(code_blocks)
    refs = set(re.findall(r"`([a-z_][a-z0-9_]{1,30})`", code_text))
    unknown_refs = [r for r in refs if r not in _L1_TOOL_NAMES and r not in _L1_SYSTEM_COMMANDS]

    checks["tool_references"] = {
        "pass": len(unknown_refs) == 0,
        "detail": {"recognized_tools": sorted(tool_names_found), "unknown_refs": unknown_refs[:10]},
    }

    # ── 4. 可执行步骤检测 ──
    has_numbered = bool(re.search(r"(?:^|\n)\s*\d+[.\)]\s", content))
    has_step_markup = bool(re.search(r"(?:步骤|Step|环节)\s*\d", content, re.IGNORECASE))
    has_tool_call = bool(re.search(
        r"(?:read_file|write_file|patch_file|exec|static_analysis|load_skill)\(", content
    ))
    steps_section = any(kw in content for kw in ["执行步骤", "## 执行", "## 操作", "工具使用顺序"])

    actionable = (has_numbered or has_step_markup or has_tool_call) and (steps_section or tool_names_found)

    checks["actionable_steps"] = {
        "pass": actionable,
        "detail": {
            "has_numbered_steps": has_numbered, "has_step_markup": has_step_markup,
            "has_tool_call": has_tool_call, "has_steps_section": steps_section,
            "tools_found_count": len(tool_names_found),
        },
    }

    all_pass = all(c.get("pass", False) for c in checks.values())
    status = "passed" if all_pass else "warning"

    _write_result(skill_name, {
        "verified_at": datetime.now().isoformat(), "level": "L1",
        "status": status, "checks": checks,
    })
    _log(skill_name, f"L1 {status}: " + json.dumps(
        {k: v.get("pass") for k, v in checks.items()}, ensure_ascii=False, default=_json_default
    ))


# ═══════════════════════════════════════════════════════════
# L2 回归测试（仅 security-audit）
# ═══════════════════════════════════════════════════════════

def _verify_skill_l2(skill_name: str) -> None:
    try:
        benchmarks = _find_benchmarks(skill_name)
        if not benchmarks:
            _write_result(skill_name, {
                "verified_at": datetime.now().isoformat(), "level": "L2",
                "status": "skipped",
                "reason": f"未找到 benchmark 目录。请在 ~/.jify/skills/{skill_name}/benchmarks/ 下放置测试用例。",
            })
            return

        results = []
        overall = {"total": len(benchmarks), "passed": 0, "regressed": 0}
        for bm in benchmarks:
            r = _run_benchmark(skill_name, bm)
            results.append(r)
            if r.get("regression", False):
                overall["regressed"] += 1
            else:
                overall["passed"] += 1

        _write_result(skill_name, {
            "verified_at": datetime.now().isoformat(), "level": "L2",
            "status": "completed", "summary": overall, "benchmarks": results,
        })
    except Exception as e:
        _write_result(skill_name, {
            "verified_at": datetime.now().isoformat(), "level": "L2",
            "status": "error", "error": str(e),
        })
        _log(skill_name, f"L2 error: {e}\n{traceback.format_exc()}")


def _find_benchmarks(skill_name: str) -> List[Path]:
    bm_dir = SKILLS_DIR / skill_name / "benchmarks"
    if not bm_dir.exists():
        return []
    return sorted([d for d in bm_dir.iterdir() if d.is_dir() and (d / "expected.json").exists()])


def _run_benchmark(skill_name: str, benchmark_dir: Path) -> dict:
    bm_id = benchmark_dir.name
    ep = benchmark_dir / "expected.json"
    try:
        with open(ep, encoding="utf-8") as f:
            expected = json.load(f)
    except Exception as e:
        return {"benchmark": bm_id, "error": f"无法读取 expected.json: {e}", "regression": False}

    expected_vulns = expected.get("expected_vulns", [])
    if not expected_vulns:
        return {"benchmark": bm_id, "description": expected.get("description", ""),
                "error": "expected.json 中未定义 expected_vulns", "regression": False}

    source_path = benchmark_dir / "source"

    try:
        from tools.registry import registry
        schemas = []
        for name in VERIFIER_WHITELIST:
            tool = registry.get(name)
            if tool:
                schemas.append({"type": "function", "function": {
                    "name": tool.name, "description": tool.description,
                    "parameters": tool.parameters,
                }})
    except Exception as e:
        return {"benchmark": bm_id, "error": f"构建 tool schemas 失败: {e}", "regression": False}

    try:
        from agent_loop import AgentConfig
        from model_client import get_model_client
        from subagent import SubagentRunner
        config = AgentConfig.load_from_yaml()
        model_client = get_model_client(
            provider=config.provider, api_key=config.api_key or None,
            base_url=config.base_url or None,
        )
    except Exception as e:
        return {"benchmark": bm_id, "error": f"创建 model_client 失败: {e}", "regression": False}

    system_prompt = f"""你是一个 skill 验证器。任务：加载 skill 后，按其描述的安全审计流程对测试代码进行实际分析，并对发现的漏洞做本地部署 + 黑盒实测验证。

操作步骤：
1. 使用 load_skill('{skill_name}') 加载 skill，仔细阅读其中的漏洞挖掘方法论、执行步骤和黑盒验证手法
2. 读取 {source_path} 下的所有源代码文件（使用 read_file）
3. 严格按照 skill 中描述的分析流程和方法论，逐文件进行安全审计，找出候选漏洞
4. 对每个候选漏洞，尝试本地部署目标（如 uvicorn/main.py/docker），用真实请求（curl 等）做黑盒验证，确认漏洞真实存在
5. 只输出已确认（confirmed）的漏洞；无法部署或验证不通过的，在 verified 字段标注状态

输出格式（仅输出 JSON，放在 ```json 代码块中）：
```json
{{"findings": [
  {{"type": "漏洞类型", "description": "具体漏洞描述", "file": "文件名", "line_hint": "大致行号或位置", "verified": "confirmed|not confirmed|out of scope", "evidence": "验证证据：请求/响应/时序/回连摘要"}}
]}}
```

注意：如果未发现任何问题或全部验证不通过，输出 {{"findings": []}}"""

    runner = SubagentRunner(model_client, config)
    try:
        raw = runner.run(
            task=f"加载 skill '{skill_name}'，然后对 {source_path} 下的所有源代码进行安全审计，严格按照 skill 中的方法论找出所有安全漏洞。",
            system_prompt=system_prompt, whitelist_schemas=schemas,
            whitelist_names=VERIFIER_WHITELIST, max_iterations=10,
        )
    except Exception as e:
        return {"benchmark": bm_id, "error": f"subagent 执行失败: {e}", "regression": False}

    findings = _parse_findings(raw)
    comparison = _compare_findings(findings, expected_vulns)
    return {"benchmark": bm_id, "description": expected.get("description", ""),
            "findings": findings, "expected": expected_vulns, **comparison}


def _parse_findings(raw: str) -> List[dict]:
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    if not m:
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return []
    try:
        return json.loads(m.group(1) if m.lastindex else m.group()).get("findings", [])
    except (json.JSONDecodeError, AttributeError):
        return []


def _compare_findings(findings: List[dict], expected_vulns: List[dict]) -> dict:
    if not expected_vulns:
        return {"recall": "N/A", "found_count": len(findings), "expected_count": 0,
                "missed": [], "new_findings": findings, "regression": False}

    found_expected, missed = [], []
    for ev in expected_vulns:
        (found_expected if _fuzzy_match(ev, findings) else missed).append(ev)

    new_findings = [f for f in findings if not _fuzzy_match_reverse(f, expected_vulns)]
    recall = len(found_expected) / len(expected_vulns)

    return {
        "recall": round(recall, 2), "found_count": len(found_expected),
        "expected_count": len(expected_vulns),
        "missed": [f"{m.get('type', '')}: {m.get('description', '')}" for m in missed],
        "new_findings": [f"{n.get('type', '')}: {n.get('description', '')}" for n in new_findings],
        "regression": recall < 1.0,
    }


_TYPE_KEYWORDS = {
    "sql": {"sql"}, "注入": {"sql", "注入"}, "injection": {"sql", "注入"},
    "xss": {"xss"}, "跨站": {"xss"},
    "csrf": {"csrf"}, "ssrf": {"ssrf"},
    "rce": {"rce", "命令", "执行"}, "命令": {"rce", "命令", "执行"},
    "路径穿越": {"path", "路径", "遍历"}, "path traversal": {"path", "路径", "遍历"},
    "idor": {"idor", "越权"}, "越权": {"idor", "越权"},
    "反序列化": {"反序列化", "deserialization"}, "deserialization": {"反序列化", "deserialization"},
}


def _extract_type_keywords(type_str: str) -> set:
    lower = type_str.lower()
    kw = set()
    for k, vs in _TYPE_KEYWORDS.items():
        if k in lower:
            kw |= vs
    return kw


def _calc_desc_similarity(exp: str, actual: str) -> float:
    ew, aw = set(exp.split()), set(actual.split())
    ws = len(ew & aw) / len(ew) if ew else 0.0

    def _ng(s, n=2):
        return {s[i:i+n] for i in range(len(s)-n+1)}
    eg, ag = _ng(exp), _ng(actual)
    ns = len(eg & ag) / len(eg) if eg else 0.0
    return max(ws, ns)


def _fuzzy_match(expected: dict, findings: List[dict]) -> bool:
    et = expected.get("type", "").lower()
    ed = expected.get("description", "").lower()
    ef = expected.get("file_hint", "").lower()
    etk = _extract_type_keywords(et)

    for f in findings:
        ft = f.get("type", "").lower()
        fd = f.get("description", "").lower()
        ff = f.get("file", "").lower()
        ftk = _extract_type_keywords(ft)

        if etk and ftk:
            if not (etk & ftk):
                if not (et in ft or ft in et):
                    continue
        elif et and ft:
            if et not in ft and ft not in et:
                continue

        fm = ef in ff if ef else True
        do = _calc_desc_similarity(ed, fd)

        if do >= 0.3:
            return True
        if fm and do >= 0.1:
            return True
    return False


def _fuzzy_match_reverse(finding: dict, expected_list: List[dict]) -> bool:
    return any(_fuzzy_match(ev, [finding]) for ev in expected_list)


def _json_default(obj):
    if isinstance(obj, set):
        return sorted(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _write_result(skill_name: str, result: dict) -> None:
    rp = SKILLS_DIR / skill_name / "verification.json"
    try:
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    except OSError:
        pass


def _log(skill_name: str, message: str) -> None:
    ld = Path.home() / ".jify" / "log"
    try:
        ld.mkdir(parents=True, exist_ok=True)
        with open(ld / "log.txt", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] [skill_verifier:{skill_name}] {message}\n")
    except OSError:
        pass
