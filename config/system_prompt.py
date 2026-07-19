"""Shared system prompt builder — single source of truth for CLI and Gateway."""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional


# Base template
# Placeholders:
#   {skills}         — formatted skill list string
#   {experience}     — experience section string (with "## 历史经验\n" prefix or empty)
#   {profile_section}— user profile/preferences section (with "## 用户偏好\n" prefix or empty)
#   {ts}             — current time string (e.g. "2026年06月23日 Tuesday")
#   {cwd}            — current working directory

TEMPLATE = """最高优先级规则：在任何对话里都禁止输出任何表情符号，除非用户在当前对话中明确要求使用，否则一律不要输出表情符号。
# 身份与使命
你是 Jify，一个交互式软件工程助手。你的唯一职责是帮助用户完成授权的软件工程任务，包括但不限于：修复 bug、添加功能、重构代码、解释代码、安全测试（仅限授权范围）、防御性安全操作、CTF 挑战及教育用途。
绝对禁止：任何用于破坏性技术、DoS 攻击、大规模定向攻击、供应链入侵、恶意规避检测的请求，必须立即拒绝。
URL 限制：除非你确认某个 URL 是用户为编程目的提供的，否则绝不为用户生成或猜测任何 URL。只能使用用户已在消息或本地文件中明确给出的 URL。

# 全局行为准则
权限遵从：
所有工具调用都受用户设定的权限模式约束。当工具需要用户批准时，必须等待用户允许。如果用户拒绝了某个工具调用，立即停止重试该工具，分析拒绝原因并调整方案。

# 上下文管理
系统会在接近上下文限制时自动压缩历史消息，因此你与用户的对话不受上下文窗口限制。

# 输出文本规范
所有非工具调用的文本都会显示给用户，用于直接沟通。
使用 GitHub-flavored Markdown 格式化，按照 CommonMark 规范 进行排版。使用等宽字体（monospace）显示
回答简洁干练，非必要不要超过300字。
重申：禁止使用表情符号，除非用户明确要求。

# 行动前审慎评估
对于本地可逆操作（如编辑文件、运行测试），可以主动执行。
对于难以撤销、影响外部共享系统、可能造成破坏或风险的操作，必须先征得用户明确同意再执行。用户确认成本极低，而误操作代价极高。

# 软件工程任务准则
意图澄清：遇到模糊指令时，结合软件工程常见任务和当前工作目录推断用户真实意图，并给出具体结果（例如：“把 methodName 改成蛇形命名” → 应定位到代码中对应方法并修改，而非仅返回字符串）。
先读后改：修改任何文件前，必须先阅读并理解其现有代码。绝不建议修改未曾读过的代码。
优先编辑而非新建：除非绝对必要，否则不要创建新文件。编辑现有文件以避免文件臃肿，更好利用现有工作。
禁止时间预估：不对任何工作或用户项目给出时间预测或预估，只关注需要完成的工作。
失败处理原则：方法失败时，先诊断原因——阅读错误信息，检查假设，尝试针对性修复。不要盲目重复，也不要因一次失败就放弃可行方案。
安全红线：你必须避免引入任何安全漏洞（命令注入、XSS、SQL 注入等 OWASP Top 10）。一旦发现不安全代码，立即修复。优先保证代码安全、可靠、正确。

# 最小改动原则：
不添加超出要求的功能、重构或“改进”。修复 bug 时不要清理周围代码；简单功能不要增加不必要的可配置性。
不为自己未修改的代码添加文档字符串、注释、类型注解。只在逻辑不明显时才添加注释。
不为不可能发生的场景添加错误处理、回退或验证。相信内部代码和框架的保证，仅在系统边界（用户输入、外部 API）进行验证。
不为一次性操作创建辅助函数、工具类或抽象。不为假设的未来需求做设计。
合适的复杂度取决于任务实际需要，几行符合现有风格的代码远胜于过早的抽象。
用户帮助：如果用户寻求帮助或希望提供反馈，告知他们可以使用 /help 获取使用指南。

# 任务跟踪（必须严格遵守）
对于任何需要 3 个或以上步骤的任务（修复 bug、实现新功能、重构、项目分析等），你必须使用 update_todos 进行规划与跟踪：
规划阶段：首先调用 update_todos 创建任务列表，所有任务初始状态为 pending。每个任务描述一个具体的、可验证的交付物或状态变更。
逐步执行与即时更新（严格遵守以下节奏）：
执行当前步骤所需的工具调用（如 read_file、patch_file、exec 等）。
立即调用 update_todos 将该步骤标记为 completed，并将下一步标记为 in_progress。
重复以上两步，直至所有任务完成。
严禁在所有步骤执行完后才批量更新任务状态，必须每完成一步立刻更新，让用户看到实时进度。
最终总结：所有任务完成后，一次性输出文本总结。
示例——用户要求“给 cli.py 添加日志功能”：
① update_todos: [☐ 读取 cli.py, ☐ 添加日志代码, ☐ 验证修改]
② read_file cli.py → 立即 update_todos: [☒ 读取 cli.py, ◐ 添加日志代码, ☐ 验证修改]
③ patch_file 添加日志 → 立即 update_todos: [☒ 读取 cli.py, ☒ 添加日志代码, ◐ 验证修改]
④ exec 验证 → 立即 update_todos: [☒, ☒, ☒] → 输出文本总结
# 工具使用规范
专用工具优先：只要存在对应专用工具，就绝不使用 exec 来替代。
读取文件 → 必须用 read_file，禁止用 cat、head、tail、sed
编辑文件 → 必须用 patch_file，禁止用 sed、awk
创建文件 → 必须用 write_file，禁止用 cat heredoc 或 echo 重定向
绝对禁止通过 exec 执行任何命令或脚本来修改代码。
并行调用：鼓励在单个响应中同时调用多个工具。如果多个工具调用之间没有依赖关系，必须并行执行以提高效率。只有存在先后依赖时才按顺序调用。
回退原则：仅在极少数没有专用工具可用的场景下，才允许使用 exec 执行命令，但绝不能用于代码变更。
# 语气与风格
极度简洁：直奔主题，避免任何冗余。先尝试最简单的方法，不绕圈子。
直接行动：省略填充词、前言、不必要的过渡。不重复用户的话，直接执行任务或给出结论。
文本输出只保留必要内容：
1、需要用户做出决策的问题
2、关键节点的状态更新
3、导致计划变更的错误或障碍
4、句子原则：能用一句话说清，绝不用三句。始终使用简短直接的句子。
工具调用前文本：不要使用冒号结尾（例如“Let me read the file:”是错误的，应写成“Let me read the file.”并以句号结尾）。
输出效率强制要求
牢记：你的核心魅力在于绝对简洁和高效。每次回复前，思考“这句话是否可以删除？”——可删则删，只保留用户必须知道的信息。直接给出答案或行动，不要解释为什么，除非用户要求。
---
## 拥有的skill
{skills}
## 历史经验
{experience}
## 用户偏好
{profile_section}
## 当前时间
{ts}
## 当前目录
{cwd}
---
请按照以上设定进行思考和回答，使用工具来完成任务。"""


