# -*- coding: utf-8 -*-
"""Team Mode 工具 — 暴露给主 Agent (Leader) 调用的 team_* 工具"""

import json
import threading

from tools.registry import register_tool
from tools.call_context import get_call_context
from team import get_leader



# team_delegate — 委派任务给某个 Worker
@register_tool(
    name="team_delegate",
    description=(
        "委派任务给团队中的某个 Worker Agent 并等待结果。"
        "Worker 是预先定义角色的子智能体 (如 reviewer / fixer / generator), "
        "各有独立的 system_prompt 和工具白名单。"
        "若不指定 worker_id, 自动选择空闲 Worker。\n"
        "黑板默认按任务隔离：本任务的记录只有本次委派的参与者能看到。\n"
        "要让多个 Worker 协作，用 team_delegate_parallel 并传 share_task_id=true，"
        "或多次委派传同一个 blackboard_task_id。\n"
        "stop_scope 控制历史 stop 是否生效：global（默认）任何未消费的 stop 都算；"
        "delegation 只认本次委派提交之后写入的 stop，不会被队友早先留下的标记秒杀。"
        "两者都仍受任务作用域约束：别的并行任务的 stop 不会停到本任务。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "要委派的任务描述, 越具体越好",
            },
            "worker_id": {
                "type": "string",
                "description": "指定 Worker ID (如 'reviewer', 'fixer'), 不传则自动选择空闲 Worker",
            },
            "timeout": {
                "type": "number",
                "description": "超时秒数, 默认 300",
                "default": 300,
            },
            "stop_scope": {
                "type": "string",
                "enum": ["delegation", "global"],
                "description": (
                    "global（默认）= 任何未消费的 stop 都算，含历史遗留；"
                    "delegation = 只认本次委派提交之后写入的 stop，不被队友早先的标记误杀"
                ),
                "default": "global",
            },
            "blackboard_task_id": {
                "type": "string",
                "description": (
                    "黑板作用域 id。多次委派传同一个值即可让它们互相看到黑板记录；"
                    "不传 = 独立作用域（与其他任务互不可见）"
                ),
            },
        },
        "required": ["task"],
    },
    parallel_safe=True,
    # requires_approval=False,
    # 外层超时须覆盖内部默认 300s 等待；留余量避免外层先于内层超时
    timeout=360,
)
def team_delegate(task: str, worker_id: str = None, timeout: float = 300,
                  stop_scope: str = "global",
                  blackboard_task_id: str = None) -> str:
    leader = get_leader()
    if leader is None:
        return json.dumps({"error": "Team 模式未启动, 请先初始化 TeamOrchestrator"})
    return leader.delegate(task_content=task, worker_id=worker_id, timeout=timeout,
                           stop_scope=stop_scope,
                           blackboard_task_id=blackboard_task_id)


# team_delegate_async — 提交任务后立刻返回，不等待
@register_tool(
    name="team_delegate_async",
    description=(
        "委派任务给 Worker 后【立刻返回】，不等待它跑完。返回 task_id，"
        "随后用 team_wait_any 收集结果。\n"
        "适合多方向并行推进：一次下发多个任务给不同 worker，谁先做完先处理谁，"
        "不会被最慢的那个拖住；主 Agent 在等待期间可以继续做别的判断。\n"
        "注意：任务在后台独立运行，本工具返回不代表任务成功。\n"
        "返回里的 worker_busy=true 表示该 worker 当时还有别的任务在跑（新任务会排队）——"
        "说明你派错了对象，应换一个空闲 worker 或先等它空出来。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "要委派的任务描述, 越具体越好（每个 worker 的任务可以不同）",
            },
            "worker_id": {
                "type": "string",
                "description": "指定 Worker ID, 不传则自动选择空闲 Worker",
            },
            "stop_scope": {
                "type": "string",
                "enum": ["delegation", "global"],
                "description": (
                    "global（默认）= 任何未消费的 stop 都算；"
                    "delegation = 只认本次委派提交之后写入的 stop"
                ),
                "default": "global",
            },
            "blackboard_task_id": {
                "type": "string",
                "description": (
                    "黑板作用域 id。与其它委派传同一个值即可互相看到黑板记录；"
                    "不传 = 独立作用域"
                ),
            },
        },
        "required": ["task"],
    },
    parallel_safe=True,
    # 只做同步入队，不等待 worker，秒级返回
    timeout=30,
)
def team_delegate_async(task: str, worker_id: str = None,
                        stop_scope: str = "global",
                        blackboard_task_id: str = None) -> str:
    leader = get_leader()
    if leader is None:
        return json.dumps({"error": "Team 模式未启动, 请先初始化 TeamOrchestrator"})
    return leader.delegate_async(task_content=task, worker_id=worker_id,
                                 stop_scope=stop_scope,
                                 blackboard_task_id=blackboard_task_id)


