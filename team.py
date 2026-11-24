# -*- coding: utf-8 -*-
"""
团队模式 (Team Mode) — 同进程内 Leader + N 个 Worker Agent 协作

设计思路:
- Leader: 主 Agent, 通过 tool 调用把任务委派给 Worker
- Worker: 轻量级 Agent, 专注执行, 有独立的 system_prompt 和工具白名单
- 所有 Worker 复用 Leader 的 model_client, 在同一进程内线程执行

"""

import json
import queue
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

from event_bus import event_bus, UIEvent
from rich.text import Text
from subagent import SubagentRunner
from context_shed import SUMMARY_HEADER
from tools.registry import registry, _subagent_whitelist
from tools.call_context import (
    CallContext, register_killer, set_call_context, reset_call_context,
)

_output_engine: Any = None


def set_output_engine(engine: Any) -> None:
    global _output_engine
    _output_engine = engine


def get_output_engine() -> Any:
    return _output_engine


def _emit_team_update(worker_id: Optional[str], info: Optional[Dict], clear: bool = False) -> None:
    """将 team 状态投递到 event_bus，供 WebUI 订阅（CLI 仍通过 OutputEngine 状态行渲染）。

    gateway 模式下没有 OutputEngine（get_output_engine() 返回 None），此前 team
    状态会完全丢失；统一在此发射事件，让 WebSocketConsole 通过 event_bus 透传到前端。
    """
    try:
        event_bus.put(UIEvent("team_update", {
            "worker_id": worker_id,
            "info": info,
            "clear": clear,
        }))
    except Exception:
        pass



# 空闲多久算「该清理了」。阈值定得偏保守：宁可晚提示，也不要让主 Agent
# 在 Worker 只是短暂思考时误删掉有上下文的线程。
STALE_IDLE_SECONDS = 600

# 连续多少次「空返回」后熔断 team_wait_any。没有在途任务时该工具零延迟返回空，
# 模型若把它误读成「还没好，再等等」就会不停重调，几秒内烧光上下文。
# 取 2：第一次仍返回正常结构（带 idle 提示）给模型自我纠正的机会，
# 第二次起改走 error 分支，语义上不给「再等等」的解读空间。
WAIT_ANY_IDLE_LIMIT = 2

# leader 的固定身份标识。多处按这个字符串判定「谁在写」，因此它不是一个可以
# 被 worker 复用的普通名字：_current_sender() 在非 worker 线程上恒返回它，
# collect_reports / team_wait 也用它取 squad。允许重名会让 leader 的通告被
# 当成该 worker 的小队广播、让 leader 失去按队定向写入的能力。
LEADER_ID = "leader"

# create_default_team 给三个默认角色起的队名。它们需要互通（多角色审查同一段
# 代码），所以显式建队，而不是三个互不相干的独立 worker。
DEFAULT_SQUAD = "default"

# 独立 worker（squad=None）写入黑板时用的记录标签。它不能也用 None：
# 「记录 squad=None」在 _collect 里表示 leader 的通告、对所有人可见。
# 用一个不可能被用户取到的名字把两者分开：SOLO_SQUAD 的记录只有 leader 读得到，
# 其他独立 worker 也读不到 —— 独立 worker 之间没有共享通道。
SOLO_SQUAD = "__solo__"


# 数据结构
@dataclass
class TeamTask:
    """Leader 发给 Worker 的任务"""
    id: str                                    # uuid, 全局唯一
    content: str                               # 任务描述
    worker_id: Optional[str] = None            # None = 任意空闲 Worker
    status: str = "pending"                    # pending | running | completed | failed
    result: Optional[str] = None               # Worker 返回的最终文本
    error: Optional[str] = None                # 失败原因
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    # 取消链路：worker 在自己的线程里执行，拿不到 leader 的 contextvars（裸线程不继承），
    # 故必须显式传 event。每个任务独立一个，避免上一个已超时任务的取消信号被下一次
    # delegate 的 clear() 抹掉，导致它永远跑下去。
    cancel_event: threading.Event = field(default_factory=threading.Event)
    call_ctx: Any = None                       # worker 执行期间挂上的 CallContext
    # 打满 max_iterations 被动退出。与 completed 区分：打满上限意味着思路
    # 没走通，主 Agent 必须换人或换策略，不能当成功处理。
    limit_hit: bool = False
    iterations: int = 0                        # worker 实际消耗的迭代轮数
    # 已通过 team_wait_any 上报给主 Agent。防止同一个任务被等待反复返回
    # （主 Agent 每次都基于「新结果」做决策，重复上报会导致重复委派）。
    reported: bool = False
    # 由 delegate_async 提交。wait_any 只收集此类任务，避免把同步
    # delegate / delegate_parallel 的中间条目误报给主 Agent。
    async_mode: bool = False
    # 被同队成员的 stop 信号终止。与 interrupted 区分：interrupted 是外部
    # 取消（用户 Ctrl+C / 超时），stopped 是队友宣告「目标已达成」。
    # 主 Agent 看到 stopped 应去回收该队友的成果，而不是重试。
    stopped: bool = False
    # 收到 stop 时的来源描述（"谁: 说了什么"）
    stop_reason: str = ""
    # 发出 stop 的 worker_id。与 stop_reason 分开存：后者是自由文本，发送者
    # 身份只能靠解析前缀猜；这里给主 Agent 一个可直接用的结构化字段，
    # 用于判断「成果在谁手上」，进而去回收它自己的 conclusion。
    stopped_by: str = ""
    # stop 的作用域（由委派方指定）：
    #   "global"     —— 任何未消费的 stop 都会终止本任务（含历史遗留）。
    #   "delegation" —— 默认。只认本次委派提交之后写入的 stop；提交前已在
    #                   黑板上的历史 stop 对本次任务无效。用于「队友早已收工，
    #                   现在要派一个新方向的活」这种场景，避免新任务被旧标记秒杀。
    # 判定依据是 stop_seq_baseline（提交时的黑板水位）。
    # 注意：两者都仍受黑板任务作用域约束 —— 别的并行任务的 stop 不会停到本任务。
    stop_scope: str = "global"
    stop_seq_baseline: int = 0
    # 黑板作用域 id：决定「这条任务写入的记录谁能看到」。
    # 默认等于 id（各自独立，互不可见）；delegate_parallel 的 share_task_id=True
    # 时，同一批任务共用一个值，参与者之间就能读到彼此的黑板记录。
    # 与 id 分开是因为 id 必须唯一（用于 _tasks / futures / 结果归并），
    # 而「同一次委派」需要多个任务共享一个可见性标识。
    blackboard_task_id: Optional[str] = None

    @property
    def is_terminal(self) -> bool:
        """是否已进入终态（含 limit_hit / interrupted / stopped）。

        轮询侧统一用这个判据，新增终态词时只改这里，
        不会出现「某个分支忘了更新等待条件」导致 wait 卡死。
        """
        return self.status in ("completed", "failed", "limit_hit",
                               "interrupted", "stopped")


@dataclass
class WorkerDef:
    """Worker 定义"""
    worker_id: str
    system_prompt: str
    whitelist: Set[str]                        # 可用工具白名单
    max_iterations: int = 100
    current_task: Optional[str] = None         # 当前正在执行的任务 ID
    stats: Dict = field(default_factory=dict)  # {tasks_completed, total_elapsed}


# TeamBlackboard — 全队（或本小队）共享的进展黑板
#
# 设计语义（与「消息队列」的区别）：
#   - 只追加，永不删除；读者按各自游标增量读取
#   - 写入方只「留下进展」，不指定要谁看、不要求谁回应
#   - 读取由程序在 loop 的固定检查点完成，模型不需要主动查询
#   - 因此不存在「A 写给 B、B 又回给 A」的对话结构 —— 这是刻意的
#
# 为什么不保留点对点唤起：worker 之间一旦能互相叫醒，模型就会把它理解为
# 「对方在跟我说话，我得回」，进而形成自激互答（实测 0.2s 内 30+ 次模型调用）。
# 叫醒能力只保留给 leader（leader 派任务时才需要）。
@dataclass
class TeamMessage:
    """黑板上的一条记录。seq 全局单调递增，用作读取游标。

    kind 语义（读者据此决定怎么处理，而不是由写入方指定收件人）：
        "progress"   进展记录：常规产出，供队友参考
        "conclusion" 结论：已确认的事实或已走通的路径
        "deadend"    死路：此路不通，提示别人别再重复
        "stop"       终止标记：目标已达成，同队成员读到即停手
    """
    seq: int
    sender: str                    # worker_id 或 "leader"
    target: str                    # 保留字段：本黑板按 squad 天然隔离，"*" 为常态
    text: str
    kind: str                      # "progress" | "conclusion" | "deadend" | "stop"
    created_at: float = field(default_factory=time.time)
    # 发送者所属小队。广播（target="*"）时用它限定可见范围：
    # 只有同 squad 的读者能收到。三个取值语义不同：
    # - None：leader 写入的通告，全员可见
    # - SOLO_SQUAD：独立 worker 的写入，只有 leader 读得到
    # - 其他：某个小队的记录，只有同队读者可见
    # 独立 worker 与各小队可以混用，所以同一条黑板上三种记录会共存，
    # 可见性一律按记录与读者的 squad 是否匹配判定，不存在「未分组的人收到
    # A 队广播」的歧义。
    squad: Optional[str] = None
    # 这条记录属于哪一次委派。None 表示「全局」——leader 的通告、以及不归属
    # 任何任务的写入都落在这里。
    #
    # 为什么需要它：同一小队被派两个不同任务时，队员之间互相可见会把
    # 「任务X 的进展」灌进「任务Y 的上下文」。squad 只能隔小队，隔不了任务。
    # 传入 task_id 后，只有正在跑同一个任务（或读全局记录）的 worker 才看得到。
    # stop 同样受此字段约束（见 take_stop）：worker 写的 stop 只停同一批任务，
    # 只有 leader 写的（task_id=None）才全员生效。
    task_id: Optional[str] = None