def _discover_skills() -> list[dict[str, str]]:
    """遍历 skills 目录，返回原始 skill 列表。

    Returns:
        list[dict[str, str]]: [{"skill_name": "description"}, ...]
    """
    skills: list[dict[str, str]] = []

    # 1. 本地 skills 目录
    skills_dir = Path(__file__).parent.parent / "skills"
    if skills_dir.exists() and skills_dir.is_dir():
        for skill_path in skills_dir.iterdir():
            if not skill_path.is_dir():
                continue
            meta_json_path = skill_path / "_meta.json"
            skill_json_path = skill_path / "skill.json"
            name, description = "", ""

            if meta_json_path.exists():
                try:
                    meta_data = json.loads(meta_json_path.read_text(encoding="utf-8"))
                    name = meta_data.get("slug", "")
                    description = meta_data.get("description", "")
                except (json.JSONDecodeError, IOError):
                    pass

            if (not name or not description) and skill_json_path.exists():
                try:
                    skill_data = json.loads(skill_json_path.read_text(encoding="utf-8"))
                    name = skill_data.get("name", "") or name
                    description = skill_data.get("description", "") or description
                except (json.JSONDecodeError, IOError):
                    continue

            if name:
                skills.append({name: description})

    # 2. ~/.jify/skills（用户级 skill）
    jify_skills_dir = Path(os.path.expanduser("~/.jify/skills"))
    if jify_skills_dir.exists() and jify_skills_dir.is_dir():
        for skill_path in jify_skills_dir.iterdir():
            if not skill_path.is_dir():
                continue
            meta_json_path = skill_path / "_meta.json"
            skill_json_path = skill_path / "skill.json"
            name, description = "", ""

            if meta_json_path.exists():
                try:
                    meta_data = json.loads(meta_json_path.read_text(encoding="utf-8"))
                    name = meta_data.get("slug", "")
                    description = meta_data.get("description", "")
                except (json.JSONDecodeError, IOError):
                    pass

            if (not name or not description) and skill_json_path.exists():
                try:
                    skill_data = json.loads(skill_json_path.read_text(encoding="utf-8"))
                    name = skill_data.get("name", "") or name
                    description = skill_data.get("description", "") or description
                except (json.JSONDecodeError, IOError):
                    continue

            if name:
                skills.append({name: description})

    # 3. 兼容 OpenClaw 的 skill path
    openclaw_skills_dir = Path(os.path.expanduser("~/.openclaw/workspace/skills"))
    if openclaw_skills_dir.exists() and openclaw_skills_dir.is_dir():
        for skill_path in openclaw_skills_dir.iterdir():
            if not skill_path.is_dir():
                continue
            meta_json_path = skill_path / "_meta.json"
            skill_json_path = skill_path / "skill.json"
            name, description = "", ""

            if meta_json_path.exists():
                try:
                    meta_data = json.loads(meta_json_path.read_text(encoding="utf-8"))
                    name = meta_data.get("slug", "")
                    description = meta_data.get("description", "")
                except (json.JSONDecodeError, IOError):
                    pass

            if not name and skill_json_path.exists():
                try:
                    skill_data = json.loads(skill_json_path.read_text(encoding="utf-8"))
                    name = skill_data.get("name", "")
                    description = skill_data.get("description", "")
                except (json.JSONDecodeError, IOError):
                    pass

            # 回退：解析 SKILL.md 的 YAML frontmatter
            if not name or not description:
                skill_md_path = skill_path / "SKILL.md"
                if not skill_md_path.exists():
                    continue
                try:
                    with open(skill_md_path, 'r', encoding='utf-8') as f:
                        lines = [next(f, '') for _ in range(20)]
                    content = ''.join(lines)
                    # 提取 YAML frontmatter 块: "---\nname: xxx\ndescription: yyy\n---..."
                    fm_match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
                    if fm_match:
                        frontmatter = fm_match.group(1)
                        name_match = re.search(r'^name:\s*(.+)', frontmatter, re.MULTILINE)
                        if name_match:
                            name = name_match.group(1).strip()
                            desc_match = re.search(r'^description:\s*(.+)', frontmatter, re.MULTILINE)
                            description = desc_match.group(1).strip() if desc_match else ""
                except (IOError, UnicodeDecodeError):
                    continue

            if name:
                skills.append({name: description})

    return skills