# team_wait_any — 阻塞到任一异步任务完成（first-completed）
@register_tool(
    name="team_wait_any",
    description=(
        "等待【任一】异步任务完成（不是等全部），返回这一批已完成的 worker 结果。"
        "必须配合 team_delegate_async 使用。\n"
        "四种情况：\n"
        "- 有任务已完成 -> 立即返回该批结果，其余仍在跑的任务列在 running\n"
        "- 无任务完成 -> 最多等 timeout 秒，期间任一完成即返回（不会等满）\n"
        "- 超时仍无完成 -> completed 为空、timed_out=true。这【不是故障】，"
        "说明任务还在跑，接着再调一次 team_wait_any 继续等即可。"
        "running 里带 elapsed / iterations / max_iterations，可据此判断是在正常推进还是卡住。"
        "警告：不要因为 timed_out 就去 team_remove_worker 杀掉正在跑的 worker——"
        "那会让已经跑很久的任务白费。\n"
        "- 没有在途任务 -> 立即返回空，说明当前无待收结果，该去派新任务了\n"
        "每个任务只会被上报一次。status 取值：completed / failed / limit_hit / "
        "interrupted / stopped。\n"
        "limit_hit 表示 worker 打满迭代上限被强制停止（没做完），"
        "此时应先回收它的结果（result 字段），再移除该 worker 或换新任务，不要当成功处理。\n"
        "stopped 表示该任务收到同队成员的 stop 而中止（队友已宣告目标达成），"
        "不是它叫停了别人；成果看 stopped_by（谁拿到的，结构化字段）与 stop_reason（stop 原文，自动透传），"
        "细节再去取该发送者单独写给 leader 的 conclusion（需 team_collect），不要重试。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "timeout": {
                "type": "number",
                "description": (
                    "最长等待【秒数】（不是轮次），默认 300。"
                    "这是「最多等多久」，任务在等待期间完成就立刻返回，不会等满。"
                    "超时只返回空 completed + timed_out=true，不会中断 worker。"
                ),
                "default": 300,
            },
            "task_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "只等待这些 task_id；不传则等待所有未上报的异步任务",
            },
        },
        "required": [],
    },
    parallel_safe=True,
    # 外层超时必须大于内层 wait 的默认 300s，否则外层先超时会误报成工具超时
    timeout=360,
)
def team_wait_any(timeout: float = 300, task_ids: list = None) -> str:
    leader = get_leader()
    if leader is None:
        return json.dumps({"error": "Team 模式未启动, 请先初始化 TeamOrchestrator"})
    return leader.wait_any(task_ids=task_ids, timeout=timeout)



