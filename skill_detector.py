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
    steps: str = ""                 # 可执行的工作流步骤描述
    tools_used: List[str] = field(default_factory=list)
    frequency: int = 0
    created_at: str = ""


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
    DETECTION_PROMPT = """你是一个编程工作流分析器。你的任务是检测用户反复执行的**可复用操作流程**，
并将其抽象为可被直接执行的 skill。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
什么是 skill（必须全部满足）：
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. **可执行** — 有明确的 Step 1 → Step 2 → ... 步骤序列，换一个人照着做也能完成
2. **跨场景复用** — 不是一次性的，类似场景出现 2 次以上
3. **涉及工具调用** — 至少 2 个工具参与
4. **有固定范式** — 工具使用顺序和参数模式相对固定

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
什么**不是** skill（绝对不要提取）：
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 纯对话/答疑（无工具调用链）
- 一次性操作（只改了一个文件，只跑了一条命令）
- 临时调试/修 bug（每次场景不同，不具备复用模板）
- 话题摘要（「用户和我讨论了 UI 压缩」→ 这不是 skill）
- 泛泛的最佳实践（「应该先读文件再改」→ 太泛，没有具体步骤）
- 没有具体执行步骤的概念（描述了一个领域但写不出怎么做）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
好 skill vs 坏 skill 对比：
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 坏: name="llm-compress-summary", description="用户讨论了 LLM 压缩上下文的方法"
   → 这是一个**话题摘要**，没有可执行步骤

 好: name="code-review-flow", description="代码审查工作流：读取变更 → 静态分析 → 跑测试 → 输出报告"
   → 有明确的 4 步操作序列，可以直接执行

 好: name="deploy-staging", description="部署到测试环境：运行测试 → 构建 → 推送 → SSH 重启服务"
   → 4 步操作链条，部署场景通用

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
      "name": "skill 英文 slug（如 code-review-flow）",
      "description": "一句话描述这个 skill 做什么（中文）",
      "trigger_pattern": "什么场景触发（如：用户要求审查代码）",
      "steps": "分步执行指令，每步标注工具名和做什么",
      "tools_used": ["read_file", "static_analysis", "exec"]
    }}
  ]
}}

关键要求：
- name 用英文 slug，必须反映**做什么**而不是**讨论了什么**
- steps 是核心——必须写清每个步骤具体做什么、用什么工具、产出什么
- 如果写不出明确可执行的步骤 → 说明这不是一个合格的 skill → 不要输出
- 如果没有发现合格的工作流 → 输出 {{"patterns": []}}"""

    def detect(self) -> List[Dict[str, Any]]:
        """从累计任务中检测可执行的工作流模式

        Returns:
            检测到的 skill 建议列表
        """
        if not self.summarizer or len(self._pending_tasks) < 5:
            return []

        # 只取有工具调用的任务（纯对话暂时不构成 skill）
        tool_tasks = [t for t in self._pending_tasks[-30:] if t.get("tools")]
        if len(tool_tasks) < 3:
            return []

        # 构建任务摘要：含用户意图 + agent 执行成果
        tasks_lines = []
        for t in tool_tasks[-20:]:
            tools_str = " → ".join(t["tools"]) if t["tools"] else "无"
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

        # 已有建議 + 拒绝名单（让 LLM 不再输出同名建议）
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

            # 检查是否已存在
            existing_names = {s.name for s in self.suggestions}
            if name in existing_names:
                for s in self.suggestions:
                    if s.name == name:
                        s.frequency += 1
                        s.steps = p.get("steps", s.steps)
                        s.tools_used = p.get("tools_used", s.tools_used)
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