def _get_skills() -> str:
    """格式化 skill 列表用于 prompt 注入。

    格式: "- name: description" 每行一个。
    """
    lines = [f"- {name}: {desc}" for skill in _discover_skills() for name, desc in skill.items()]
    return "\n".join(lines)


class SystemPromptBuilder:
    """单一入口：构建 Jify 的完整 system prompt。

    CLI 和 Gateway 统一使用此类，消除两处重复的 prompt 拼装逻辑。
    """

    def __init__(self, user_id: str = "", evolution_engine=None):
        self._user_id = user_id
        self._evolution = evolution_engine

    @property
    def skills(self) -> str:
        """已格式化的 skill 列表字符串。"""
        if not hasattr(self, '_skills_cache'):
            self._skills_cache = _get_skills()
        return self._skills_cache

    @skills.setter
    def skills(self, value: str) -> None:
        """允许外部覆盖 skill 列表（仅用于兼容旧调用方直接赋值）。"""
        self._skills_cache = value

    def _get_experience_section(self) -> str:
        """从 ExperienceExtractor 获取经验段落。"""
        try:
            from experience_extractor import ExperienceExtractor
            ee = ExperienceExtractor()
            section = ee.build_prompt_section()
            if section:
                return section + "\n"
        except Exception:
            pass
        return ""

    def _get_profile_section(self) -> str:
        """获取用户偏好段落。

        优先使用 SelfEvolutionEngine，回退到 UserProfileExtractor。
        """
        # 优先使用自进化引擎的画像
        if self._evolution is not None:
            try:
                section = self._evolution.get_profile_section()
                if section:
                    return section + "\n"
            except Exception:
                pass

        # 回退到 UserProfileExtractor
        if self._user_id:
            try:
                from user_profile import UserProfileExtractor
                up = UserProfileExtractor(user_id=self._user_id)
                section = up.build_prompt_section()
                if section:
                    return section + "\n"
            except Exception:
                pass

        return ""

    def build(self) -> str:
        """构建完整的 system prompt。"""
        now = datetime.now()
        ts = now.strftime("%Y年%m月%d日 %A")
        cwd = os.getcwd()

        prompt = TEMPLATE.format(
            skills=self.skills,
            experience=self._get_experience_section(),
            profile_section=self._get_profile_section(),
            ts=ts,
            cwd=cwd,
        )
        return prompt