# team_broadcast — 广播同一任务给某个小队的全部成员
@register_tool(
    name="team_broadcast",
    description=(
        "把同一任务广播给某个小队的全部成员, 并行执行后返回汇总结果。\n"
        "全批共享同一块黑板：参与者能互相看到进展与结论，同批成员的 stop 互相生效。\n"
        "squad 必填 —— 这是队内专用工具。\n"
        "给多个独立 worker 并行派活请用 team_delegate_parallel"
        "（tasks 传相同内容、worker_ids 传目标列表），"
        "独立 worker 没有共享黑板，广播给它们没有协作意义。\n"
        "单个派活用 team_delegate / team_delegate_async。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "要广播给该小队成员的任务",
            },
            "squad": {
                "type": "string",
                "description": "目标小队名（必填）。不存在或无成员会报错并列出可用小队",
            },
            "timeout": {
                "type": "number",
                "description": "超时秒数, 默认 300",
                "default": 300,
            },
        },
        "required": ["task", "squad"],
    },
    parallel_safe=True,
    # requires_approval=False,
    # 外层超时须覆盖内部默认 300s 等待；留余量避免外层先于内层超时
    timeout=360,
)
def team_broadcast(task: str, squad: str = None, timeout: float = 300) -> str:
    leader = get_leader()
    if leader is None:
        return json.dumps({"error": "Team 模式未启动, 请先初始化 TeamOrchestrator"})
    return leader.broadcast(task_content=task, timeout=timeout, squad=squad)



# team_delegate_parallel — 并行委派多个不同任务
@register_tool(
    name="team_delegate_parallel",
    description=(
        "并行委派多个不同的任务给 Worker, 等待全部完成后返回汇总。\n"
        "若提供 worker_ids, 任务与 Worker 按顺序一一对应; "
        "否则自动分配。\n"
        "share_task_id=true 时本批任务视为同一次委派，参与者互相可见黑板记录"
        "（小队一起攻克同一个目标就用这个）；默认 false，各任务互相隔离。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "任务描述列表",
            },
            "worker_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Worker ID 列表 (与 tasks 一一对应), 不传则自动分配",
            },
            "timeout": {
                "type": "number",
                "description": "超时秒数, 默认 300",
                "default": 300,
            },
            "share_task_id": {
                "type": "boolean",
                "description": (
                    "true = 本批成员共享同一个黑板作用域，互相能看到进展与结论"
                    "（协作场景）；false（默认）= 各任务独立隔离"
                ),
                "default": False,
            },
            "stop_scope": {
                "type": "string",
                "enum": ["delegation", "global"],
                "description": (
                    "global（默认）= 任何未消费的 stop 都算；"
                    "delegation = 只认本次委派提交之后写入的 stop"
                ),
                "default": "global",
            },
        },
        "required": ["tasks"],
    },
    parallel_safe=True,
    # requires_approval=False,
    # 外层超时须覆盖内部默认 300s 等待；留余量避免外层先于内层超时
    timeout=360,
)
def team_delegate_parallel(
    tasks: list, worker_ids: list = None, timeout: float = 300,
    share_task_id: bool = False, stop_scope: str = "global",
) -> str:
    leader = get_leader()
    if leader is None:
        return json.dumps({"error": "Team 模式未启动, 请先初始化 TeamOrchestrator"})
    return leader.delegate_parallel(
        tasks=tasks, worker_ids=worker_ids, timeout=timeout,
        share_task_id=share_task_id, stop_scope=stop_scope,
    )



# team_status — 查询所有 Worker 状态
@register_tool(
    name="team_status",
    description="查询团队中所有 Worker 的状态: 是否繁忙、已完成任务数、总耗时等",
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
    parallel_safe=True,
    # requires_approval=False,
)
def team_status() -> str:
    leader = get_leader()
    if leader is None:
        return json.dumps({"error": "Team 模式未启动, 请先初始化 TeamOrchestrator"})
    return leader.status()


# team_collect — 取走 Worker 发给 leader 的上报
@register_tool(
    name="team_collect",
    description=(
        "取走所有 Worker 发给 leader 的上报（target='leader' 的黑板记录）。\n"
        "【读走即弃】：取完之后这些记录不会再出现，第二次调用返回空。\n"
        "只在确实要处理上报时调用；只是想看谁在忙、进度多少，用 team_status，"
        "它只观测不消费，并会显示待取条数 pending_reports。"
    ),
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
    parallel_safe=True,
)
def team_collect() -> str:
    leader = get_leader()
    if leader is None:
        return json.dumps({"error": "Team 模式未启动, 请先初始化 TeamOrchestrator"})
    reports = leader.collect_reports()
    payload = {"reports": reports}
    if not reports:
        payload["note"] = "没有待取的上报。"
    return json.dumps(payload, ensure_ascii=False)