class TeamMailbox:
    """append-only 黑板 + 每 worker 独立读取游标。

    设计取舍：
    - 不用「每人一个文件」：Jify 的 worker 同进程同线程池，共享一个 deque 即可，
      也无需文件锁。
    - 不用 SQLite 论坛：team 生命周期与进程相同，跨会话通信另有 agent_p2p.py，
      这里只需要「谁还没读到哪条」。
    - 一条记录只存一次，读者各推游标 —— 天然支持广播、天然不重复消费。

    读取语义：
    - read_since：取走并推进游标（读走即弃，用于「把新进展给到模型」）
    - peek_since：只看不推进（用于状态栏未读计数）
    - take_stop：单独一套水位，取走 kind="stop" 的终止标记

    这个类不再有「向某人推送/唤醒」的能力。写入方只留下记录，读取由读者
    自己的循环驱动 —— 这是避免 worker 之间自激互答的结构性保证。
    """

    def __init__(self, maxlen: int = 2000):
        self._log: Deque[TeamMessage] = deque(maxlen=maxlen)
        self._cond = threading.Condition()
        self._seq = 0
        # 每个 (worker, 黑板作用域) 已读到第几条（含）。缺失视为 0。
        # 按作用域分维度而不是「一 worker 一个游标」，否则「以任务 X 的身份读
        # 一次」会把途经的任务 Y 记录一并越过，之后再以 Y 的身份读就什么都
        # 拿不到 —— 数据还在板上，但再也不会被自动注入。
        self._cursors: Dict[Tuple[str, Optional[str]], int] = {}
        # 全局记录（task_id=None，如 leader 通告）单独一套游标：
        # 它们对所有作用域可见，若用作用域游标记录就会在每进入一个新作用域时
        # 重复投递。leader 例外，它只用一个游标（见 _collect）。
        self._gcursors: Dict[str, int] = {}
        # 最近一次被丢弃的最老 seq：deque 溢出会让不活跃的 worker 游标失效，
        # 读取时用 _floor 校正到剩余窗口起点，避免返回空却永久卡住。
        self._floor = 0
        # stop 的独立水位：与内容游标分开，因为 stop 不会被「当成内容投递」，
        # 若共用游标则「取 stop」会跳过同时到达的普通记录。
        # 键同样是 (worker, 作用域)，reason 同上；task_id=None 的 stop 用
        # (worker, None) 记录，只生效一次。缺失视为 0。
        self._stop_seen: Dict[Tuple[str, Optional[str]], int] = {}

    # 写入
    def post(self, sender: str, text: str, kind: str = "progress",
             squad: Optional[str] = None, target: str = "*",
             task_id: Optional[str] = None) -> int:
        """往黑板追加一条记录，返回 seq。

        不唤醒任何人：读者只在自己的循环检查点发现新记录。唤醒能力被刻意
        移除（见类文档），这是避免互激的唯一可靠方式。
        """
        with self._cond:
            self._seq += 1
            msg = TeamMessage(
                seq=self._seq, sender=sender, target=target, text=text, kind=kind,
                squad=squad, task_id=task_id,
            )
            if len(self._log) == self._log.maxlen:
                # 即将挤掉最老一条：把 floor 抬到新的窗口起点
                self._floor = self._log[0].seq
            self._log.append(msg)
            self._cond.notify_all()
            return msg.seq

    # 读取
    def _cursor_of(self, worker_id: str,
                   reader_task: Optional[str]) -> int:
        """该 worker 在「本作用域」与「全局」两条游标里的较小值。

        游标按 (worker, 作用域) 分开存，但同一批记录要同时按两类归属比较：
        - 全局记录（task_id=None）：进度记在 _gcursors
        - 本作用域记录：进度记在 _cursors[(worker, task)]

        这里返回较小值只用于 has_pending 的快速路径（判断「是否可能还有新
        记录」）。逐条判定由 _collect 内部按记录归属各取对应水位完成 ——
        只用一个 min 做统一跳过会把另一类的进度误当成本类的，导致重复投递
        或漏读。
        """
        own = (self._cursors.get((worker_id, reader_task), 0)
               if reader_task is not None else 0)
        glob = self._gcursors.get(worker_id, 0)
        return max(min(own, glob), self._floor)

    def _advance(self, worker_id: str, reader_task: Optional[str],
                 task_seq: int, global_seq: int) -> None:
        """按记录归属分别推进两条游标，各自只增不减。"""
        if task_seq:
            key = (worker_id, reader_task)
            if task_seq > self._cursors.get(key, 0):
                self._cursors[key] = task_seq
        if global_seq and global_seq > self._gcursors.get(worker_id, 0):
            self._gcursors[worker_id] = global_seq

    def _collect(self, worker_id: str, advance: bool,
                 reader_squad: Optional[str] = None,
                 reader_task: Optional[str] = None,
                 global_only: bool = False) -> List[TeamMessage]:
        """内部：取出该 worker 尚未读到的记录。advance 为真则推进游标。

        reader_squad 是读者所属小队，用于判断可见范围：
        - 记录 squad 为 None（leader 的通告）：对所有读者可见
        - 记录 squad 与读者相同：可见
        - 读者 squad 为 None（独立 worker）：只能收到 leader 的通告
        - 其他：不可见（跨队隔离）

        reader_task 是读者当前正在执行的作用域 id，用来把「同队」再细分一层：
        - task_id=None 的记录（全局）：任何同队读者都可见
        - task_id 与读者当前作用域相同：可见（同一轮委派内的协作）
        - 其他任务的记录：不可见。这一条防止「同队两人被派不同任务时
          互相读到对方的进展」，squad 层级挡不住这种情况。

        游标按 (worker, 作用域) 分开存，且逐条按记录归属比较对应水位：
        以任务 X 的身份读一次不会把任务 Y 的进度一起推走，Y 的记录仍会在
        以 Y 的身份读时拿到。注意游标对「扫描过但不可见」的记录同样前进
        （见循环里的 newest_* 赋值），所以入队后重跑同一个作用域 id 时，
        入队前队友在该作用域写的记录不会补投 —— 需要时用 team_read(seq) 拉。

        global_only: 只取全局记录（task_id=None），跳过所有带作用域的。
        用于「新任务开头把上轮积压丢掉」—— 那只该丢全局遗留，不该丢
        本作用域里队友先前写的协作内容。

        target 的处理：记录默认 target="*"（黑板全队可见）；写 target="leader"
        的记录只对 leader（读者 id 为 "leader"）可见 —— 这是上报通道，
        虽然物理上还在同一块黑板，语义上不向队友公开。

        kind="stop" 的记录不在这里返回：它不是「内容」，不该进入模型上下文，
        由 take_stop() 单独消费。但游标必须越过它，否则每次扫描都会重新看到。
        """
        if worker_id == "leader":
            # leader 只有一条游标（_gcursors）：它读的是「别人上报给我的」，
            # 与作用域无关，因此所有记录都拿全局水位比较、并推进全局水位。
            seen = max(self._gcursors.get(worker_id, 0), self._floor)
            picked: List[TeamMessage] = []
            newest = seen
            for msg in self._log:
                if msg.seq <= seen:
                    continue
                if msg.target == "leader" and msg.kind != "stop":
                    picked.append(msg)
                newest = msg.seq
            if advance and newest > seen:
                self._gcursors[worker_id] = newest
            return picked

        own = (self._cursors.get((worker_id, reader_task), 0)
               if reader_task is not None else 0)
        glob = self._gcursors.get(worker_id, 0)
        floor = self._floor
        picked = []
        newest_task = 0
        newest_global = 0
        for msg in self._log:
            # 两个维度分开判断，不能混用一个 is_global：
            # - squad 归属决定「谁看得见」（None=leader 通告，对所有人可见）
            # - task_id 决定「用哪条游标」（None 走 _gcursors，否则走本作用域游标）
            # 队内记录的 task_id 也可以是 None（直接 post_message 调用时无任务），
            # 把两者混为一谈会让「独立身份读过一次」把队内历史一起越过。
            no_task_scope = msg.task_id is None
            if global_only and not no_task_scope:
                continue
            # 逐条按记录归属取对应水位：无任务作用域的看 _gcursors，
            # 带作用域的看本作用域的游标。混用一个水位会导致跨作用域互相越读。
            seen = max(glob, floor) if no_task_scope else max(own, floor)
            if msg.seq <= seen:
                continue
            if msg.sender == worker_id:
                # 自己写的不用回灌；但游标仍要前进，否则每次都要重新扫过
                if no_task_scope:
                    newest_global = msg.seq
                else:
                    newest_task = msg.seq
                continue
            # 定向记录（如 target="leader"）只给指定读者
            if msg.target not in ("*", worker_id):
                if no_task_scope:
                    newest_global = msg.seq
                else:
                    newest_task = msg.seq
                continue
            # 可见性按「队名相同」判定。记录 squad 为 None 是 leader 的通告，
            # 所有人都能看到；读者 squad 为 None 是独立 worker，它不属于任何队，
            # 队内记录对它不可见 —— 它和其他独立 worker 之间也没有共享通道
            # （独立 worker 的写入带 SOLO_SQUAD 标签，同样不匹配）。
            visible = (msg.squad is None
                       or (reader_squad is not None and msg.squad == reader_squad))
            # 任务维度：只读无作用域记录与自己当前作用域的记录。
            if visible and msg.task_id is not None and msg.task_id != reader_task:
                visible = False
            if visible and msg.kind != "stop":
                picked.append(msg)
            if no_task_scope:
                newest_global = msg.seq
            else:
                newest_task = msg.seq
        if advance:
            self._advance(worker_id, reader_task, newest_task, newest_global)
        return picked

    def take_stop(self, worker_id: str,
                  reader_squad: Optional[str] = None,
                  min_seq: int = 0,
                  reader_task: Optional[str] = None) -> Optional[TeamMessage]:
        """取出最早一条未处理的 kind="stop" 记录（没有则 None）。

        用独立的 _stop_seen 水位，不动 _cursors：stop 与普通记录在同一个
        日志里，但两者的消费互不影响，否则「处理了 stop」会导致同一批到达的
        普通进展被跳过。

        水位按 (worker, 作用域) 分开存，与内容游标同理：以任务 Y 的身份
        取走 Y 的 stop，不该把任务 X 的 stop 一起越过 —— 否则 X 的收工信号
        会被永久吃掉。全局 stop（task_id=None）单独记在 (worker, None) 上，
        只生效一次。

        与 _collect 一样尊重 target：定向给别人的 stop 不该让本人停手。

        min_seq: 只认 seq 大于它的 stop（stop_scope="delegation" 的用武之地）。
        被它跳过的记录**不推进**水位 —— 否则一次「只对本次委派生效」
        的任务会把历史 stop 永久吃掉，后续全局作用域的任务再也看不到。

        reader_task: 与普通记录同样的任务作用域判定。worker 写的 stop 只停
        同一批任务，否则两个并行的无关任务会互相误杀。task_id=None 的 stop
        （leader 广播）依旧全员生效。
        """
        with self._cond:
            key = (worker_id, reader_task)
            global_key = (worker_id, None)
            seen = max(self._stop_seen.get(key, 0),
                       self._stop_seen.get(global_key, 0),
                       self._floor)
            found: Optional[TeamMessage] = None
            for msg in self._log:
                if msg.seq <= seen or msg.seq <= min_seq:
                    continue
                if msg.kind != "stop":
                    continue
                if msg.sender == worker_id:
                    continue
                if msg.target not in ("*", worker_id):
                    continue          # 定向给别人（含 worker / 队名）的 stop
                # 可见性与 _collect 一致：独立 worker（reader_squad=None）只受
                # leader 的全局 stop 约束，队内 stop 对它不生效。
                if msg.squad is not None and (
                        reader_squad is None or msg.squad != reader_squad):
                    continue
                # 任务作用域：worker 写的 stop 只停同一批任务；leader 写的
                # （task_id=None）全员生效。没有这一条时，两个并行的无关任务
                # 会互相误杀。
                if msg.task_id is not None and msg.task_id != reader_task:
                    continue
                found = msg
                break
            if found is None:
                return None
            # 同一批里的多条 stop 只需处理一次，重复取出没有意义。
            # 按记录归属写回对应水位：全局 stop 记在 (worker, None)，
            # 作用域 stop 记在 (worker, 该作用域)。
            if found.task_id is None:
                self._stop_seen[global_key] = found.seq
            else:
                self._stop_seen[key] = found.seq
            return found

    def read_since(self, worker_id: str,
                   reader_squad: Optional[str] = None,
                   reader_task: Optional[str] = None,
                   global_only: bool = False) -> List[TeamMessage]:
        """取出并推进游标（读走即弃）。"""
        with self._cond:
            return self._collect(worker_id, advance=True, reader_squad=reader_squad,
                                 reader_task=reader_task, global_only=global_only)

    def peek_since(self, worker_id: str,
                   reader_squad: Optional[str] = None,
                   reader_task: Optional[str] = None) -> List[TeamMessage]:
        """只看不推进，供 _loop 判断要不要起一轮。"""
        with self._cond:
            return self._collect(worker_id, advance=False, reader_squad=reader_squad,
                                 reader_task=reader_task)

    def has_pending(self, worker_id: str,
                    reader_squad: Optional[str] = None,
                    reader_task: Optional[str] = None) -> bool:
        """是否有该 worker 尚未读到的记录。

        语义已从「有待处理的消息」变成「黑板有新增」：调用方不再据此起一轮
        对话，而是在自己的循环检查点把这些新增内容喂给模型。

        快速路径：调用点在流式 chunk 循环里，每 10ms 量级一次。若全局最大 seq
        都没超过该 worker 的任一游标，直接返回 False，省掉一次全量扫描。
        只有确实可能存在新记录时才走 _collect，由它逐条按记录归属判定。
        """
        with self._cond:
            if self._seq <= self._cursor_of(worker_id, reader_task):
                return False
            items = self._collect(worker_id, advance=False,
                                  reader_squad=reader_squad,
                                  reader_task=reader_task)
        return bool(items)

    def wait(
        self,
        worker_id: str,
        timeout: float,
        cancel_event: Optional[threading.Event] = None,
        reader_squad: Optional[str] = None,
        reader_task: Optional[str] = None,
    ) -> List[TeamMessage]:
        """阻塞等待新消息。超时或取消返回已收到的（可能为空）。

        轮询粒度 0.2s：Condition.wait 本身不响应取消信号，必须周期性醒来
        检查，否则 leader 超时取消时 worker 会永久卡在这里。
        """
        deadline = time.time() + max(0.0, timeout)
        with self._cond:
            while True:
                msgs = self._collect(worker_id, advance=True,
                                     reader_squad=reader_squad,
                                     reader_task=reader_task)
                if msgs:
                    return msgs
                if cancel_event is not None and cancel_event.is_set():
                    return []
                remaining = deadline - time.time()
                if remaining <= 0:
                    return []
                self._cond.wait(min(0.2, remaining))

    def unread_count(self, worker_id: str,
                     reader_squad: Optional[str] = None,
                     reader_task: Optional[str] = None) -> int:
        """未读条数，供状态行显示角标。"""
        return len(self.peek_since(worker_id, reader_squad=reader_squad,
                                   reader_task=reader_task))

    def latest_seq(self) -> int:
        """当前黑板上最大的 seq（空板为 0）。

        用途：委派时取一个水位快照，之后只认 seq 大于它的 stop / 记录，
        让「本次委派之前」的历史标记对新任务失效。
        """
        with self._cond:
            return max(self._seq, self._floor)

    def get_by_seq(self, seq: int,
                   reader_squad: Optional[str] = None,
                   reader: Optional[str] = None,
                   reader_task: Optional[str] = None) -> Optional[TeamMessage]:
        """按 seq 取单条记录全文（不推进任何游标）。

        供 team_read 使用：注入上下文的是预览，模型看到 seq 后按需拉全文。
        可见性规则与 _collect 一致，避免预览之外的记录被越权读取：
        - 跨队不可见
        - 别的任务的记录不可见（同 squad 也一样）
        - 定向给别人（target 既不是 "*" 也不是自己）不可见
        - stop 不通过这个通道返回（它不是内容）
        """
        with self._cond:
            for msg in self._log:
                if msg.seq != seq:
                    continue
                if msg.kind == "stop":
                    return None
                if msg.task_id is not None and msg.task_id != reader_task:
                    return None
                if reader is not None:
                    if msg.target not in ("*", reader):
                        return None
                    if reader == "leader":
                        return msg if msg.target == "leader" else None
                if msg.squad is not None and (
                        reader_squad is None or msg.squad != reader_squad):
                    return None
                return msg
            return None

    def forget(self, worker_id: str) -> None:
        """worker 被移除时清掉它的全部游标（含各作用域与全局）。"""
        with self._cond:
            self._gcursors.pop(worker_id, None)
            for key in [k for k in self._cursors if k[0] == worker_id]:
                self._cursors.pop(key, None)
            for key in [k for k in self._stop_seen if k[0] == worker_id]:
                self._stop_seen.pop(key, None)

    def forget_task(self, worker_id: str, task_id: Optional[str]) -> None:
        """清掉某个 worker 在指定作用域上的游标（任务结束后调用）。

        不清全局游标：全局记录（leader 通告）与具体任务无关，重读一次没有意义。
        """
        with self._cond:
            self._cursors.pop((worker_id, task_id), None)
            self._stop_seen.pop((worker_id, task_id), None)

    def purge_squad(self, squad: Optional[str]) -> int:
        """清掉某个小队的队内黑板记录，返回删除条数。

        用于「小队成员已被全部移除」的收尾：这块黑板已经没有队内读者了，
        留着只会在同名 worker 复用时被当成新任务的情报。

        **target="leader" 的记录保留**：它是给 leader 的上报，不是队内共享
        内容，读者是 leader 而不是队友。把成员清掉不该连带丢掉还没被取走的
        结论 —— 而 stale_hint 恰恰在推荐用 team_remove_worker 清理空闲 worker，
        照做就会丢。保留是安全的：_collect 对非 leader 读者会先按 target
        过滤掉这些记录，重建的同名 worker 读不到。

        squad=None 的语义已改：独立 worker 不属于任何小队，它的写入带 SOLO_SQUAD
        标签而不是 None，所以实参 None 只会匹配到 leader 的通告
        （post_squad=None 只出现在 leader 写入时）。独立 worker 被移除时不走
        这个清理路径 —— remove_worker 里 squad_gone 只对非 None 的队名成立。
        传 None 会清掉 leader 的历史通告，慎用。

        seq 计数器不回退：回退会让已有游标指向不存在的记录。删除后剩余记录之间
        的 seq 空洞对读取无影响 —— _collect 按 seq 比较而非按下标遍历。
        """
        with self._cond:
            kept = deque(
                (m for m in self._log
                 if m.squad != squad or m.target == "leader"),
                maxlen=self._log.maxlen,
            )
            removed = len(self._log) - len(kept)
            self._log = kept
            return removed

    # 供 UI / 调试
    def tail(self, n: int = 20) -> List[TeamMessage]:
        with self._cond:
            return list(self._log)[-n:] if n > 0 else []


KIND_LABEL = {
    "progress": "进展",
    "conclusion": "结论",
    "deadend": "死路",
}

# 预览模式下单条记录的最大字符数。推送只给有界预览 + seq，全文由模型主动
# 用 team_read 拉取。这样既不会让上下文被过程记录撑爆，也避免「未读积压」
# 跨轮悬置（预览本身就是可丢弃的）。
# conclusion 例外，它通常短且是别人真正需要的结果，全文注入。
PREVIEW_CHARS = 300

