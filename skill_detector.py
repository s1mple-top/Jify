# -*- coding: utf-8 -*-
"""
技能模式检测模块

检测用户反复执行的任务模式，生成 skill 创建建议。

检测维度：
  重复性任务、固定工作流、偏好工具链

输出：~/.jify/self_evolution/skills/{user_id}.json（per-user 隔离）
"""

import os
import re
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime


@dataclass
class SkillSuggestion:
    """技能建议数据结构"""
    name: str = ""
    description: str = ""
    trigger_pattern: str = ""
    steps: str = ""                 # methodology → 思维框架/检查清单；workflow → 可执行步骤
    tools_used: List[str] = field(default_factory=list)
    frequency: int = 0
    created_at: str = ""
    skill_type: str = "workflow"    # "methodology"(思路，可横移) | "workflow"(经验，绑定具体栈)


class SkillDetector:
    """检测重复任务模式，生成技能创建建议"""

    STORAGE_DIR = os.path.join(os.path.expanduser("~"), ".jify", "self_evolution", "skills")
    SKILLS_DIR = os.path.join(os.path.expanduser("~"), ".jify", "skills")
    MIN_FREQUENCY = 3  # 相同的范式至少出现 3 次才建议生成技能

    def __init__(self, summarizer=None, user_id: str = "cli_user"):
        """
        Args:
            summarizer: Callable(prompt: str) -> str，LLM 调用接口
            user_id: 用户唯一标识，CLI 模式默认 "cli_user"
        """
        self.summarizer = summarizer
        self.suggestions: List[SkillSuggestion] = []
        self._pending_tasks: List[Dict] = []  # 累積待分析的任務
        self._approved_names: set = set()     # 用户已审批通过，已落盘
        self._rejected_names: set = set()     # 用户已拒绝，防止重复弹窗
        safe_id = re.sub(r'[<>:"/\\\\|?*]', '_', user_id)
        self._storage_path = os.path.join(self.STORAGE_DIR, f"{safe_id}.json")
        os.makedirs(self.STORAGE_DIR, exist_ok=True)
        self._load()


    # 持久化
    def _ensure_dir(self):
        os.makedirs(os.path.dirname(self._storage_path), exist_ok=True)

    def _load(self):
        if not os.path.exists(self._storage_path):
            return
        try:
            with open(self._storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                items = data
            else:
                items = data.get("suggestions", [])
                self._approved_names = set(data.get("approved", []))
                self._rejected_names = set(data.get("rejected", []))
            for item in items:
                s = SkillSuggestion(
                    name=item.get("name", ""),
                    description=item.get("description", ""),
                    trigger_pattern=item.get("trigger_pattern", ""),
                    steps=item.get("steps", ""),
                    tools_used=item.get("tools_used", []),
                    frequency=item.get("frequency", 0),
                    created_at=item.get("created_at", ""),
                    skill_type=item.get("skill_type", "workflow"),
                )
                self.suggestions.append(s)
        except (json.JSONDecodeError, IOError):
            pass

    def _save(self):
        self._ensure_dir()
        suggestions_data = []
        for s in self.suggestions:
            suggestions_data.append({
                "name": s.name,
                "description": s.description,
                "trigger_pattern": s.trigger_pattern,
                "steps": s.steps,
                "tools_used": s.tools_used,
                "frequency": s.frequency,
                "created_at": s.created_at,
                "skill_type": s.skill_type,
            })
        data = {
            "suggestions": suggestions_data,
            "approved": sorted(self._approved_names),
            "rejected": sorted(self._rejected_names),
        }
        with open(self._storage_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


    # 任务累积（供引擎调用）
    def add_task(self, user_msg: str, assistant_msg: str, tools_used: List[str], outcome: str = ""):
        """添加一轮对话的任务信息用于后续批量分析

        Args:
            user_msg: 用户输入
            assistant_msg: agent 回复
            tools_used: 使用的工具列表
            outcome: 本轮实际产出摘要（如生成了什么文件、修了什么、部署到了哪）
        """
        self._pending_tasks.append({
            "user": user_msg[:200],
            "assistant": assistant_msg[:5000],
            "tools": tools_used,
            "outcome": outcome[:500] if outcome else "",
            "time": datetime.now().isoformat(),
        })
        if len(self._pending_tasks) > 50:
            self._pending_tasks = self._pending_tasks[-50:]


    # 技能检测
    # methodology 专用于漏洞挖掘类任务，workflow 用于非漏洞挖掘的可复用行为范式
    DETECTION_PROMPT = """你是一个工作流与方法论分析器。你的任务是从用户与 Jify 的协作历史中，检测可沉淀的 skill。
根据任务的性质，分为以下两种类型，**请按各自的标准分别判断**：

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Skill 的两种类型
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. 方法论型（methodology）— **仅用于漏洞挖掘 / 安全审计类任务**
   定义：一套可横移的漏洞挖掘思考框架 / 审计思路 / 检查清单，不绑定具体技术栈。

   什么时候用 methodology：
   - 任务涉及漏洞挖掘、渗透测试、代码审计、逆向分析、安全评估
   - 用户反复表现出某种可套用到不同目标的漏洞挖掘或安全审计思路

   可横移测试：把这套思路套到另一个技术栈（Java → Android、Python → Node、Web → 客户端/二进制），
   核心逻辑是否依然成立？成立 → methodology。

   好：name="component-vuln-hunting"，description="组件/依赖漏洞挖掘思路：枚举组件与版本 → 匹配公开 CVE → 无公开漏洞时审计危险 sink → 构造 PoC 验证"
      → Java 框架适用，横移到 Android 组件、npm 包、Go module 同样成立
   好：name="injection-verify-loop"，description="注入类漏洞验证思路：无害 payload 探测回显/时序 → 确认注入点 → 判定漏洞类型 → 无副作用 PoC 确认"
      → 不绑定具体语言与框架

   输出要求：steps 写「思维框架 / 检查清单」，**禁止写具体命令、具体 API、具体文件路径**。

2. 流程型（workflow）— **用于非漏洞挖掘类的可复用行为范式**
   定义：用户反复执行的某种可复用操作范式，不一定是绑定具体技术栈，关键在于「行为模式可复用」。

   什么时候用 workflow：
   - 任务**不涉及**漏洞挖掘 / 安全审计
   - 用户反复执行某类操作，且行为模式具备复用价值
   - 不限定具体领域 —— 可以是代码管理（如同步远程仓库）、部署流程、文件批处理、项目初始化、数据处理流水线、文档生成、测试自动化等任何反复出现的操作范式
   - 关键判断标准：**用户是否多次做了"同一类事情"**，而不在于用了什么工具或栈

   好：name="sync-github-remote"，description="同步 GitHub 远程代码：pull 最新代码 → 解决冲突 → 运行测试 → push"
   好：name="batch-file-refactor"，description="批量文件重构：扫描目标文件 → 逐个应用修改 → 验证语法 → 统一提交"
   好：name="project-scaffold"，description="项目脚手架初始化：创建目录结构 → 安装依赖 → 配置 lint/format → 初始化 git"
   好：name="data-pipeline-run"，description="数据处理流水线：拉取数据源 → 清洗格式化 → 运行分析脚本 → 输出报告 → 归档结果"

   注意：以上只是示例，workflow 应覆盖**任何领域**的可复用行为范式，不要局限于代码管理类操作。

   输出要求：steps 写分步指令，标注每一步用什么工具、做什么操作。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
判断流程（务必遵守）：
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. 先判断任务是否属于「漏洞挖掘 / 安全审计」类
   - 是 → 尝试抽象为 methodology（可横移的漏洞挖掘思路），抽象不出则不产出
   - 否 → 判断是否有「可复用的行为范式」，有则产出为 workflow
2. 同一组任务历史中，两种类型可以同时产出（如果既有漏洞挖掘任务又有其他可复用范式）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
什么**不是** skill（绝对不要提取）：
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 纯对话/答疑（无工具调用链）
- 一次性操作（只改了一个文件，只跑了一条命令）
- 临时调试/修 bug（每次场景不同，不具备复用模板）
- 话题摘要（「用户和我讨论了 UI 压缩」→ 这不是 skill）
- 空泛口号（「应该先读文件再改」→ 既没有可复用思维框架，也没有具体步骤）
- 绑定单一 API 的扫描技巧（除非能抽象出可横移的审计思路）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
任务历史（用户意图 + 执行成果）：
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{tasks}

已有技能建议（避免重复）：
{existing}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
输出格式（JSON only，不要其他文字）：
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{{
  "patterns": [
    {{
      "skill_type": "methodology 或 workflow",
      "name": "skill 英文 slug（如 component-vuln-hunting）",
      "description": "一句话描述这个 skill 做什么（中文）",
      "trigger_pattern": "什么场景触发（如：用户要求审计某个组件/框架的安全性）",
      "steps": "methodology → 思维框架/检查清单；workflow → 分步指令（每步标注工具名和做什么）",
      "tools_used": ["read_file", "static_analysis", "exec"]
    }}
  ]
}}

关键要求：
- skill_type 必须是 "methodology" 或 "workflow"
- methodology **仅限漏洞挖掘 / 安全审计场景**，steps 写可横移的思维框架，禁止写具体 API/命令/路径
- workflow 用于非漏洞挖掘的可复用行为范式，steps 写具体分步指令
- name 用英文 slug，必须反映**做什么**而不是**讨论了什么**
- 如果没有发现合格的 skill → 输出 {{"patterns": []}}"""

    def detect(self) -> List[Dict[str, Any]]:
        """从累计任务中检测可执行的工作流模式

        Returns:
            检测到的 skill 建议列表
        """
        if not self.summarizer:
            return []

        tasks_lines = []
        for t in self._pending_tasks[-20:]:
            tools_str = " → ".join(t["tools"]) if t.get("tools") else "无"
            outcome = t.get("outcome", "")
            if outcome:
                outcome = outcome[:200]
            else:
                outcome = ""
            tasks_lines.append(
                f"- 用户意图: {t['user'][:200]}\n"
                f"  agent 操作: {tools_str}\n"
                f"  执行成果: {outcome or '（无记录）'}"
            )
        tasks_text = "\n".join(tasks_lines)

        # 已有建议 + 拒绝名单（让 LLM 不再输出同名建议）
        existing = json.dumps(
            [{"name": s.name, "description": s.description} for s in self.suggestions],
            ensure_ascii=False
        )
        rejected_text = ""
        if self._rejected_names:
            rejected_text = f"\n用户已拒绝以下 skill（不要再建议）：\n{json.dumps(sorted(self._rejected_names), ensure_ascii=False)}\n"

        prompt = self.DETECTION_PROMPT.format(
            tasks=tasks_text,
            existing=existing + rejected_text,
        )
        # 给大模型检测重复格式，沉淀模版
        try:
            result = self.summarizer(prompt)
            result = result.strip()
            if result.startswith("```"):
                lines = result.split("\n")
                result = "\n".join(lines[1:-1]) if len(lines) > 2 else result
            parsed = json.loads(result)
            return parsed.get("patterns", [])
        except (json.JSONDecodeError, Exception):
            return []

    def apply(self, patterns: List[Dict[str, Any]]) -> bool:
        """应用检测到的技能模式

        Returns:
            True 表示有新增建议
        """
        if not patterns:
            return False

        changed = False
        now = datetime.now().isoformat()

        for p in patterns:
            name = p.get("name", "")
            if not name:
                continue
            skill_type = p.get("skill_type", "workflow")
            if skill_type not in ("methodology", "workflow"):
                skill_type = "workflow"

            # 检查是否已存在
            existing_names = {s.name for s in self.suggestions}
            if name in existing_names:
                for s in self.suggestions:
                    if s.name == name:
                        s.frequency += 1
                        s.steps = p.get("steps", s.steps)
                        s.tools_used = p.get("tools_used", s.tools_used)
                        s.skill_type = skill_type
                        changed = True
                        # 不在此处落盘，改为入待审队列，由 CLI 下一轮对话前弹窗确认
                        break
            else:
                self.suggestions.append(SkillSuggestion(
                    name=name,
                    description=p.get("description", ""),
                    trigger_pattern=p.get("trigger_pattern", ""),
                    steps=p.get("steps", ""),
                    tools_used=p.get("tools_used", []),
                    frequency=1,
                    created_at=now,
                    skill_type=skill_type,
                ))
                changed = True

        if changed:
            self._save()
        return changed


    # Skill 落盘（自进化闭环：检测达标 → 自动生成 SKILL.md）
    @staticmethod
    def _build_skill_md_content(suggestion: SkillSuggestion) -> str:
        """将 SkillSuggestion 转为 SKILL.md 内容"""
        tools_list = "\n".join(f"- {t}" for t in suggestion.tools_used) if suggestion.tools_used else "- (无)"
        return (
            "---\n"
            f"name: {suggestion.name}\n"
            f"description: {suggestion.description}\n"
            "---\n"
            "## 触发场景\n"
            f"{suggestion.trigger_pattern or '(自动检测，待补充)'}\n\n"
            "## 执行步骤\n"
            f"{suggestion.steps or '(自动检测，待补充)'}\n\n"
            "## 使用工具\n"
            f"{tools_list}\n\n"
            "---\n"
            f"*此 Skill 由 Jify 自进化机制自动生成，基于 {suggestion.frequency} 次重复模式检测。*\n"
        )

    def _write_skill_to_disk(self, suggestion: SkillSuggestion) -> bool:
        """将 SkillSuggestion 落盘为 ~/.jify/skills/{name}/SKILL.md

        仅当文件不存在时才创建，避免覆盖用户手动编辑。
        """
        skill_dir = Path(self.SKILLS_DIR) / suggestion.name
        skill_md = skill_dir / "SKILL.md"

        # 已存在则跳过（用户可能已手动编辑）
        if skill_md.exists():
            return False

        try:
            skill_dir.mkdir(parents=True, exist_ok=True)

            # 写入 SKILL.md
            content = self._build_skill_md_content(suggestion)
            skill_md.write_text(content, encoding="utf-8")

            # 写入 skill.json（供 _get_skills() 发现）
            skill_json = skill_dir / "skill.json"
            skill_json.write_text(
                # 写清楚skill的描述以及使用场景，避免skill过多模型选择紊乱，所以附带使用场景
                json.dumps(
                    {"name": suggestion.name, "description": suggestion.description + suggestion.trigger_pattern},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            return True
        except OSError:
            return False

    def get_actionable_suggestions(self) -> List[SkillSuggestion]:
        return [s for s in self.suggestions if s.frequency >= self.MIN_FREQUENCY]


    def get_pending_approval(self) -> List[SkillSuggestion]:
        """返回 frequency >= MIN_FREQUENCY 且未被审批/拒绝的建议"""
        return [
            s for s in self.suggestions
            if s.frequency >= self.MIN_FREQUENCY
            and s.name not in self._approved_names
            and s.name not in self._rejected_names
        ]

    def approve(self, name: str) -> bool:
        """审批通过：落盘 SKILL.md 并加入 approved 名单"""
        if name in self._approved_names:
            return False
        self._approved_names.add(name)
        for s in self.suggestions:
            if s.name == name:
                self._write_skill_to_disk(s)
                break
        self._save()
        return True

    def reject(self, name: str) -> bool:
        """拒绝：加入 rejected 名单，防止重复弹窗"""
        if name in self._rejected_names:
            return False
        self._rejected_names.add(name)
        self._save()
        return True