# team_add_worker — 动态添加 Worker
@register_tool(
    name="team_add_worker",
    description=(
        "动态添加一个 Worker 到团队中。Worker 是预定义角色的子智能体, "
        "各有独立的 system_prompt 和工具白名单。仅完成注册: 添加后处于空闲待命, "
        "不会执行任何任务; 必须再调 team_delegate / team_delegate_parallel 下发 task 才会开始执行。\n"
        "squad 用于分队：同一 squad 的 worker 才能互相通信，也只能看到本队成员。"
        "不传则是独立 worker：不属于任何小队、没有可共享的黑板，"
        "它只能收到 leader 的通告，写的记录也只有 leader 能看到。"
        "独立 worker 与各小队可以自由混用。"
        "建队后可用 team_assign_squad 把独立 worker 拉进来，"
        "或用 team_leave_squad 把成员退回独立。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "worker_id": {
                "type": "string",
                "description": "Worker 唯一标识 (如 'reviewer', 'fixer', 'generator')",
            },
            "system_prompt": {
                "type": "string",
                "description": "Worker 的角色 system prompt, 定义其职责和行为",
            },
            "whitelist": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Worker 可用的工具白名单 (如 ['read_file', 'patch_file', 'exec'])",
            },
            "max_iterations": {
                "type": "number",
                "description": "最大迭代次数, 默认 100",
                "default": 100,
            },
            "squad": {
                "type": "string",
                "description": (
                    "小队名。传了就成为该队成员：能和同队成员通过黑板互通，"
                    "花名册也只列本队。不传=独立 worker，与任何小队都不互通。"
                    "队名可以与已有小队同名（加入该队），也可以是新名字（建新队）。"
                ),
            },
        },
        "required": ["worker_id", "system_prompt", "whitelist"],
    },
    parallel_safe=True,
    # requires_approval=False,
)
def team_add_worker(
    worker_id: str,
    system_prompt: str,
    whitelist: list,
    max_iterations: int = 100,
    squad: str = None,
) -> str:
    leader = get_leader()
    if leader is None:
        return json.dumps({"error": "Team 模式未启动, 请先初始化 TeamOrchestrator"})
    try:
        leader.add_worker(
            worker_id=worker_id,
            system_prompt=system_prompt,
            whitelist=set(whitelist),
            max_iterations=max_iterations,
            squad=squad,
        )
        where = f"（{squad} 小队）" if squad else ""
        return json.dumps({"ok": f"Worker '{worker_id}' 已添加{where}，空闲待命中（需 team_delegate 下发才开始执行）"})
    except ValueError as e:
        return json.dumps({"error": str(e)})


# team_remove_worker — 移除指定 Worker
@register_tool(
    name="team_remove_worker",
    description=(
        "从团队中移除指定 Worker 并关闭其线程。"
        "传入 worker_id 将该 Worker 关闭并从 Worker 池中移除。\n"
        "移除后该 Worker 的对话历史（探索记忆）一并丢失，重建同名 Worker 也是全新的。\n"
        "若移除的是所属小队的最后一名成员，该小队的黑板会被清空，"
        "但已经发给 leader 的 target='leader' 上报会保留，仍可用 team_collect 取走。\n"
        "队里还有成员存活时，黑板内容整体保留。\n"
        "注意：清掉 Worker 不等于发 stop —— 想让队友停手应该先写 kind='stop' 的记录，"
        "移除只释放线程资源。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "worker_id": {
                "type": "string",
                "description": "要移除的 Worker ID",
            },
        },
        "required": ["worker_id"],
    },
    parallel_safe=True,
)
def team_remove_worker(worker_id: str) -> str:
    leader = get_leader()
    if leader is None:
        return json.dumps({"error": "Team 模式未启动, 请先初始化 TeamOrchestrator"})
    try:
        leader.shutdown(worker_id=worker_id)
        return json.dumps({"ok": f"Worker '{worker_id}' 已移除并关闭"})
    except Exception as e:
        return json.dumps({"error": str(e)})