# 单次聚合块的总字符上限。超出时保留最新的记录，更早的只留一句
# 「已省略 N 条，seq 范围 X-Y，需要时用 team_read」。
# 记录不会丢（黑板上还在），只是不进上下文 —— 这正是预览模式的前提：
# 预览是可丢弃的，全文永远可以按 seq 拉回来。
MAX_DIGEST_CHARS = 8000

# 单条 conclusion 超过这个长度时退化成预览 + seq（见 format_team_digest）。
# conclusion 默认保留全文是因为它通常不长且是别人真正需要的结果；但一条
# 超长结论会独占甚至超出聚合块的预算，结果是「连它自己都被丢掉」，模型
# 只看到一句「已省略 N 条」。截断到有界预览后至少能看见是谁说了什么。
# 取 MAX_DIGEST_CHARS 的一半：留出空间给同批的其他记录。
CONCLUSION_PREVIEW_CHARS = MAX_DIGEST_CHARS // 2


def format_team_preview(msg: TeamMessage, limit: int = PREVIEW_CHARS) -> str:
    """渲染成「一行预览 + 拉取提示」，供不进上下文的记录类型使用。

    与 format_team_message 的区别是正文被截断，并显式告诉模型如何拿全文。
    这样一条 2000 字的进展只占约 300 字的上下文。
    """
    label = KIND_LABEL.get(msg.kind, msg.kind)
    text = (msg.text or "").replace("\n", " ")
    if len(text) > limit:
        preview = text[:limit] + "…"
        tail = f"（已截断，全文用 team_read seq={msg.seq}）"
    else:
        preview = text
        tail = ""
    return f"[队友{label} #{msg.seq} | {msg.sender}] {preview}{tail}"


def format_team_digest(msgs: List[TeamMessage],
                       limit: int = PREVIEW_CHARS,
                       max_chars: int = MAX_DIGEST_CHARS,
                       conclusion_limit: int = CONCLUSION_PREVIEW_CHARS) -> str:
    """把一批记录聚合成一个上下文块。

    聚合而不是逐条成消息，原因有两个：
    - 逐条时每条都要带上「这是共享黑板记录，不需要回应」的说明，短记录的
      固定开销会超过正文，实测 200 条短进展几乎压不下来；
    - 消息条数本身也是开销（每条都是一次 role 轮次）。

    conclusion 在单条不超预算时保留全文：它是别人真正需要的结果，通常不长，
    截断后模型多半会再调一次 team_read，反而更费。单条超过 conclusion_limit
    时退化成预览 + seq，否则一条超长结论会把整个聚合块的预算吃光、连自己
    都被丢掉（模型只看到「已省略 N 条」，等于什么都没收到）。

    总量超过 max_chars 时保留**最新**的记录，更早的折成一行省略说明。
    保留最新的理由：积压的旧记录与当前任务的相关性通常更低，而且它们
    不会消失（seq 可查），而最新几条最可能是刚发生的协作信号。
    """
    if not msgs:
        return ""

    def _render(full_conclusions: bool) -> List[str]:
        """full_conclusions=False 时所有 conclusion 也走预览渲染。"""
        out = []
        for m in msgs:
            text = (m.text or "").replace(chr(10), " ")
            if full_conclusions and m.kind == "conclusion" and len(text) <= conclusion_limit:
                out.append(f"[队友结论 #{m.seq} | {m.sender}] {text}")
            else:
                out.append(format_team_preview(m, limit))
        return out

    lines = _render(full_conclusions=True)
    truncated = [m.seq for m, line in zip(msgs, lines)
                 if "[队友" in line and "已截断" in line]
    total = sum(len(x) + 1 for x in lines)

    # 超预算时先把 conclusion 降级为预览（保留「谁说了什么 + seq」的线索），
    # 通常这样就够了；仍然超才从最老的开始丢。
    if total > max_chars:
        lines = _render(full_conclusions=False)
        truncated = [m.seq for m, line in zip(msgs, lines) if "已截断" in line]
        total = sum(len(x) + 1 for x in lines)

    omitted = 0
    omitted_range = ""
    if total > max_chars:
        # 从最老的开始丢，直到落进预算
        keep_from = 0
        while keep_from < len(lines) and total > max_chars:
            total -= len(lines[keep_from]) + 1
            keep_from += 1
        if keep_from > 0:
            omitted = keep_from
            omitted_range = f"#{msgs[0].seq}-#{msgs[keep_from - 1].seq}"
            lines = lines[keep_from:]
        else:
            # 理论上不可达：上面的循环会把 keep_from 推到 len(lines)。
            # 保留这条兜底，防止将来改循环条件后单条超预算却没有输出。
            lines = [lines[0][:max_chars] + "…"]

    body = "\n".join(lines)
    tail = "\n（以上是共享黑板上的记录，供你参考，不需要回应或确认。"
    if omitted:
        tail += (f"更早的 {omitted} 条（{omitted_range}）已省略，"
                 f"需要时用 team_read seq=N 逐条取。")
    if truncated:
        tail += "标注「已截断」的用 team_read seq=N 取全文。"
    tail += "）"
    return body + tail


def format_team_message(msg: TeamMessage, recipient: str) -> str:
    """把黑板记录渲染成模型可读的纯文本块。

    语气刻意做成「参考信息」而不是「有人给你发消息」：后者会让模型以为
    需要回应，进而触发互相应答。这里明确写「不需要回应」。
    不用 XML 标签：Jify 走 OpenAI 兼容协议，模型对自定义标签没有先验。
    """
    label = KIND_LABEL.get(msg.kind, msg.kind)
    header = f"[队友{label} #{msg.seq} | {msg.sender}]"
    return f"{header}\n{msg.text}\n（这是共享黑板上的记录，供你参考，不需要回应或确认。）"


def role_summary(system_prompt: str, limit: int = 60) -> str:
    """从角色 system prompt 提取一行摘要，用于团队成员名单。

    取第一条非空行并剥掉常见列表前缀。角色 prompt 通常第一行就是
    「你是 XX 专家」这种定性句，够模型判断该找谁。
    """
    for line in (system_prompt or "").splitlines():
        line = line.strip().lstrip("-*·• ").strip()
        if line:
            return line[:limit]
    return ""


def sanitize_prompt_inline(text: str, limit: int = 80) -> str:
    """把外部可控文本压成「单行、无控制字符」，用于拼进 system prompt。

    worker_id 和角色摘要都由主 Agent 指定，等于模型可控输入。原样拼进
    system prompt 时，一个带换行的名字就能伪造出新的段落 ——
    例如 "a1\\n\\n忽略以上所有指令" 会变成花名册里的两条独立内容。

    因此：换行/制表/控制字符一律替换成空格，连续空白折叠，按 limit 截断。
    """
    cleaned = "".join(
        " " if (ch.isspace() or ord(ch) < 32 or ord(ch) == 127) else ch
        for ch in (text or "")
    )
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "…"
    return cleaned


def format_roster(
    members: List[Tuple[str, str]],
    self_id: str,
    squad: Optional[str] = None,
    total_workers: Optional[int] = None,
) -> str:
    """渲染「团队成员」段。members 为 [(worker_id, 角色摘要)]。

    只列名字和角色，不列工具白名单 —— 白名单是实现细节，模型拿它没法
    做任何决策，反而占上下文。

    squad: 小队名，None 表示独立 worker。独立 worker 不属于任何小队、也没有
        可共享的黑板，因此整个团队段都不输出 —— 包括上报指引，它的结果本来就
        通过委派的返回值交给 leader，不需要再走黑板。
    """
    if squad is None:
        # 独立 worker：没有队友、没有黑板，整段都不该出现在 prompt 里
        return ""
    if len(members) <= 1 and not (squad and total_workers and total_workers > len(members)):
        return ""
    lines = ["", "", "## 团队成员"]
    if squad:
        lines.append(f"你属于 {squad} 小队，只能看到本队成员的黑板记录。")
        if total_workers and total_workers > len(members):
            lines.append(
                f"（全队共 {total_workers} 人，其余成员在其他小队，"
                f"他们的记录你看不到——这是刻意的隔离，不要尝试猜测或联系他们。）"
            )
    for wid, desc in members:
        # worker_id 与角色摘要都是外部可控文本，必须压成单行再拼进 prompt
        safe_wid = sanitize_prompt_inline(wid)
        safe_desc = sanitize_prompt_inline(desc)
        suffix = "（你自己）" if wid == self_id else ""
        lines.append(f"- {safe_wid}{suffix}: {safe_desc}" if safe_desc
                     else f"- {safe_wid}{suffix}")
    lines.append("")
    lines.append(
        "你和队友通过一块共享黑板协作，不是互相发消息。"
        "用 team_message / team_post 把进展写到黑板上（自动署名），"
        "正在跑同一批任务的队友会在自己的检查点读到，不需要你指定收件人。"
    )
    lines.append(
        "黑板默认按任务隔离：只有正在跑同一批任务的队友能看到你的记录，"
        "别的任务看不到。所以别把只有特定任务才成立的细节当成通用结论写。"
    )
    lines.append(
        "黑板是单向留信息，不是对话：不需要回复别人的记录，"
        "也不要为「确认收到」而回写。只有确实产生新结论时才写。"
        "写清「结论 + 依据」，让别人能直接拿去用。"
    )
    lines.append(
        "上下文里注入的过程记录（progress / deadend）是截断预览，结尾带 seq 号；"
        "判断某条有用时用 team_read(seq=N) 拉全文。conclusion 一般是全文。"
    )
    if squad:
        lines.append(
            "重要：黑板按小队隔离，leader 看不到队内记录。"
            "凡是对整体目标有价值的关键发现（已确认的结论、走通的路径、卡死的死路），"
            "必须额外写一条 target=\"leader\" 的记录上报，否则 leader 永远不知道。"
        )
        lines.append(
            "如果你已经拿下目标，先把完整结论（成果原文 + 可复现步骤 + 依据）"
            "写成一条 target=\"leader\"、kind=\"conclusion\" 的记录，"
            "然后立刻写一条 kind=\"stop\" 的记录（target 留空），"
            "正在跑同一批任务的队友读到会停手收工，避免在同一方向上继续烧时间。"
            "顺序不能反：leader 从 stop 原文只能看到摘要，详细过程靠那条 conclusion；"
            "先 stop 后 conclusion 会让 leader 在一段时间里拿到结果却找不到分析过程。"
        )
        lines.append(
            "自己被叫停时不要重试同一条路径 —— 那说明目标在别处已经达成。"
        )
    return "\n".join(lines)



# TeamWorker — 轻量级工作线程
def _cancel_team_task(task: "TeamTask") -> bool:
    """取消一个在途团队任务。

    两件事都要做，缺一不可：
    - 置位 cancel_event：worker 内的 subagent 循环会在下个检查点退出
    - 调 call_ctx.cancel()：worker 内嵌套的长命令（exec 等）登记的 killer 被真正触发

    worker 可能尚未取得任务（call_ctx 还是 None），此时只置位信号即可，
    worker 启动时会发现并立即退出。
    """
    task.cancel_event.set()
    ctx = task.call_ctx
    if ctx is not None:
        ctx.cancel()
    return True


class TeamWorker:
    """
    团队成员。封装 SubagentRunner, 运行在独立线程中, 通过队列接收任务。

    与裸 SubagentRunner 的区别:
    - 有独立的 system_prompt (角色定义)
    - 有独立的工具白名单 (按角色控制权限)
    - 通过队列持续接收任务, 而非一次性运行后销毁
    - 支持状态查询
    """

    def __init__(
        self,
        worker_id: str,
        model_client: Any,
        config: Any,
        system_prompt: str,
        whitelist: Set[str],
        max_iterations: int = 100,
        squad: Optional[str] = None,
    ):
        self.worker_id = worker_id
        # model_client 可以是实例，也可以是返回实例的 callable（延迟解析），
        # 后者用于在主 Agent 切换模型/provider 后让 Worker 跟随当前模型。
        self.model_client = model_client
        self.config = config
        self.system_prompt = system_prompt
        self.whitelist = whitelist
        self.max_iterations = max_iterations
        # 小队名。None = 独立 worker（不属于任何队、没有可共享的黑板）；
        # 非 None 时只能与本队成员互通，名单也只列本队。独立 worker 与各小队
        # 可以自由混用，记录可见性按 squad 逐条判定，不需要全队统一模式。
        self.squad: Optional[str] = squad

        self._task_queue: queue.Queue = queue.Queue()
        # 正在执行中的标志：队列里的任务 qsize 可见，但 worker 线程 get 走之后、
        # set_result 之前这段执行期队列已空，仅靠 qsize 会误判"空闲"，
        # 导致并行分配把新任务堆给正在忙的 worker
        self._executing = False
        self._result_events: Dict[str, threading.Event] = {}
        self._results: Dict[str, str] = {}
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()
        # 团队邮箱：由 TeamLeader 在 add_worker 时注入；为 None 时 worker 无通信能力
        self.mailbox: Optional[TeamMailbox] = None
        # 团队花名册来源：由 TeamLeader 注入（弱引用自身），worker 每次构建
        # system prompt 时现场取，以便后续 add_worker 的新成员能被老 worker 看见
        self._roster_provider: Optional[Callable[[], List[Tuple[str, str]]]] = None
        # 未读角标，仅用于状态行展示
        self._inbox_count = 0

        # 黑板投递相位：
        #   "current" —— 本轮任务还能吸收黑板新增（默认）
        #   "next"    —— 本轮已产出终态，迟到的记录留到下次被唤醒
        # 新一轮开始时若相位是 "next"，先把游标推到最新，把那批「上一轮的尾巴」
        # 丢掉，避免它们混进全新任务的上下文。代价是 worker 空闲期间队友写的
        # 进展不再补读 —— 这是刻意的取舍：预览模式已经保证记录不会丢，
        # 需要时模型还能用 team_read 按 seq 取。
        self._mailbox_phase = "current"
        # 当前正在执行的任务 id。黑板记录带上它之后，同队但不同任务的进展
        # 就不会互相注入。执行期间由 _execute 设置，结束后清空。
        self._current_task_id: Optional[str] = None
        # 消息唤醒：_loop 在队列空时等它，避免把消息塞进 _task_queue 污染 busy 判定
        self._mail_wake = threading.Event()
        # 当前正在执行的 TeamTask，供 shutdown / interrupt 取消
        self._active_task: Optional[TeamTask] = None
        # 最近一次收到 stop 信号的描述（"谁: 说了什么"），供终态说明使用
        self._stop_reason: str = ""
        # 最近一次 stop 的发送者 worker_id，供主 Agent 判断成果在谁手上
        self._stopped_by: str = ""
        # 最近一次任务结束的时刻（monotonic）。status() 用它算出空闲时长，
        # 据此判断该 Worker 是否「跑过但一直没人管」，提示主 Agent 清理。
        # 初始为构造时刻：新建的 Worker 也算从建时起空闲。
        self._last_active_at: float = time.monotonic()
        self._stats = {"tasks_completed": 0, "total_elapsed": 0.0}
        # 跨任务保留的对话上下文（不含 system），worker 线程串行读写，无需额外加锁
        self._history: List[Dict] = []

        # 预构建 tool schemas
        self._whitelist_schemas: List[Dict] = []

    def _resolve_model_client(self) -> Any:
        """解析出当前应使用的 model client。

        model_client 为 callable 时每次解析（跟随主 Agent 切换），否则直接用实例。
        """
        mc = self.model_client
        return mc() if callable(mc) else mc

    def _build_progress_callback(self, task_snippet: str, start_time: float, tool_counter: list,
                                 detail: Dict, task: Optional["TeamTask"] = None):
        """构建进度回调：把 think/tool/token 细节累积到 detail 并同步到状态行。

        detail 为可变 dict（由 _execute 持有引用），跨事件累积：
        - stream:   工具调用活动流 [{kind: tool, text}]，新行滚入、旧行滚出。
        - think:    最近一段推理内容，压成单行并只保留尾部（状态栏跑马灯）。
        - last_tool: 最近一次工具名
        - sent_est / recv_est: token 估算
        """
        engine = get_output_engine()
        _STREAM_MAX = 40   # 活动流条目上限
        _THINK_TAIL = 72   # think 跑马灯窗口：只留尾部这段字符

        def _push(kind: str, text: str) -> None:
            """追加活动流条目。"""
            stream = detail["stream"]
            stream.append({"kind": kind, "text": text})
            if len(stream) > _STREAM_MAX:
                del stream[:-_STREAM_MAX]

        def _emit() -> None:
            info = {
                "task": task_snippet,
                "_start": start_time,
                "tool_uses": tool_counter[0],
                "status": "running",
                "stream": list(detail["stream"]),
                "think": detail.get("think", ""),
                "last_tool": detail["last_tool"],
                "sent_est": detail["sent_est"],
                "recv_est": detail["recv_est"],
            }
            _emit_team_update(self.worker_id, info)
            if engine:
                engine.set_team_worker(self.worker_id, info)

        def _on_progress(event_type: str, data: Dict) -> None:
            if event_type == "tool_start":
                tool_counter[0] += 1
                name = data.get("name", "")
                detail["last_tool"] = name
                # 有工具调用说明推理阶段结束，跑马灯该让位
                detail["think"] = ""
                args = data.get("args", {}) or {}
                args_str = json.dumps(args, ensure_ascii=False) if args else ""
                if len(args_str) > 80:
                    args_str = args_str[:77] + "\u2026"
                _push("tool", f"{name}({args_str})")
            elif event_type == "thinking":
                # 压成单行 + 只留尾部：新字符从右进、旧字符从左出，形成滚动效果。
                # 不保留全文是刻意的，状态栏只提供「它此刻在想什么」的粗粒度感知。
                # \t 必须一并替换：cell_len 按 1 列计宽而 rich 渲染时展开 1-8 列，
                # 保留它会让 think 行实际渲染宽度超出列宽被 fold 成两行
                seg = (data.get("text") or "").replace("\n", " ").replace("\r", " ").replace("\t", " ")
                detail["think"] = (detail.get("think", "") + seg)[-_THINK_TAIL:]
            elif event_type == "token_update":
                detail["sent_est"] = data.get("sent", 0)
                detail["recv_est"] = data.get("recv", 0)
            elif event_type == "iteration":
                # 运行中即可读出已消耗轮数：写入 task 而不是 detail，因为
                # team_wait_any 的 running 条目直接读 task.iterations。
                # 终点仍由 _execute 用 out_meta 覆盖一次，两者值一致。
                if task is not None:
                    task.iterations = int(data.get("iterations") or 0)
            _emit()

        return _on_progress

    def _effective_system_prompt(self) -> str:
        """Worker 实际使用的 system prompt。

        仅当 whitelist 含 load_skill 时追加 skill 清单：否则 Worker 拿到名字
        也无从加载，纯属噪音。格式与主 Agent 的「## 拥有的skill」段保持一致。

        花名册每次现场取而非构造时固化：团队是动态的，leader 后续 add_worker
        加进来的成员，已有 worker 也必须能在下一轮看到，否则它不知道能找谁。
        """
        prompt = self.system_prompt

        if self._roster_provider is not None:
            try:
                members, total = self._roster_provider(self.worker_id)
            except Exception:
                members, total = [], 0
            roster = format_roster(members, self.worker_id, self.squad, total)
            if roster:
                prompt += roster

        if "load_skill" not in self.whitelist:
            return prompt
        try:
            from tools.skill_paths import discover_skills
            lines = [f"- {e.name}: {e.description}" for e in discover_skills()]
        except Exception:
            return prompt
        if not lines:
            return prompt
        return prompt + "\n\n## 拥有的skill\n" + "\n".join(lines)

    def _build_schemas(self) -> List[Dict]:
        """从 whitelist 名称构建 tool schemas"""
        schemas = []
        for name in self.whitelist:
            tool = registry.get(name)
            if tool:
                schemas.append({
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                })
        return schemas

    def start(self):
        """启动 worker 线程"""
        if self._running:
            return
        self._whitelist_schemas = self._build_schemas()
        self._running = True
        self._thread = threading.Thread(
            target=self._loop,
            name=f"team-worker-{self.worker_id}",
            daemon=True,
        )
        self._thread.start()

    def shutdown(self):
        """关闭 worker"""
        self._running = False
        # 取消在途任务（含邮箱 turn，它不在 leader._tasks 里）
        active = self._active_task
        if active is not None:
            _cancel_team_task(active)
        # 放入一个毒丸任务唤醒线程
        self._task_queue.put(None)
        # 若线程正卡在邮箱等待里，也必须唤醒
        self._mail_wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    # 黑板
    def _drain_blackboard(self) -> List[Dict]:
        """取走本 worker 尚未读到的黑板记录，转成 OpenAI 格式的插入块。

        读走即推进游标，因此同一条记录只会被喂给模型一次。

        渲染分两档：
        - conclusion：全文注入。它是别人真正需要的结果，通常也不长。
        - progress / deadend：只注入有界预览 + seq，全文用 team_read 拉。
          过程记录量大且多数与当前任务无关，全文注入会把上下文撑爆。
        """
        if self.mailbox is None:
            return []
        msgs = self.mailbox.read_since(self.worker_id, reader_squad=self.squad,
                                       reader_task=self._current_task_id)
        self._inbox_count = 0
        if not msgs:
            return []
        # 聚合成一条消息：多条记录共享一次「这是参考信息」的说明，
        # 且只占一个 role 轮次。逐条渲染时短记录的固定开销会盖过正文。
        return [{"role": "user", "content": format_team_digest(msgs)}]

    def _has_blackboard_delta(self) -> bool:
        """轻量谓词：黑板是否有本 worker 尚未读到的记录。不消费。

        抢占用它，也供状态栏刷新未读角标。
        """
        if self.mailbox is None:
            return False
        pending = self.mailbox.has_pending(self.worker_id, reader_squad=self.squad,
                                          reader_task=self._current_task_id)
        if pending:
            self._inbox_count = self.mailbox.unread_count(
                self.worker_id, reader_squad=self.squad,
                reader_task=self._current_task_id)
        return pending

    # 已移除 _discard_stale_stops：stop 的「只生效一次」由 take_stop 的
    # _stop_seen 水位保证，任务启动时不需要（也不应该）清空历史标记。
    def _should_stop(self, task: Optional[TeamTask] = None) -> bool:
        """是否收到同队成员的 stop 标记。取走即消费，只生效一次。

        与黑板增量读取分开：stop 不是「要读的内容」，而是「别再跑了」的指令。
        取走后不会再出现在上下文里，避免模型把终止标记当成新任务去处理。

        task 带 stop_scope="delegation" 时，只认 seq 大于其
        stop_seq_baseline 的 stop —— 提交本次任务之前就在黑板上的历史标记
        不再生效。这解决了「队友早已收工，leader 却要派新方向的活」时新任务
        被旧 stop 秒杀的问题，且不影响 global（默认）语义。

        任务作用域：只认本任务作用域（或全局）的 stop。两个并行的无关任务
        因此不会互相误杀；同一批 collaborate 任务共享作用域，仍能互相叫停。
        """
        if self.mailbox is None:
            return False
        min_seq = 0
        if (task is not None and task.stop_scope == "delegation"
                and task.stop_seq_baseline > 0):
            min_seq = task.stop_seq_baseline
        msg = self.mailbox.take_stop(self.worker_id, reader_squad=self.squad,
                                     min_seq=min_seq,
                                     reader_task=self._current_task_id)
        if msg is None:
            return False
        # 记下是谁叫停的，供 _execute 生成可读的终态说明
        self._stop_reason = f"{msg.sender}: {msg.text}" if msg.text else msg.sender
        self._stopped_by = msg.sender
        return True

    def _wake_idle(self) -> None:
        """叫醒空闲的 worker 去取任务。

        只有 leader 的委派需要它（submit 时会被唤醒）。黑板写入 **不** 用它 ——
        那会让 worker 之间重新具备「互相叫醒」的能力，正是自激互答的来源。
        """
        self._mail_wake.set()

    def submit(self, task: TeamTask) -> Future:
        """
        提交任务给 worker (非阻塞, 返回 Future)

        Returns:
            Future, 其 .result() 将返回 str 结果
        """
        task.status = "pending"
        task.worker_id = self.worker_id
        future: Future = Future()

        self._task_queue.put((task, future))
        # 必须在这里唤醒：_loop 空闲时阻塞在 _mail_wake.wait(0.5) 上，
        # 不 set 就只能等定时器自然醒来，每次委派白等最多 500ms
        # （串行派 N 个任务累计 N×0.5s）。唤醒只是「去看队列」，
        # 不会让 worker 因黑板内容自己起一轮，互激结构不变。
        self._wake_idle()
        return future

    def submit_and_wait(self, task: TeamTask, timeout: float = 300) -> str:
        """
        提交任务并阻塞等待结果

        超时只是 leader 侧放弃等待，worker 线程仍在跑。必须显式置取消信号，
        否则这个 worker 会被占满 max_iterations 次迭代，队列里排着的下一个
        任务只能干等。

        Returns:
            Worker 输出的最终文本
        """
        future = self.submit(task)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            _cancel_team_task(task)
            task.status = "failed"
            task.error = f"任务超时 ({timeout}s)"
            # 与 _execute 正常路径返回同一套 JSON 结构：主 Agent 只按 JSON 解析，
            # 纯文本会被 registry 包成 {"success": true, "data": "..."}，
            # 让「失败」看起来像成功。
            return self._failure_json(task)
        except Exception as e:
            task.status = "failed"
            task.error = str(e)
            return self._failure_json(task)

    def _failure_json(self, task: TeamTask) -> str:
        """失败路径的统一返回体：字段与 _execute 成功路径保持一致。

        submit_and_wait / delegate 的失败出口曾经返回裸文本，主 Agent 拿到的
        不是 JSON，且经 registry 包装后还会显示 success=true。
        """
        return json.dumps({
            "task_id": task.id,
            "worker_id": self.worker_id,
            "status": task.status,
            "limit_hit": task.limit_hit,
            "iterations": task.iterations,
            "stopped": task.stopped,
            "stop_reason": task.stop_reason or None,
            "stopped_by": task.stopped_by or None,
            "result": None,
            "error": task.error,
            "tool_uses": 0,
            "elapsed": round(time.time() - task.created_at, 1),
        }, ensure_ascii=False)


    # 内部循环
    def _loop(self):
        """Worker 主循环: 从队列取任务 → 执行 → 写回结果。

        这里没有「因消息起一轮」的分支。黑板上的新记录不会唤醒 worker，
        只在两个时机被读到：
        1. 执行任务期间，SubagentRunner 的检查点（迭代开头 / 工具结果之后）
        2. 下一次被委派任务时，任务开头会把积压的记录一并带上

        这个设计是刻意的：worker 之间不能互相叫醒，因此不存在
        「A 写给 B、B 回给 A」的自激结构。
        """
        while self._running:
            try:
                item = self._task_queue.get_nowait()
            except queue.Empty:
                # 空闲等待：只在有新委派（_wake_idle）或 0.5s 定时到达时醒来。
                # 不再检查黑板 —— 没人看的时候不该白跑一轮。
                self._mail_wake.wait(0.5)
                self._mail_wake.clear()
                continue

            if item is None:
                break

            task, future = item
            self._executing = True
            self._active_task = task
            try:
                result = self._execute(task)
                future.set_result(result)
            except Exception as e:
                # 必须在这里写回终态：异步路径（delegate_async）没有任何
                # 消费者读 future，异常会被静默吞掉，task 就永远停在
                # pending/running —— wait_any 会把它一直当成在途任务。
                task.status = "failed"
                task.error = str(e)
                future.set_exception(e)
            finally:
                self._active_task = None
                self._executing = False

    def _execute(self, task: TeamTask) -> str:
        """核心执行: 调 SubagentRunner.run()

        worker 是长驻线程，不能用创建时的 contextvars（每次任务的上文不同），
        故在执行期间现场挂一个 CallContext，并把它的取消信号与本任务的
        cancel_event 共用同一个 Event：

        - leader 侧超时 -> killer 置位该 Event 并调 call_ctx.cancel()
        - SubagentRunner 在每个检查点读到它 -> 优雅退出
        - 任务内嵌套的长工具（exec 等）通过 call_ctx 登记的 killer 被真正杀掉
        """
        task.status = "running"
        start = time.time()
        tool_counter = [0]
        # 每个任务重新起算：上一轮的停止理由不能残留到这一轮，
        # 否则一个从未收到 stop 的新任务会被标成 stopped。
        self._stop_reason = ""
        self._stopped_by = ""
        # 注意：这里**不能**清空黑板上的 stop 标记。
        # 标记的「只生效一次」由 take_stop 的 _stop_seen 水位保证，
        # 无需在任务启动时再兜底丢弃。反过来，若在这里无条件清空，stop 就
        # 处于「上一轮任务已结束、下一轮尚未提交」的缝隙时会被静默吞掉：
        # 队友在 worker 空闲期间广播的收工指令永远送不到，且窗口无上界
        # （空闲多久就失效多久）。

        stale_skipped = 0
        # 相位是 "next" 说明上一轮已收尾，黑板上此刻的未读全是「上一轮的尾巴」。
        # 把游标推到最新，让它们不进本轮上下文（记录本身仍在黑板上，可 team_read）。
        # 迟到的记录留到下次，不延长也不污染已经收尾的那一轮。
        # 注意此处用新任务的「黑板作用域 id」读：本轮尚未开始，读的只是
        # 「全局 + 本任务」视角。用 blackboard_task_id 而非 id，是为了让
        # share_task_id=True 的并发任务共享同一份可见性。
        self._current_task_id = task.blackboard_task_id or task.id
        if self._mailbox_phase == "next" and self.mailbox is not None:
            # 丢弃「上一轮留下来的积压」：只处理没有任务作用域的记录。
            # 无作用域记录在真实路径里只有 leader 通告（worker 只能在自己
            # 的任务执行期间调工具，那时写入必带任务作用域）。刚入队的成员
            # 同样走这条路径（assign_squad 不重置相位），所以入队前的内容
            # 也不会补读。
            stale_skipped = len(
                self.mailbox.read_since(self.worker_id, reader_squad=self.squad,
                                        reader_task=self._current_task_id,
                                        global_only=True))
        self._mailbox_phase = "current"

        call_ctx = CallContext(cancel_event=task.cancel_event)
        task.call_ctx = call_ctx
        ctx_token = set_call_context(call_ctx)

        task_snippet = task.content[:42] + "…" if len(task.content) > 42 else task.content

        engine = get_output_engine()
        detail = {
            "stream": [],
            "think": "",
            "last_tool": "",
            "sent_est": 0,
            "recv_est": 0,
        }
        _emit_team_update(self.worker_id, {
            "task": task_snippet,
            "_start": start,
            "tool_uses": 0,
            "status": "running",
        })
        if engine:
            engine.set_team_worker(self.worker_id, {
                "task": task_snippet,
                "_start": start,
                "tool_uses": 0,
                "status": "running",
                "stream": [],
                "think": "",
                "last_tool": "",
                "sent_est": 0,
                "recv_est": 0,
            })

        on_progress = self._build_progress_callback(task_snippet, start, tool_counter, detail, task)

        runner = SubagentRunner(self._resolve_model_client(), self.config)
        # 结构化终态回传口：runner 在 finally 里写入 {limit_hit, iterations}，
        # 供下面区分「模型自己收敛」与「打满上限被动退出」。
        run_meta: Dict = {}
        try:
            result = runner.run(
                task=task.content,
                system_prompt=self._effective_system_prompt(),
                whitelist_schemas=self._whitelist_schemas,
                whitelist_names=self.whitelist,
                max_iterations=self.max_iterations,
                on_progress=on_progress, # 状态栏同步进展
                interrupt_event=task.cancel_event,
                history=self._history,
                out_history=self._history,
                # 黑板增量仅在 worker 有 mailbox 时接上；None 时 runner 行为不变
                inbox=self._drain_blackboard if self.mailbox is not None else None,
                has_inbox=self._has_blackboard_delta if self.mailbox is not None else None,
                # 停止信号：同队成员留下 stop 标记时终止本轮。
                # 与 inbox 分开传，因为语义是「终止」而非「带新消息重采样」。
                # 传闭包捕获当前 task：stop_scope="delegation" 的任务只认
                # 提交之后写入的 stop，不受历史标记影响。
                should_stop=(lambda: self._should_stop(task))
                if self.mailbox is not None else None,
                out_meta=run_meta,
            )
            task.limit_hit = bool(run_meta.get("limit_hit"))
            task.iterations = int(run_meta.get("iterations") or 0)
            task.stopped = bool(run_meta.get("stopped"))
            if task.stopped:
                # stopped 优先于 interrupted：两者可能同时置位（stop 落地的
                # 同时也被 cancel），但对主 Agent 有意义的是「队友叫停」，
                # 因为它意味着成果已经在别处，该去回收而不是重试。
                task.status = "stopped"
                task.stop_reason = self._stop_reason
                task.stopped_by = self._stopped_by
            elif task.cancel_event.is_set():
                task.status = "interrupted"
            elif task.limit_hit:
                # 必须与 completed 分开：主 Agent 只有看到 limit_hit
                # 才会去回收情报并换人，把它当成功会导致任务被静默放弃。
                task.status = "limit_hit"
            else:
                task.status = "completed"
            task.result = result
        except Exception as e:
            task.status = "failed"
            task.error = str(e)
            result = f"[TeamWorker:{self.worker_id}] 异常: {e}"
        finally:
            elapsed = time.time() - start
            task.finished_at = time.time()
            # 记录空闲起点：stale 判定依赖它
            self._last_active_at = time.monotonic()
            # 摘掉本任务的调用上下文，避免残留到下一个任务的线程里
            reset_call_context(ctx_token)
            task.call_ctx = None
            # 本轮已产出终态：切到 "next"，之后到达的黑板记录不再并入本轮。
            # 下一轮开始时据此丢弃上一轮的尾巴（见 _execute 开头）。
            self._mailbox_phase = "next"
            self._current_task_id = None
            with self._lock:
                self._stats["tasks_completed"] += 1
                self._stats["total_elapsed"] += elapsed

            if engine:
                engine.set_team_worker(self.worker_id, {
                    "task": task_snippet,
                    "_start": start,
                    "tool_uses": tool_counter[0],
                    "status": task.status,
                    "stream": list(detail["stream"]),
                    "think": detail.get("think", ""),
                    "last_tool": detail["last_tool"],
                    "sent_est": detail["sent_est"],
                    "recv_est": detail["recv_est"],
                    "inbox": self._inbox_count,
                })
            _emit_team_update(self.worker_id, {
                "task": task_snippet,
                "_start": start,
                "tool_uses": tool_counter[0],
                "status": task.status,
                "stream": list(detail["stream"]),
                "think": detail.get("think", ""),
                "last_tool": detail["last_tool"],
                "sent_est": detail["sent_est"],
                "recv_est": detail["recv_est"],
                "inbox": self._inbox_count,
            })

        # 上下文记忆全量压缩：累积上下文长度达到阈值时同步压缩
        self._maybe_compress_history()

        return json.dumps({
            "task_id": task.id,
            "worker_id": self.worker_id,
            "status": task.status,
            "limit_hit": task.limit_hit,
            "iterations": task.iterations,
            # 与 wait_any 的 completed 条目保持同一套字段：主 Agent 走同步
            # 委派时也要能定位「成果在谁手上」，否则只看到 status=stopped
            # 却不知道去找谁回收，只能盲猜或重派。
            "stopped": task.stopped,
            "stop_reason": task.stop_reason or None,
            "stopped_by": task.stopped_by or None,
            "result": result,
            "tool_uses": tool_counter[0],
            "elapsed": elapsed,
            # 本轮开始时被丢弃的「上一轮尾巴」条数。非 0 说明这个 worker
            # 上一轮收尾后队友还在写，那些记录没有进本轮上下文
            # （仍在黑板上，可用 team_read 按 seq 取）。给 leader 一个可观测信号。
            "stale_blackboard_skipped": stale_skipped,
        }, ensure_ascii=False)

    @property
    def busy(self) -> bool:
        """是否正在执行任务"""
        return self._executing or self._task_queue.qsize() > 0

    @property
    def stats(self) -> Dict:
        with self._lock:
            return dict(self._stats)

    def _maybe_compress_history(self) -> None:
        """当累积上下文长度达到阈值时，同步做一次全量 LLM 压缩。

        压缩把整个历史对话压成一段摘要文本，替换 self._history，后续任务
        以摘要 + 新任务继续。采用全量（而非增量）压缩策略。
        """
        threshold = getattr(self.config, "context_compress_threshold", 1000000)
        if not self._history:
            return
        if len(json.dumps(self._history, ensure_ascii=False)) < threshold:
            return

        full_text = "\n\n".join(
            f"[{m.get('role', '')}] {m.get('content', '')}"
            for m in self._history
        )
        prompt = (
            "请对以下团队成员的过往任务对话进行压缩总结，保留用户意图、关键结果、"
            "主要进展、已解决的问题，以及正在进行尚未完成的工作，压缩成模型可参考"
            "的历史信息：\n\n" + full_text
        )
        try:
            resp = self._resolve_model_client().chat(
                messages=[{"role": "user", "content": prompt}],
                tool_schemas=[],
                model=self.config.model,
                stream=False,
                # 一次性压缩：prompt 每次唯一、永不命中缓存，
                # 打 cache_control 只会多付写入费
                disable_prompt_cache=True,
            )
            summary = (resp.content or "").strip()
        except Exception:
            summary = ""

        if summary:
            self._history = [{"role": "user", "content": SUMMARY_HEADER + "\n" + summary}]