# team_assign_squad — 把独立 worker 加入小队
@register_tool(
    name="team_assign_squad",
    description=(
        "把一个 Worker 加入指定小队（或从一个队转到另一个队）。\n"
        "加入后队内互通立刻生效：它写的记录归到该队，也能读到队内记录。\n"
        "只影响后续记录：入队前的内容不会自动补读，需要时用 team_read(seq=N) 拉取。\n"
        "它之前写的记录归属不变，仍留在原来的队。"
        "Worker 的对话历史、线程都保留。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "worker_id": {
                "type": "string",
                "description": "要加入小队的 Worker ID",
            },
            "squad": {
                "type": "string",
                "description": "目标小队名，可以与已有小队同名（加入该队）或是新名字（建队）",
            },
        },
        "required": ["worker_id", "squad"],
    },
    parallel_safe=True,
)
def team_assign_squad(worker_id: str, squad: str) -> str:
    leader = get_leader()
    if leader is None:
        return json.dumps({"error": "Team 模式未启动, 请先初始化 TeamOrchestrator"})
    return json.dumps(leader.assign_squad(worker_id, squad), ensure_ascii=False)


# team_leave_squad — 把成员移出小队
@register_tool(
    name="team_leave_squad",
    description=(
        "把一个 Worker 移出小队，变回独立 worker。\n"
        "它立刻读不到队内黑板记录，也不再受队内 stop 影响；"
        "它之前写的记录留在原队，队友仍可读（写出去的话收不回）。\n"
        "Worker 本身不删除，对话历史、线程都保留。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "worker_id": {
                "type": "string",
                "description": "要移出小队的 Worker ID",
            },
        },
        "required": ["worker_id"],
    },
    parallel_safe=True,
)
def team_leave_squad(worker_id: str) -> str:
    leader = get_leader()
    if leader is None:
        return json.dumps({"error": "Team 模式未启动, 请先初始化 TeamOrchestrator"})
    return json.dumps(leader.leave_squad(worker_id), ensure_ascii=False)


# team_disband_squad — 解散小队
@register_tool(
    name="team_disband_squad",
    description=(
        "解散一个小队：所有成员变回独立 worker，队内黑板记录清空。\n"
        "成员本身不删除（对话历史、线程都保留），只是失去队归属；"
        "要连线程一起关请用 team_remove_worker。\n"
        "已发给 leader 的 target='leader' 上报会保留，仍可用 team_collect 取走。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "squad": {
                "type": "string",
                "description": "要解散的小队名",
            },
        },
        "required": ["squad"],
    },
    parallel_safe=True,
)
def team_disband_squad(squad: str) -> str:
    leader = get_leader()
    if leader is None:
        return json.dumps({"error": "Team 模式未启动, 请先初始化 TeamOrchestrator"})
    return json.dumps(leader.disband_squad(squad), ensure_ascii=False)