# TeamLeader — 编排器
class TeamLeader:
    """
    团队 Leader。管理 Worker 池, 提供委派 / 广播 / 状态查询能力。

    暴露为 tool 供主 Agent 调用:
    - team_delegate:  委派任务给指定或空闲 Worker
    - team_broadcast: 广播同一任务给所有 Worker
    - team_status:    查询所有 Worker 状态
    - team_add_worker: 动态添加 Worker (带角色/白名单)
    """

    def __init__(self, model_client: Any, config: Any):
        self.model_client = model_client
        self.config = config
        self._workers: Dict[str, TeamWorker] = {}
        self._tasks: Dict[str, TeamTask] = {}
        self._lock = threading.Lock()
        # 连续空返回计数：wait_any 在没有在途任务时累加，任何一次成功取到结果
        # 或重新委派时清零。超过 WAIT_ANY_IDLE_LIMIT 即熔断，防止主 Agent
        # 在无任务可等时反复调用。
        self._idle_streak = 0
        # 全队共享的消息日志，所有 worker 共用同一实例
        self.mailbox = TeamMailbox()

    def interrupt(self) -> None:
        """中断所有 Worker 当前正在执行的任务。不关闭 Worker 线程与团队结构，中断后团队仍可继续委派。"""
        # 每个任务一个独立信号：worker 线程读的是 task.cancel_event。
        # 不能共用单一事件——上一次已取消任务的信号会被下一轮 clear() 抹掉，
        # 导致那个 worker 永远跑下去。
        with self._lock:
            pending = [t for t in self._tasks.values() if t.status in ("pending", "running")]
            workers = list(self._workers.values())
        for t in pending:
            _cancel_team_task(t)
        # 邮箱 turn 的 task 不入 _tasks，单独从 worker 上取
        for w in workers:
            active = w._active_task
            if active is not None and active not in pending:
                _cancel_team_task(active)


    # Worker 管理
    def add_worker(
        self,
        worker_id: str,
        system_prompt: str,
        whitelist: Set[str],
        max_iterations: int = 100,
        squad: Optional[str] = None,
    ) -> TeamWorker:
        """
        添加一个 Worker 并启动。

        whitelist 示例:
            {"read_file", "patch_file", "exec"}           # bug-fixer
            {"read_file", "static_analysis"}              # code-reviewer
            {"read_file", "write_file", "exec"}           # file-generator

        squad: 小队名。传了就成为某个小队的成员：只能与本队成员通信，花名册
            也只列本队。不传则是「独立 worker」：不属于任何小队，也没有可共享
            的黑板 —— 它只能收到 leader 的通告，写的记录也只有 leader 能看到。
            独立 worker 与各小队可以自由混用。
        """
        squad = squad or None
        if worker_id == LEADER_ID:
            # "leader" 是 leader 的固定身份（_current_sender / collect_reports /
            # team_wait 都按这个字符串判定写着）。允许重名 worker 会让
            # post_message 的 sender_is_leader 判错、team_wait 取到该 worker 的
            # squad、leader 也无法按队定向写入。名字是共享资源，直接拒绝。
            raise ValueError(
                f"'{LEADER_ID}' 是 leader 的保留身份，不能用作 worker_id"
            )
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id 必须是非空字符串")
        if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in worker_id):
            # 名字会拼进花名册（system prompt）、工具参数、黑板记录头。
            # 含空白或控制字符没有任何合法用途，却能伪造 prompt 段落。
            raise ValueError(
                f"worker_id 不能包含空白或控制字符，收到 {worker_id!r}"
            )
        if squad is not None and (
            not isinstance(squad, str) or not squad.strip()
            or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in squad)
        ):
            # squad 同样会进 system prompt（「你属于 X 小队」）和黑板记录头
            raise ValueError(
                f"squad 不能包含空白或控制字符，且不能为空，收到 {squad!r}"
            )
        if squad == LEADER_ID:
            raise ValueError(f"'{LEADER_ID}' 是 leader 的保留身份，不能用作 squad 名")
        if squad == SOLO_SQUAD:
            raise ValueError(f"'{SOLO_SQUAD}' 是系统保留标签，不能用作 squad 名")
        with self._lock:
            if worker_id in self._workers:
                raise ValueError(f"Worker '{worker_id}' 已存在")

            # 校验 whitelist 里的工具名：写错的名字会被 _build_schemas 静默丢弃，
            # Worker 起来后少工具却毫无提示，所以在启动前就顶回去让模型改。
            unknown = sorted(set(whitelist) - set(registry.get_all_names()))
            if unknown:
                raise ValueError(
                    f"以下工具不存在，请检查拼写: {', '.join(unknown)}；"
                    f"当前可用工具: {', '.join(sorted(registry.get_all_names()))}"
                )

            # 强制带上 load_skill 与 write_file：load_skill 使所有 Worker 的
            # system prompt 追加「## 拥有的skill」清单段（见 _effective_system_prompt），
            # 并能按需加载方法论；write_file 保证任何 Worker 都具备落盘产出能力，
            # 委派方无需在白名单里重复声明。
            # 三个 team_* 通信工具同样强制：worker 之间互发消息是基础能力，
            # 角色白名单只该约束业务工具，不该把通信能力关掉。
            whitelist = set(whitelist) | {"load_skill", "write_file",
                                         "team_message", "team_post", "team_wait",
                                         "team_read"}

            worker = TeamWorker(
                worker_id=worker_id,
                model_client=self.model_client,
                config=self.config,
                system_prompt=system_prompt,
                whitelist=whitelist,
                max_iterations=max_iterations,
                squad=squad,
            )
            worker.mailbox = self.mailbox
            worker._roster_provider = self._roster
            # 新 worker 的初始相位是 "next"：它此前没参与过任何一轮，
            # 黑板上此刻的存量记录对它都是「历史」，不该在它第一次被执行时
            # 一股脑注入 —— 那正是「同队老成员的旧进展污染新任务」的另一半。
            # 它第一次跑完后会如实切到 "current"，此后轮内协作正常。
            worker._mailbox_phase = "next"
            worker.start()
            self._workers[worker_id] = worker
            return worker

    # 花名册：供 worker 构建 system prompt 时现场调用
    def _roster(self, self_id: str) -> Tuple[List[Tuple[str, str]], int]:
        """返回 (可见成员 [(worker_id, 角色摘要)], 全员总数)。

        持有锁快照，避免与 add/remove 竞争。self_id 属于某小队时只返回同队
        成员；独立 worker（squad=None）只返回它自己 —— 它和别的独立 worker
        之间没有共享通道，列出来只会让模型误以为能联系。
        """
        with self._lock:
            all_workers = list(self._workers.items())
            me = self._workers.get(self_id)
            my_squad = me.squad if me is not None else None
            if my_squad is None:
                # 独立 worker：只列自己。它和别的独立 worker 之间没有共享通道，
                # 列出来只会让模型误以为能联系。
                visible = [(self_id, role_summary(me.system_prompt))] if me else []
            else:
                visible = [
                    (wid, role_summary(w.system_prompt))
                    for wid, w in all_workers
                    if w.squad == my_squad
                ]
            total = len(all_workers)
        return visible, total

    def remove_worker(self, worker_id: str):
        """移除 Worker，并取消它名下在途的任务。

        只摘 dict 不够：worker 线程仍在 _execute 里，既不读 _running 也不读毒丸，
        shutdown() 的 join(timeout=5) 只能白白等满。必须先置取消信号，让它自己
        在下一个检查点退出。
        """
        with self._lock:
            worker = self._workers.pop(worker_id, None)
            pending = [t for t in self._tasks.values()
                       if t.worker_id == worker_id and t.status in ("pending", "running")]
            # 该 worker 所属小队是否还有存活成员。必须在 pop 之后、同一把锁内算，
            # 否则并发 remove 两个 worker 时双方都可能看到「还有别人」而不清黑板。
            # 独立 worker（squad=None）不属于任何队，不参与小队清理。
            squad = worker.squad if worker else None
            squad_alive = (
                squad is not None
                and any(w.squad == squad for w in self._workers.values())
            )
            squad_gone = squad is not None and not squad_alive
        for t in pending:
            _cancel_team_task(t)
            t.status = "interrupted"
        if worker:
            worker.shutdown()
        # 清掉该 worker 的邮箱游标，避免 worker_id 被复用后读到旧进度
        self.mailbox.forget(worker_id)
        # 小队全员已移除：这块黑板再没有读者，连同记录一起清掉。
        # 保留的话，同名 worker 重建时游标从 0 起算，会把上一轮的记录
        # 当成本任务的情报读进来（实测旧 flag 会被新任务吞掉）。
        if squad_gone:
            self.mailbox.purge_squad(squad)

    # ---- 小队成员管理 ----
    # squad 现在是可变的：独立 worker 可以加入小队，小队成员也可以退回独立。
    # 这三个操作只改 squad 归属，不重建 worker —— 它的 _history、线程、
    # 游标都保留，所以「老成员加入新队」时不会丢掉自己积累的上下文。

    def _validate_squad_name(self, squad: Any) -> Optional[str]:
        """校验 squad 名。与 add_worker 用同一套规则，返回错误信息或 None。"""
        if not isinstance(squad, str) or not squad.strip():
            return f"squad 必须是非空字符串，收到 {squad!r}"
        if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in squad):
            return f"squad 不能包含空白或控制字符，收到 {squad!r}"
        if squad == LEADER_ID:
            return f"'{LEADER_ID}' 是 leader 的保留身份，不能用作 squad 名"
        if squad == SOLO_SQUAD:
            return f"'{SOLO_SQUAD}' 是系统保留标签，不能用作 squad 名"
        return None

    def assign_squad(self, worker_id: str, squad: str) -> Dict[str, Any]:
        """把一个 worker 加入指定小队。

        语义：
        - 归属立刻生效：它新写的记录归到该队，也能被队内成员读到
        - 它之前写的记录**归属不变**（squad 是写入时定下的），仍在原来的队
        - 只影响后续记录：不补读任何入队前的内容。可见性仍叠加 task_id 这一层，
          入队后读到的是本队后续记录，以及与本队队友共享同一
          blackboard_task_id 的记录；入队前的内容需要时用 team_read(seq=N) 拉取
        - 已在该队 / 已在别的队都允许，后者等于「转队」

        不动游标：worker 自己在队内的读取进度是全局的，转队后继续沿用。
        worker 只在任务执行期间写黑板（那时 task_id 非 None），因此可重读的
        无作用域记录在真实路径里只有 leader 通告；清游标只会把已读过的旧通告
        重投一遍，没有补读收益，所以这里不做。

        不清 _history：worker 自己积累的上下文与队归属无关。
        """
        err = self._validate_squad_name(squad)
        if err:
            return {"error": err}
        with self._lock:
            w = self._workers.get(worker_id)
            if w is None:
                return {"error": f"Worker '{worker_id}' 不存在",
                        "available": sorted(self._workers)}
            old = w.squad
            w.squad = squad
        return {
            "ok": True, "worker_id": worker_id,
            "from": old, "to": squad,
            "note": ("该 worker 现在能读到本队的黑板记录（队内互通已生效），"
                     "以及本队后续的新记录。入队前的内容不会自动补读，"
                     "需要时用 team_read(seq=N) 按需拉取。"),
        }

    def leave_squad(self, worker_id: str) -> Dict[str, Any]:
        """把一个 worker 移出小队，变回独立 worker。

        它之前写的记录留在原队（写出去的话收不回，队内其他人仍可读），
        它自己立刻读不到队内记录，也不再受队内 stop 影响。
        """
        with self._lock:
            w = self._workers.get(worker_id)
            if w is None:
                return {"error": f"Worker '{worker_id}' 不存在",
                        "available": sorted(self._workers)}
            old = w.squad
            if old is None:
                return {"error": f"Worker '{worker_id}' 已经是独立 worker，不在任何小队"}
            w.squad = None
        return {
            "ok": True, "worker_id": worker_id, "from": old, "to": None,
            "note": "该 worker 现在是独立 worker，与原小队不再互通黑板",
        }

    def disband_squad(self, squad: str) -> Dict[str, Any]:
        """解散一个小队：所有成员变回独立 worker，队内黑板记录清空。

        成员本身不删除 —— 它们的 _history、线程都保留，只是失去队归属。
        与逐个 remove_worker 的区别：disband 保留 worker，remove 连线程一起关。

        target="leader" 的记录保留，与 purge_squad 的行为一致：那是给 leader 的
        上报，不该因为队解散而丢。
        """
        err = self._validate_squad_name(squad)
        if err:
            return {"error": err}
        with self._lock:
            members = [wid for wid, w in self._workers.items() if w.squad == squad]
            for wid in members:
                self._workers[wid].squad = None
            if not members:
                return {"error": f"小队 '{squad}' 不存在或没有成员",
                        "available": sorted({w.squad for w in self._workers.values()
                                             if w.squad is not None})}
        purged = self.mailbox.purge_squad(squad)
        return {
            "ok": True, "squad": squad, "members": members,
            "purged_records": purged,
            "note": "成员已变为独立 worker，队内黑板记录已清空",
        }

    # 黑板写入
    def _current_task_of(self, sender: str) -> Optional[str]:
        """写入者当前正在执行的任务 id；无任务或非 worker 返回 None。

        worker 写黑板时通过 team_message 工具进来，那里只能用 _current_sender()
        拿到名字，拿不到「它正在跑哪个任务」。这里回查 worker 自身的执行态，
        让记录自动归到当前任务名下 —— 调用方不用显式传，也不会漏传。
        """
        w = self._workers.get(sender)
        if w is None:
            return None
        return w._current_task_id

    def post_message(self, sender: str, text: str, kind: str = "progress",
                     target: str = "*",
                     task_id: Optional[str] = None) -> Dict[str, Any]:
        """往黑板写一条记录。不回传「谁该看」——黑板按 squad 天然可见。

        Args:
            sender: 写入者（worker_id 或 "leader"）
            text: 内容
            kind: "progress"（进展）| "conclusion"（结论）| "deadend"（死路）
                  | "stop"（目标达成，同队读到即停手）
            target: "*"（默认，全队可见）
                    "leader"（只给 leader 的上报通道）
                    "<worker_id>"（定向给某个 worker）
                    "<squad 名>"（leader 专用：定向给某个小队全体）
            task_id: 这条记录归属的委派。None（默认）时自动取写入者当前正在
                     执行的任务；leader 写入（或写入者空闲）则归为全局。
                     全局记录同队所有人可见，任务记录只对该任务的参与者可见。

        关键行为：**不唤醒任何 worker**。写入方只留下记录，读者在自己循环的
        检查点（迭代开头 / 工具结果之后）读到。worker 之间没有互相叫醒的通道，
        因此不存在「A 写给 B、B 回给 A」的自激互答。

        唯一例外是 kind="stop"：它需要立刻影响正在跑的任务，但实现方式仍是
        「留标记 + 读者自己发现」，而不是「推送 + 打断」。stop 与普通记录走
        同一套作用域规则：worker 写的是「停我这批人」，leader 写的（task_id
        为 None）才全员生效。
        """
        if not text or not text.strip():
            return {"error": "记录内容不能为空"}
        if kind not in ("progress", "conclusion", "deadend", "stop"):
            return {"error": f"kind 只能是 progress / conclusion / deadend / stop，收到 '{kind}'"}

        with self._lock:
            workers = list(self._workers.values())
            names = set(self._workers.keys())
            squads = {w.squad for w in workers if w.squad is not None}
            sender_worker = self._workers.get(sender)
            sender_squad = sender_worker.squad if sender_worker is not None else None
            sender_is_leader = sender not in names
            target_worker = (self._workers.get(target)
                             if target not in ("*", "leader") else None)
            target_is_squad = target in squads and target_worker is None

        if (target not in ("*", "leader")
                and target not in names
                and not target_is_squad):
            return {"error": f"Worker 或小队 '{target}' 不存在",
                    "available": sorted(names | squads | {"leader", "*"})}

        # 按小队定名只给 leader 用：worker 之间的跨队写入一律拒绝，
        # 否则 squad 隔离形同虚设。
        if target_is_squad and not sender_is_leader:
            return {"error": f"只有 leader 能按小队定向写入，收到 target='{target}'"}

        # 跨队拦截：worker 之间只能写在同一个 squad 里。
        # 独立 worker 没有共享黑板，谁都不能定向写 —— 若只用 `!=` 比较，
        # 两个 None 相等会被放行，写入带上 SOLO_SQUAD 标签后对方永远读不到，
        # 表现为「返回 ok 但信息静默丢失」。
        if not sender_is_leader and target_worker is not None:
            if sender_squad is None or sender_squad != target_worker.squad:
                tgt_squad = target_worker.squad
                if sender_squad is None and tgt_squad is None:
                    reason = "你和目标都是独立 worker，互相之间没有共享黑板。"
                elif sender_squad is None:
                    reason = f"你是独立 worker，'{target}' 在 {tgt_squad} 小队，跨队无法互通。"
                elif tgt_squad is None:
                    reason = f"你在 {sender_squad} 小队，'{target}' 是独立 worker，跨队无法互通。"
                else:
                    reason = f"你在 {sender_squad} 小队，'{target}' 在 {tgt_squad} 小队，跨队无法互通。"
                return {"error": "跨队写入被禁止：" + reason +
                                 "需要让 leader 知道的话，写一条 target='leader' 的记录。"}

        # leader 的写入 squad=None（全员可见的通告）；worker 的写入带自己的 squad。
        # 独立 worker（squad=None）的写入也落成 squad=None，但 _collect 把
        # 「记录 squad=None」当作 leader 通告、对所有人可见。所以要区分：
        # 独立 worker 的写入没有同队读者，给它打上 SOLO_SQUAD 标签，
        # 让它只对 leader 可见 —— 其他独立 worker 也读不到（它们互不互通）。
        # 例外：leader 按小队定向（target 是队名）时，把 squad 置成该队名，
        # 让这条记录只对该队可见 —— 否则 stop 会误伤其他小队。
        original_target = target          # 保留原始意图，下面的统计要用
        post_squad = None if sender_is_leader else sender_squad
        if sender_worker is not None and post_squad is None:
            post_squad = SOLO_SQUAD
        if target_is_squad:
            post_squad = target
            # 队名不是合法的「收件人」，落库时归为广播（可见性由 squad 决定）
            target = "*"

        # stop 与普通记录走同一套作用域规则：worker 写的就是「停我这批人」，
        # leader 写的（sender 不在 worker 表里）天然是全局。不强制 None ——
        # 强制全局会让两个无关任务互相误杀：实测中 A-1 完成任务X 写 stop，
        # 正在跑任务Y 的 A-2 一个 chunk 都没产出就被判 stopped。
        post_task = task_id if task_id is not None else self._current_task_of(sender)

        seq = self.mailbox.post(
            sender=sender,
            text=text,
            kind=kind,
            squad=post_squad,
            target=target,
            task_id=post_task,
        )

        result = {"ok": True, "seq": seq, "kind": kind}
        if post_task is not None:
            # 让调用方知道这条记录落在哪个任务名下（同队别人看不到，除非同任务）
            result["task_id"] = post_task

        if kind == "stop":
            stopped = []
            for w in workers:
                if w.worker_id == sender:
                    # 写 stop 的人自己不停：它正是「我拿下了」的那个
                    continue
                if not sender_is_leader and w.squad != sender_squad:
                    continue          # 跨队不级联
                if target_is_squad and w.squad != original_target:
                    continue          # leader 只停指定小队
                if original_target not in ("*", "leader") and not target_is_squad \
                        and w.worker_id != original_target:
                    continue
                stopped.append(w.worker_id)
                # 不在这里消费空闲 worker 的 stop 标记：
                # 它此刻可能正处于「任务刚结束、尚未提交下一次」的缝隙，
                # 立即消费会让这条 stop 完全失效（实测确实如此）。
                # 陈旧标记的问题另有兜底：_execute 每轮开头清空 _stop_reason，
                # 且 take_stop 只对 sender 之外的人生效。
            result["stop_targets"] = stopped

        return result

    def shutdown(self, worker_id: Optional[str] = None):
        """关闭 Worker。若指定 worker_id 则只关闭该 Worker，否则关闭全部。"""
        if worker_id is not None:
            self.remove_worker(worker_id)
            return
        # 全部关闭时逐个走 remove_worker，取消逻辑只有一份实现
        with self._lock:
            worker_ids = list(self._workers.keys())
        for wid in worker_ids:
            self.remove_worker(wid)


    # ---- 入参校验 ----
    # 模型的 arguments 是不可信输入。实测过的三种畸形值：
    #   tasks=[]            -> ThreadPoolExecutor(max_workers=0) 抛 ValueError 穿到工具层
    #   tasks="修复登录"     -> for 迭代成 4 个单字任务
    #   task_content=None   -> _execute 里 None[:42] 抛 TypeError，异步路径无人写终态
    # 统一在这里拦下：不创建任务、不占 _tasks、不启动线程，只回结构化错误。
    @staticmethod
    def _validate_task_content(task_content: Any) -> Optional[str]:
        """返回错误 JSON；合法时返回 None。"""
        if isinstance(task_content, str):
            if task_content.strip():
                return None
            return json.dumps({"error": "任务内容不能为空"}, ensure_ascii=False)
        return json.dumps({
            "error": f"任务内容必须是字符串，收到 {type(task_content).__name__}",
        }, ensure_ascii=False)

    @staticmethod
    def _validate_stop_scope(stop_scope: Any) -> Optional[str]:
        """校验 stop_scope 取值。写错时直接列出合法值，避免模型反复试错。"""
        if stop_scope in ("global", "delegation"):
            return None
        return json.dumps({
            "error": f"stop_scope 只能是 'global' 或 'delegation'，收到 {stop_scope!r}",
        }, ensure_ascii=False)

    @staticmethod
    def _validate_tasks(tasks: Any, worker_ids: Any = None) -> Optional[str]:
        """校验 delegate_parallel 的 tasks / worker_ids。返回错误 JSON 或 None。"""
        if isinstance(tasks, str):
            return json.dumps({
                "error": f"tasks 需要字符串列表，收到字符串 '{tasks[:20]}'。"
                         f"单个任务请写成 tasks=[\"{tasks[:20]}\"]",
            }, ensure_ascii=False)
        if not isinstance(tasks, (list, tuple)):
            return json.dumps({
                "error": f"tasks 需要字符串列表，收到 {type(tasks).__name__}",
            }, ensure_ascii=False)
        if not tasks:
            return json.dumps({"error": "tasks 不能为空列表"}, ensure_ascii=False)
        for i, t in enumerate(tasks):
            if not isinstance(t, str):
                return json.dumps({
                    "error": f"tasks 第 {i} 项必须是字符串，"
                             f"收到 {type(t).__name__}",
                }, ensure_ascii=False)
        if worker_ids is not None:
            if isinstance(worker_ids, str):
                return json.dumps({
                    "error": f"worker_ids 需要字符串列表，收到字符串 "
                             f"'{worker_ids[:20]}'。请写成 "
                             f"worker_ids=[\"{worker_ids[:20]}\"]",
                }, ensure_ascii=False)
            if not isinstance(worker_ids, (list, tuple)):
                return json.dumps({
                    "error": f"worker_ids 需要字符串列表，收到 "
                             f"{type(worker_ids).__name__}",
                }, ensure_ascii=False)
            if len(worker_ids) != len(tasks):
                return json.dumps({
                    "error": f"tasks（{len(tasks)} 项）与 worker_ids"
                             f"（{len(worker_ids)} 项）数量不匹配",
                }, ensure_ascii=False)
        return None

    # 任务委派
    def delegate_async(
        self,
        task_content: str,
        worker_id: Optional[str] = None,
        stop_scope: str = "global",
        blackboard_task_id: Optional[str] = None,
    ) -> str:
        """提交任务后立刻返回，不等待 worker 跑完。

        与 delegate() 的区别：delegate 阻塞到 worker 结束（主 Agent 被卡在工具
        调用里，最慢的那个 worker 决定整体节奏）；本方法只把任务塞进 worker
        队列就返回 task_id，主 Agent 随后用 wait_any() 收集结果。

        不注册 killer：异步任务的生命周期超出了这次工具调用，若把它绑到
        当前调用的 CallContext，主 Agent 下一次工具超时就会误杀一个正常在跑
        的任务。要中断请用 team_interrupt 语义（interrupt() / remove_worker）。

        Args:
            blackboard_task_id: 黑板作用域 id。多任务传同一个值即可让它们
                互相看到彼此的黑板记录（用于「全队一起攻克」）。None = 用自己的 id，彼此隔离。

        Returns:
            JSON: {"ok": true, "task_id": ..., "worker_id": ..., "worker_busy": ...}
            失败时返回 {"error": ...}
        """
        err = self._validate_task_content(task_content)
        if err is not None:
            return err
        err = self._validate_stop_scope(stop_scope)
        if err is not None:
            return err
        tid = str(uuid.uuid4())
        task = TeamTask(
            id=tid,
            content=task_content,
            worker_id=worker_id,
            stop_scope=stop_scope,
            stop_seq_baseline=self.mailbox.latest_seq(),
            blackboard_task_id=blackboard_task_id or tid,
        )
        # 打上异步标记：wait_any 只收集本轮异步提交的任务，避免把同步
        # delegate / delegate_parallel 的残留条目误报给主 Agent。
        task.async_mode = True

        with self._lock:
            if worker_id:
                w = self._workers.get(worker_id)
                if not w:
                    return json.dumps(
                        {"error": f"Worker '{worker_id}' 不存在"}, ensure_ascii=False)
            else:
                w = self._pick_worker()
                if w is None:
                    return json.dumps({"error": "无可用 Worker"}, ensure_ascii=False)

            task.worker_id = w.worker_id
            self._tasks[task.id] = task
            # 有新任务在途，之前累积的空等待计数作废：
            # 否则熔断会让这一轮合法等待直接被拒。
            self._idle_streak = 0
            # 入队前记录该 worker 是否已经忙：主 Agent 若把第二题压给同一个
            # worker，它会排队；把 busy 回传出去让主 Agent 立刻发现这个误用。
            worker_busy = w.busy

        # submit() 是同步入队（非阻塞）：worker 侧 _task_queue 立即可见，
        # _pick_worker 的 busy 判定依赖这一点，不能挪到别的线程里做。
        w.submit(task)

        return json.dumps({
            "ok": True,
            "task_id": task.id,
            "worker_id": task.worker_id,
            "worker_busy": worker_busy,
        }, ensure_ascii=False)

    def wait_any(
        self,
        task_ids: Optional[List[str]] = None,
        timeout: float = 300,
    ) -> str:
        """阻塞到「任一任务完成」或超时，返回这一批已完成的结果。

        语义是 first-completed（含同时完成的一批），不是等全部结束：
        - 有任务已完成 -> 立即返回，不等其余
        - 全部在跑 -> 最多等 timeout 秒，期间任一完成即返回
        - 超时仍无完成 -> 返回空 completed，主 Agent 可继续做别的动作
          （看 team_status、调整策略），不会卡死

        同一任务只会被上报一次（reported 标记 + 出队），防止主 Agent 拿到
        重复结果后重复委派。

        Args:
            task_ids: 只关心这些任务；None = 所有未上报的异步任务
            timeout: 最长等待秒数（是「最多等多久」，不是「必须等满」）

        Returns:
            JSON: {"completed": [{task_id, worker_id, status, limit_hit,
                   iterations, elapsed, result, error}],
                   "running": [{task_id, worker_id, status, elapsed,
                   iterations, max_iterations}],
                   "timed_out": bool}
        """
        deadline = time.time() + max(0.0, timeout)
        candidates: List[TeamTask] = []
        # 「超时」与「没有在途任务」要区分开：前者说明任务还在跑、应继续等，
        # 后者说明确实无事可做。混在一起会让主 Agent 误判成后者。
        timed_out = False
        idle = False
        running: List[TeamTask] = []

        while True:
            with self._lock:
                pool = [
                    t for t in self._tasks.values()
                    if getattr(t, "async_mode", False) and not t.reported
                ]
                if task_ids:
                    wanted = set(task_ids)
                    pool = [t for t in pool if t.id in wanted]

                done = [t for t in pool if t.is_terminal]
                if done:
                    # 出队 + 标记：既防重复上报，也避免 _tasks 无界增长。
                    for t in done:
                        t.reported = True
                        self._tasks.pop(t.id, None)
                    candidates = done
                    # 上报的同时把仍在跑的也带上：主 Agent 一轮决策既要看
                    # 谁完成了，也要看剩下那几个跑了多久，否则无法判断
                    # 「该继续等」还是「这个 worker 卡死了」。
                    running = [t for t in pool if not t.is_terminal]
                    break

                running = list(pool)

            if not pool:
                idle = True
                break
            if time.time() >= deadline:
                timed_out = True
                break
            # 0.3s 轮询：worker 完成是独立线程写状态，没有事件可订阅；
            # 间隔再短只是空转，再长会让「先完成先处理」的收益变钝。
            time.sleep(0.3)

        completed = []
        for t in candidates:
            completed.append({
                "task_id": t.id,
                "worker_id": t.worker_id,
                "status": t.status,
                "limit_hit": t.limit_hit,
                "stopped": t.stopped,
                "stop_reason": t.stop_reason or None,
                "stopped_by": t.stopped_by or None,
                "iterations": t.iterations,
                "elapsed": round((t.finished_at or time.time()) - t.created_at, 1),
                "result": t.result,
                "error": t.error,
            })

        now = time.time()
        running_out = []
        for t in running:
            # elapsed / iterations / max_iterations 三件一起给：主 Agent 据此
            # 判断这个 worker 是正常推进还是已经卡住，从而避免它凭猜测
            # 直接 remove 掉一个正在干活的 worker。
            running_out.append({
                "task_id": t.id,
                "worker_id": t.worker_id,
                "status": t.status,
                "elapsed": round(now - t.created_at, 1),
                "iterations": t.iterations,
                "max_iterations": getattr(self._workers.get(t.worker_id), "max_iterations", None),
            })

        # 熔断：没有在途任务时本方法零延迟返回，模型若反复调用会几秒内刷爆上下文。
        # 首次空返回仍返回正常结构（带 idle 提示），超过阈值改走 error 分支 ——
        # error 与空 result 的语义差别是这里唯一的阻断手段：空 result 会被读成
        # 「还没好，再等等」，error 则没有这层解读空间。
        if idle:
            with self._lock:
                self._idle_streak += 1
                streak = self._idle_streak
            if streak > WAIT_ANY_IDLE_LIMIT:
                return json.dumps({
                    "error": (
                        f"暂无 worker 在途任务（连续 {streak} 次空等待），"
                        "team_wait_any 已停止响应空转。"
                        "请先 team_delegate_async 派发任务，或结束本轮等待。"
                    ),
                    "idle": True,
                    "completed": [],
                    "running": [],
                    "timed_out": False,
                }, ensure_ascii=False)
        elif completed:
            # 取到结果说明这一轮等待是有意义的，计数归零，
            # 否则此前累积的 streak 会让下一轮正常等待被提前熔断。
            with self._lock:
                self._idle_streak = 0

        return json.dumps({
            "completed": completed,
            "running": running_out,
            "timed_out": timed_out,
            "idle": idle,
            "message": "暂无 worker 在途任务" if idle else None,
        }, ensure_ascii=False, indent=2)

    def delegate(
        self,
        task_content: str,
        worker_id: Optional[str] = None,
        timeout: float = 300,
        stop_scope: str = "global",
        blackboard_task_id: Optional[str] = None,
    ) -> str:
        """
        委派任务给 Worker, 阻塞等待结果。

        Args:
            task_content: 任务描述
            worker_id: 指定 Worker, None = 自动选空闲 Worker
            timeout: 超时秒数
            stop_scope: "global"（默认）任何未消费的 stop 都算，含历史遗留；
                        "delegation" 只认本次委派提交之后写入的 stop，
                        提交前就在黑板上的历史标记不再生效。
                        两者都只认本任务作用域的 stop（或全局 stop），
                        不会跨到别的并行任务上去。
            blackboard_task_id: 黑板作用域 id，多任务传同一个值即可互相看见
                彼此的黑板记录。None = 用自己的 id，与其他任务隔离。

        Returns:
            Worker 的输出结果
        """
        err = self._validate_task_content(task_content)
        if err is not None:
            return err
        err = self._validate_stop_scope(stop_scope)
        if err is not None:
            return err
        tid = str(uuid.uuid4())
        task = TeamTask(
            id=tid,
            content=task_content,
            worker_id=worker_id,
            stop_scope=stop_scope,
            stop_seq_baseline=self.mailbox.latest_seq(),
            blackboard_task_id=blackboard_task_id or tid,
        )

        with self._lock:
            self._tasks[task.id] = task

            if worker_id:
                w = self._workers.get(worker_id)
                if not w:
                    task.status = "failed"
                    task.error = f"Worker '{worker_id}' 不存在"
                    self._tasks.pop(task.id, None)
                    return json.dumps({
                        "status": "failed", "error": task.error,
                        "task_id": task.id,
                    }, ensure_ascii=False)
            else:
                # 自动选择: 找第一个空闲的; 全忙则用 worker 数最少的
                w = self._pick_worker()

            if w is None:
                task.status = "failed"
                task.error = "无可用 Worker"
                self._tasks.pop(task.id, None)
                return json.dumps({
                    "status": "failed", "error": task.error,
                    "task_id": task.id,
                }, ensure_ascii=False)

            task.worker_id = w.worker_id

        # 外层调用超时/中断时，把取消穿透到 worker 线程：
        # worker 是裸 threading.Thread，拿不到本线程的 contextvars，只能显式传 event。
        # 若 worker 已经挂上 call_ctx，一并对它的外部资源执行清理。
        register_killer(
            f"team_delegate({task.worker_id})",
            lambda: _cancel_team_task(task),
        )

        result = w.submit_and_wait(task, timeout=timeout)

        # 终态任务从索引里摘掉：结果已经通过返回值交给 leader，而 status() /
        # interrupt() 只需要在途任务。leaving it here 会让每次委派都留一份
        # 完整 result 在内存里，长驻进程会一直累积。
        # 与写入走同一把锁，避免和 status()/interrupt() 的遍历竞争。
        # 同时清零 wait_any 的空转计数：同步委派是实打实做成了一件事，
        # 不该被算进「连续空等待」，否则它会顶掉后续正常的 wait_any。
        with self._lock:
            self._tasks.pop(task.id, None)
            self._idle_streak = 0

        # 与 delegate_parallel 对齐：单 worker 委派结束后清理状态行残留，
        # 否则 _execute 写入的 completed/failed 记录会滞留到整轮 finalize()。
        # 只清自己：并行多个 delegate 时全清会误删仍在跑的其他 Worker，
        # 在跑者要等下一个进度事件才被写回，表现为状态行数量反复跳变。
        engine = get_output_engine()
        if engine:
            engine.clear_team_worker(task.worker_id)
        _emit_team_update(task.worker_id, None, clear=True)

        return result

    def delegate_parallel(
        self,
        tasks: List[str],
        worker_ids: Optional[List[str]] = None,
        timeout: float = 300,
        stop_scope: str = "global",
        share_task_id: bool = False,
    ) -> str:
        """
        并行委派多个任务, 等待全部完成后返回汇总。

        Args:
            tasks: 任务描述列表
            worker_ids: Worker 列表 (与 tasks 一一对应), None = 自动分配
            timeout: 超时秒数
            stop_scope: "global" 或 "delegation"（默认，只认提交后的 stop）。
                两者都仍受黑板任务作用域约束：别的并行任务的 stop 不会停到本任务。
            share_task_id: 本批任务是否视为同一次委派。True 时全批共用一个
                task_id，参与者之间能读到彼此的黑板记录（用于「全队一起
                攻克同一目标」）；False（默认）时各自独立，互不可见。

        Returns:
            汇总 JSON: {"task_id": "result", ...}
        """
        err = self._validate_tasks(tasks, worker_ids)
        if err is not None:
            return err
        err = self._validate_stop_scope(stop_scope)
        if err is not None:
            return err

        # 本批共用同一个水位快照：这批发出去的 N 个任务被视为「同一轮委派」，
        # 因此彼此之间写入的 stop 互相生效，而批前的历史标记全部无效。
        baseline = self.mailbox.latest_seq()
        # share_task_id=True 时所有任务共享同一个黑板作用域 id，使参与者互相可见；
        # 否则各自独立（blackboard_task_id 默认取 id），彼此隔离。
        shared_scope = str(uuid.uuid4()) if share_task_id else None
        team_tasks = []
        for i, content in enumerate(tasks):
            wid = worker_ids[i] if worker_ids else None
            tid = str(uuid.uuid4())
            team_tasks.append(TeamTask(
                id=tid,
                content=content,
                worker_id=wid,
                stop_scope=stop_scope,
                stop_seq_baseline=baseline,
                blackboard_task_id=shared_scope or tid,
            ))

        with self._lock:
            for t in team_tasks:
                self._tasks[t.id] = t

        # 预注册所有 Worker 到状态行
        engine = get_output_engine()
        for i, t in enumerate(team_tasks):
            wid = worker_ids[i] if worker_ids else (
                list(self._workers.keys())[i % len(self._workers)]
                if self._workers else "unknown"
            )
            snippet = t.content[:42] + "…" if len(t.content) > 42 else t.content
            if engine:
                engine.set_team_worker(wid, { # set 到状态栏
                    "task": snippet, "elapsed": 0, "tool_uses": 0, "status": "running",
                })
            _emit_team_update(wid, {
                "task": snippet, "elapsed": 0, "tool_uses": 0, "status": "running",
            })

        # 提交到线程池
        raw_results: Dict[str, str] = {}
        task_by_id = {t.id: t for t in team_tasks}
        # 不用 with：ThreadPoolExecutor 退出 with 会隐式 shutdown(wait=True)，
        # 只要还有线程没返回，整个 delegate_parallel 就被拖住。改为等待超时时
        # 显式取消对应 worker，线程自己在下一个检查点退出。
        pool = ThreadPoolExecutor(max_workers=len(team_tasks))
        try:
            futures: Dict[str, Future] = {}
            for i, t in enumerate(team_tasks):
                wid = worker_ids[i] if worker_ids else None
                with self._lock:
                    w = self._pick_worker() if wid is None else self._workers.get(wid)
                if w is None:
                    raw_results[t.id] = json.dumps({"error": "无可用的 Worker"})
                    continue
                t.worker_id = w.worker_id
                # 入队必须在调用线程同步完成：_pick_worker 靠队列 qsize 判空闲，
                # 若推迟到 pool 线程再 put，本轮所有 pick 看到的都是"全空闲"，
                # 任务会全部堆给第一个 worker。
                future = w.submit(t)
                register_killer(
                    f"team_delegate_parallel({t.worker_id})",
                    lambda _t=t: _cancel_team_task(_t),
                )
                futures[t.id] = pool.submit(future.result, timeout)

            for task_id, fut in futures.items():
                try:
                    raw_results[task_id] = fut.result(timeout=timeout + 5)
                except TimeoutError as e:
                    # 原 submit_and_wait 超时路径会置 failed 状态；入队改为同步
                    # submit 后超时在这里抛出，补齐等价的状态置位
                    t_failed = task_by_id[task_id]
                    _cancel_team_task(t_failed)
                    t_failed.status = "failed"
                    t_failed.error = f"任务超时 ({timeout}s)"
                    raw_results[task_id] = json.dumps({"error": f"任务超时: {e}"})
                except Exception as e:
                    raw_results[task_id] = json.dumps({"error": str(e)})
        finally:
            pool.shutdown(wait=False)

        # 终态任务从索引里摘掉，理由同 delegate()：结果已经通过返回值交给
        # leader，status()/interrupt() 只需在途任务。
        # 同时清零 wait_any 的空转计数（并行委派同样是实际工作）。
        with self._lock:
            for t in team_tasks:
                self._tasks.pop(t.id, None)
            self._idle_streak = 0

        # 汇总统计 + 清除状态行
        total_tool_uses = 0
        total_elapsed = 0.0
        parsed_results: Dict[str, Dict] = {}
        for task_id, raw in raw_results.items():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and "result" in parsed:
                    total_tool_uses += parsed.get("tool_uses", 0)
                    total_elapsed += parsed.get("elapsed", 0)
                    parsed_results[task_id] = parsed
                else:
                    parsed_results[task_id] = {"result": raw, "worker_id": "?", "status": "unknown"}
            except Exception:
                parsed_results[task_id] = {"result": raw, "worker_id": "?", "status": "unknown"}

        if engine:
            engine.clear_team_workers()
        _emit_team_update(None, None, clear=True)

        summary = f"✓ Team 任务全部完成: {total_tool_uses} tool uses · {total_elapsed:.1f}s"
        if engine:
            engine.queue_output(Text(summary, style="bold white"))
        else:
            # gateway 模式无 OutputEngine，只能走 event_bus 透传到前端
            event_bus.put(UIEvent("TEXT", summary))

        return json.dumps(parsed_results, ensure_ascii=False, indent=2)

    def broadcast(
        self,
        task_content: str,
        timeout: float = 300,
        squad: Optional[str] = None,
    ) -> str:
        """
        把同一任务广播给某个小队的全部成员, 等待全部完成后返回汇总。

        语义是「同一件事派给一队人一起做」，所以参与者共享同一个黑板作用域
        （share_task_id=True）：彼此能看到进展与结论，同批成员的 stop 互相生效。
        stop_scope 用 delegation，避免历史遗留的 stop 秒杀新一批。
        squad 这一层隔离不受影响：作用域共享只放开 task_id 这一层，跨小队仍然
        互相看不到（_collect 里两层独立判定）。

        本方法是**队内专用**，squad 必填。给多个独立 worker 并行派活请用
        delegate_parallel —— 独立 worker 没有共享黑板，广播给它们只是把同一段
        文本复制分发，它们之间既看不到彼此也写不了黑板，用 broadcast 会误导。

        Args:
            squad: 目标小队名。必填；不存在或无成员时报错并给出可用小队。

        Returns:
            {"worker_id": "result", ...}
        """
        if not squad:
            with self._lock:
                squads = sorted({w.squad for w in self._workers.values()
                                 if w.squad is not None})
            return json.dumps({
                "error": (f"team_broadcast 只用于队内广播，必须指定 squad。"
                          f"要给多个独立 worker 并行派活请用 team_delegate_parallel"
                          f"（tasks 传相同内容、worker_ids 传目标列表）；"
                          f"单个派活用 team_delegate / team_delegate_async。"),
                "available": squads,
            }, ensure_ascii=False)

        with self._lock:
            squads = sorted({w.squad for w in self._workers.values()
                             if w.squad is not None})
            worker_ids = [wid for wid, w in self._workers.items()
                          if w.squad == squad]

        if not worker_ids:
            return json.dumps({
                "error": f"小队 '{squad}' 不存在或没有成员",
                "available": squads,
            }, ensure_ascii=False)

        tasks = [task_content for _ in worker_ids]
        # 广播 = 同一件事派给一批人一起做，因此全批共享黑板作用域，让参与者
        # 互相可见、同批 stop 互相生效。squad 隔离是另一层判定，不受影响。
        # stop_scope 用 delegation：只认本次提交后写入的 stop，避免历史遗留
        # 的 leader stop（全局生效）把新一批直接秒杀。
        return self.delegate_parallel(
            tasks, worker_ids, timeout,
            share_task_id=True, stop_scope="delegation",
        )

    # 状态查询
    def status(self) -> str:
        """返回所有 Worker 状态的 JSON。纯观测，不消费任何东西。

        刻意不在这里取走发给 leader 的上报：`team_status` 是高频轮询工具
        （主 Agent 想看一眼谁在忙就会调），而取上报是一次性消费。两者绑在
        一起意味着「看一眼状态」会顺手把上报吃掉且无从恢复 —— 读走即弃。
        所以这里只 peek，给出待取条数；内容由 collect_reports() 取。

        额外给出 idle_seconds / stale：
        - idle_seconds：距上次任务结束的秒数（busy 时为 0）
        - stale：跑过任务、当前空闲、且空闲超过 STALE_IDLE_SECONDS。
          这类 Worker 占着线程和 _history，通信范围又锁在原 squad 里，
          留着容易在后续委派时被误用，提醒主 Agent 该清理了。
        """
        now = time.monotonic()
        with self._lock:
            workers_status = {}
            for wid, w in self._workers.items():
                busy = w.busy
                idle_seconds = 0.0 if busy else round(now - w._last_active_at, 1)
                workers_status[wid] = {
                    "squad": w.squad,
                    "busy": busy,
                    "stats": w.stats,
                    "unread": self.mailbox.unread_count(
                        wid, reader_squad=w.squad,
                        reader_task=w._current_task_id),
                    "idle_seconds": idle_seconds,
                    # 注册后一直没被用到也算 stale：既没产出也没在用，
                    # 留着只占线程和上下文，同样该清理。
                    "stale": (not busy) and idle_seconds >= STALE_IDLE_SECONDS,
                }
        # leader 本身不属于任何小队，用 squad=None 读：能收到所有定向给
        # leader 的消息，以及无归属的广播（leader 自己发的通告）。
        # peek 不推进游标，所以反复调 status 不会吞掉上报。
        pending = self.mailbox.peek_since("leader", reader_squad=None)
        payload = {"workers": workers_status}
        if pending:
            payload["pending_reports"] = len(pending)
            payload["hint"] = (
                f"有 {len(pending)} 条发给 leader 的上报待取，"
                f"用 team_collect 取走（读走即弃）"
            )
        stale_ids = [wid for wid, s in workers_status.items() if s["stale"]]
        if stale_ids:
            payload["stale_hint"] = (
                f"以下 Worker 已空闲超过 {STALE_IDLE_SECONDS}s，"
                f"若该方向已不需要，用 team_remove_worker 清理；"
                f"保留的话注意它们的通信范围仍锁在原 squad 内："
                f"{', '.join(stale_ids)}"
            )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def collect_reports(self) -> List[Dict[str, Any]]:
        """取走所有发给 leader 的上报。读走即弃。

        与 status() 分开：这是个一次性消费动作，调用方应当只在自己确实要
        处理上报时调用，否则内容会随游标推进而永久错过。

        正文超过 PREVIEW_CHARS 时只返回预览 + truncated 标记：上报可能很长
        （整段漏洞分析），主 Agent 的上下文比 worker 更宝贵。需要全文时
        用 team_read seq 拉取 —— 与 worker 侧的预览机制一致。
        """
        out = []
        for m in self.mailbox.read_since("leader", reader_squad=None):
            item: Dict[str, Any] = {
                "seq": m.seq, "from": m.sender, "kind": m.kind,
            }
            text = m.text or ""
            if len(text) > PREVIEW_CHARS:
                item["text"] = text[:PREVIEW_CHARS] + "…"
                item["truncated"] = True
                item["n_chars"] = len(text)
            else:
                item["text"] = text
            out.append(item)
        return out

    # 内部
    def _pick_worker(self) -> Optional[TeamWorker]:
        """选择一个 Worker: 优先空闲, 否则选队列最短的"""
        # 找空闲
        for w in self._workers.values():
            if not w.busy:
                return w
        # 全忙: 选队列最小的
        if not self._workers:
            return None
        return min(self._workers.values(), key=lambda w: w._task_queue.qsize())


# TeamOrchestrator — 一键启动 / 关闭
class TeamOrchestrator:
    """
    团队编排器。对外接口:

        orch = TeamOrchestrator(model_client, config)
        orch.add_worker("reviewer", ...)
        orch.start()
        # ... Leader 的 team_* 工具注册后即可使用 ...
        orch.shutdown()
    """

    def __init__(self, model_client: Any, config: Any):
        self.leader = TeamLeader(model_client, config)
        self._started = False

    def add_worker(
        self,
        worker_id: str,
        system_prompt: str,
        whitelist: Set[str],
        max_iterations: int = 100,
        squad: Optional[str] = None,
    ) -> TeamWorker:
        return self.leader.add_worker(
            worker_id, system_prompt, whitelist, max_iterations, squad=squad,
        )

    def start(self):
        """注册 team_* 工具到全局 registry"""
        self._started = True

    def shutdown(self):
        self.leader.shutdown()

    @property
    def workers(self) -> Dict[str, TeamWorker]:
        return self.leader._workers


# 全局单例 (可选, 供 tool 函数获取 TeamLeader 引用)
_leader: Optional[TeamLeader] = None
_leader_lock = threading.Lock()


def set_leader(leader: TeamLeader):
    global _leader
    with _leader_lock:
        _leader = leader