# team_read — 按 seq 拉取黑板记录全文
@register_tool(
    name="team_read",
    description=(
        "按 seq 读取一条黑板记录的【全文】。\n"
        "用途：上下文里注入的 progress / deadend 记录是截断预览（结尾带 "
        "seq 号），当你判断某条记录对当前任务有用时，用本工具拉全文。\n"
        "conclusion 记录本身已是全文，一般不需要再拉。\n"
        "可见性与黑板一致：跨队记录、定向给别人的记录、stop 标记都读不到。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "seq": {
                "type": "number",
                "description": "预览里给出的记录编号，如 '[队友进展 #12 | a1]' 中的 12",
            },
        },
        "required": ["seq"],
    },
    parallel_safe=True,
)
def team_read(seq: int) -> str:
    leader = get_leader()
    if leader is None:
        return json.dumps({"error": "Team 模式未启动, 请先初始化 TeamOrchestrator"})
    sender = _current_sender()
    worker = leader._workers.get(sender)
    squad = worker.squad if worker is not None else None
    task = worker._current_task_id if worker is not None else None
    msg = leader.mailbox.get_by_seq(int(seq), reader_squad=squad, reader=sender,
                                    reader_task=task)
    if msg is None:
        return json.dumps({
            "error": f"记录 #{seq} 不存在或对你不可见",
            "hint": ("跨队记录、别的任务的记录、定向给别人的记录、"
                     "stop 标记都读不到"),
        }, ensure_ascii=False)
    return json.dumps({
        "seq": msg.seq,
        "from": msg.sender,
        "kind": msg.kind,
        "text": msg.text,
        "note": "这是队友留下的参考记录，不需要回应。",
    }, ensure_ascii=False)


# ---- 团队通信 ----

def _current_sender() -> str:
    """当前调用者身份。

    team worker 跑在裸 threading.Thread 里，不继承 contextvars，
    只能靠启动时设置的线程名（TeamWorker.start 里为 f"team-worker-{id}"）判断。
    """
    name = threading.current_thread().name
    prefix = "team-worker-"
    if name.startswith(prefix):
        return name[len(prefix):]
    return "leader"


# team_message — 往共享黑板写一条记录
@register_tool(
    name="team_message",
    description=(
        "往团队共享黑板写一条记录。这是【留信息】，不是【发消息】：\n"
        "- 同队其他人会在自己的检查点（每次迭代开头、每个工具执行完之后）读到，"
        "不需要你指定收件人，也不会有人「回复」你。\n"
        "- 不要为了「确认收到」而回写；也不要来回问答，那只是在白烧模型调用。\n"
        "- 只有 leader 会看 target='leader' 的记录（你的上报入口）。\n"
        "kind 用于分类，帮助别人判断这条记录该不该采信：\n"
        "  progress   常规进展（默认）\n"
        "  conclusion 已确认的结论、走通的路径\n"
        "  deadend    死路：这个方向不通，别重复\n"
        "  stop       终止标记：目标已达成，同队其他人读到即停手收工\n"
        "写记录的原则：写清「结论 + 依据」，不要只说「有进展了」；"
        "写别人能直接用的东西，而不是你的心路历程。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "记录内容：结论 + 依据。不要只说'有进展了'",
            },
            "kind": {
                "type": "string",
                "enum": ["progress", "conclusion", "deadend", "stop"],
                "description": (
                    "progress=进展（默认）；conclusion=已确认的结论；"
                    "deadend=此路不通别重复；stop=目标已达成，同队收到即停手"
                ),
                "default": "progress",
            },
            "target": {
                "type": "string",
                "description": (
                    "默认不用传（黑板本来就是全队可见）。"
                    "只有要单独上报给 leader 时才传 'leader'。"
                    "leader 可以传小队名（如 'A'）向该队定向，配合 kind='stop' 停掉整队。"
                ),
            },
        },
        "required": ["text"],
    },
    parallel_safe=True,
)
def team_message(text: str, kind: str = "progress", target: str = None) -> str:
    leader = get_leader()
    if leader is None:
        return json.dumps({"error": "Team 模式未启动, 请先初始化 TeamOrchestrator"},
                          ensure_ascii=False)
    if kind not in ("progress", "conclusion", "deadend", "stop"):
        return json.dumps({"error": f"kind 只能是 progress / conclusion / deadend / stop，收到 '{kind}'"},
                          ensure_ascii=False)
    # 不传 target 时一律写全队可见的记录：黑板本来就是广播性质的，
    # 写作者不需要（也不应该）指定收件人 —— 指定收件人会诱导模型做「对话」。
    if target is None:
        target = "*"
    sender = _current_sender()
    # 独立 worker 没有可共享的黑板：它写的记录只有 leader 读得到。明确告知
    # 比静默落库好 —— 否则模型以为写完队友会看到，实际谁都不会读。
    # target="leader" 的上报不受此限，那本来就是这个通道的正常用法。
    if target == "*" and sender != "leader":
        with leader._lock:
            me = leader._workers.get(sender)
            is_solo = me is not None and me.squad is None
        if is_solo:
            return json.dumps({
                "error": ("你是独立 worker，不属于任何小队，没有可共享的黑板。"
                          "要让 leader 知道请写 target='leader'；"
                          "要与队友协作请先让 leader 用 team_assign_squad 把你加入小队。"),
            }, ensure_ascii=False)
    result = leader.post_message(
        sender=sender,
        text=text,
        kind=kind,
        target=target,
    )
    return json.dumps(result, ensure_ascii=False)