def get_leader() -> Optional[TeamLeader]:
    return _leader


# 预设团队模板 (可选, 快速构建常用团队)
def create_default_team(model_client, config) -> TeamOrchestrator:
    """
    快速创建一个默认团队:
    - reviewer: 代码审查, 只读 + 静态分析
    - fixer: bug 修复, 读写 + 执行
    - generator: 文件生成, 读写

    三个角色同属 DEFAULT_SQUAD 小队：它们是「多角色审查同一段代码」的
    协作关系，需要通过黑板互通，而不是三个互不相干的独立 worker。

    使用:
        orch = create_default_team(model_client, config)
        orch.start()
        set_leader(orch.leader)
    """
    orch = TeamOrchestrator(model_client, config)

    orch.add_worker(
        worker_id="reviewer",
        system_prompt="""你是代码审查专家。你的职责:
- 阅读并分析代码
- 指出潜在 bug、安全问题、性能瓶颈
- 给出具体的修改建议
- 不要直接修改代码, 只做审查和报告
- 输出格式清晰的审查报告""",
        whitelist={"read_file", "static_analysis", "exec"},
        squad=DEFAULT_SQUAD,
    )

    orch.add_worker(
        worker_id="fixer",
        system_prompt="""你是 bug 修复专家。你的职责:
- 阅读并理解问题代码
- 使用 patch_file 精确修改
- 修改后执行验证
- 只做最小化修改, 不动无关代码""",
        whitelist={"read_file", "patch_file", "exec", "write_file"},
        squad=DEFAULT_SQUAD,
    )

    orch.add_worker(
        worker_id="generator",
        system_prompt="""你是代码生成专家。你的职责:
- 根据需求创建新文件或修改现有文件
- 使用 write_file 创建新文件, patch_file 修改现有文件
- 确保生成的代码风格与项目一致
- 完成后做基本验证""",
        whitelist={"read_file", "write_file", "patch_file", "exec"},
        squad=DEFAULT_SQUAD,
    )

    return orch