# team_post — 往黑板写一条不分类的简短进展（team_message 的便捷形式）
@register_tool(
    name="team_post",
    description=(
        "把一条进展写到共享黑板。等价于 team_message(kind='progress')，"
        "不需要指定收件人、不唤醒任何人、不会有人回复。\n"
        "适合记录：已确认的结论、走过的死路、待验证的猜想。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "要记录的进展，写清结论和依据",
            },
        },
        "required": ["text"],
    },
    parallel_safe=True,
)
def team_post(text: str) -> str:
    leader = get_leader()
    if leader is None:
        return json.dumps({"error": "Team 模式未启动, 请先初始化 TeamOrchestrator"},
                          ensure_ascii=False)
    sender = _current_sender()
    with leader._lock:
        me = leader._workers.get(sender)
        if me is not None and me.squad is None:
            return json.dumps({
                "error": ("你是独立 worker，不属于任何小队，没有可共享的黑板。"
                          "要让 leader 知道请用 team_message(target='leader')。"),
            }, ensure_ascii=False)
    result = leader.post_message(
        sender=sender, text=text, kind="progress", target="*",
    )
    return json.dumps(result, ensure_ascii=False)


# team_wait — 显式等待黑板有新内容
@register_tool(
    name="team_wait",
    description=(
        "阻塞等待黑板出现新内容。在你手头没有可推进的事、必须等队友进展才能"
        "继续时才用它，比反复空转或反复调工具省资源。\n"
        "返回等待期间新增的记录；超时返回空列表（不是故障，说明队友还没产出）。\n"
        "如果手头还有活，不要用它 —— 继续做事，新记录会在下一个检查点自动出现。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "timeout_ms": {
                "type": "integer",
                "description": "最长等待毫秒数，默认 60000（1 分钟），上限 600000（10 分钟）",
                "default": 60000,
            },
        },
        "required": [],
    },
    parallel_safe=False,
)
def team_wait(timeout_ms: int = 60000) -> str:
    leader = get_leader()
    if leader is None:
        return json.dumps({"error": "Team 模式未启动, 请先初始化 TeamOrchestrator"},
                          ensure_ascii=False)
    timeout_ms = max(0, min(int(timeout_ms or 0), 600000))
    sender = _current_sender()
    ctx = get_call_context()
    cancel = ctx.cancel_event if ctx is not None else None
    # 读者的 squad 决定它能收到哪些广播；leader 无 squad（None = 收全部通告）
    worker = leader._workers.get(sender)
    squad = worker.squad if worker is not None else None
    task = worker._current_task_id if worker is not None else None
    msgs = leader.mailbox.wait(sender, timeout_ms / 1000.0, cancel,
                               reader_squad=squad, reader_task=task)
    return json.dumps({
        "messages": [
            {"seq": m.seq, "from": m.sender, "kind": m.kind, "text": m.text}
            for m in msgs
        ],
        "note": "这些都是队友留下的参考记录，不需要回应。" if msgs else None,
    }, ensure_ascii=False)
