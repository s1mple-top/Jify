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
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from event_bus import event_bus, UIEvent
from rich.text import Text
from subagent import SubagentRunner
from tools.registry import registry, _subagent_whitelist

_output_engine: Any = None


def set_output_engine(engine: Any) -> None:
    global _output_engine
    _output_engine = engine


def get_output_engine() -> Any:
    return _output_engine



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


@dataclass
class WorkerDef:
    """Worker 定义"""
    worker_id: str
    system_prompt: str
    whitelist: Set[str]                        # 可用工具白名单
    max_iterations: int = 20
    current_task: Optional[str] = None         # 当前正在执行的任务 ID
    stats: Dict = field(default_factory=dict)  # {tasks_completed, total_elapsed}



# TeamWorker — 轻量级工作线程
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
        max_iterations: int = 20,
    ):
        self.worker_id = worker_id
        self.model_client = model_client
        self.config = config
        self.system_prompt = system_prompt
        self.whitelist = whitelist
        self.max_iterations = max_iterations

        self._task_queue: queue.Queue = queue.Queue()
        self._result_events: Dict[str, threading.Event] = {}
        self._results: Dict[str, str] = {}
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()
        self._stats = {"tasks_completed": 0, "total_elapsed": 0.0}

        # 预构建 tool schemas
        self._whitelist_schemas: List[Dict] = []

    def _build_progress_callback(self, task_snippet: str, start_time: float, tool_counter: list):
        """构建进度回调：每隔几次工具调用更新 OutputEngine 状态行"""
        engine = get_output_engine()

        def _on_progress(event_type: str, data: Dict) -> None:
            if not engine:
                return
            if event_type == "tool_start":
                tool_counter[0] += 1
            status = "running"
            engine.set_team_worker(self.worker_id, {
                "task": task_snippet,
                "_start": start_time,
                "tool_uses": tool_counter[0],
                "status": status,
            })

        return _on_progress

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
        # 放入一个毒丸任务唤醒线程
        self._task_queue.put(None)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

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
        return future

    def submit_and_wait(self, task: TeamTask, timeout: float = 300) -> str:
        """
        提交任务并阻塞等待结果

        Returns:
            Worker 输出的最终文本
        """
        future = self.submit(task)
        try:
            return future.result(timeout=timeout)
        except Exception as e:
            task.status = "failed"
            task.error = str(e)
            return f"[TeamWorker:{self.worker_id}] 任务失败: {e}"


    # 内部循环
    def _loop(self):
        """Worker 主循环: 从队列取任务 → 执行 → 写回结果"""
        while self._running:
            try:
                item = self._task_queue.get(timeout=1)
            except queue.Empty:
                continue

            if item is None:
                break

            task, future = item
            try:
                result = self._execute(task)
                future.set_result(result)
            except Exception as e:
                future.set_exception(e)

    def _execute(self, task: TeamTask) -> str:
        """核心执行: 调 SubagentRunner.run()"""
        task.status = "running"
        start = time.time()
        tool_counter = [0]

        task_snippet = task.content[:42] + "…" if len(task.content) > 42 else task.content

        engine = get_output_engine()
        if engine:
            engine.set_team_worker(self.worker_id, {
                "task": task_snippet,
                "_start": start,
                "tool_uses": 0,
                "status": "running",
            })

        on_progress = self._build_progress_callback(task_snippet, start, tool_counter)

        runner = SubagentRunner(self.model_client, self.config)
        try:
            result = runner.run(
                task=task.content,
                system_prompt=self.system_prompt,
                whitelist_schemas=self._whitelist_schemas,
                whitelist_names=self.whitelist,
                max_iterations=self.max_iterations,
                on_progress=on_progress, # 状态栏同步进展
            )
            task.status = "completed"
            task.result = result
        except Exception as e:
            task.status = "failed"
            task.error = str(e)
            result = f"[TeamWorker:{self.worker_id}] 异常: {e}"
        finally:
            elapsed = time.time() - start
            task.finished_at = time.time()
            with self._lock:
                self._stats["tasks_completed"] += 1
                self._stats["total_elapsed"] += elapsed

            if engine:
                engine.set_team_worker(self.worker_id, {
                    "task": task_snippet,
                    "_start": start,
                    "tool_uses": tool_counter[0],
                    "status": task.status,
                })

        return json.dumps({
            "worker_id": self.worker_id,
            "status": task.status,
            "result": result,
            "tool_uses": tool_counter[0],
            "elapsed": elapsed,
        }, ensure_ascii=False)

    @property
    def busy(self) -> bool:
        """是否正在执行任务"""
        return self._task_queue.qsize() > 0

    @property
    def stats(self) -> Dict:
        with self._lock:
            return dict(self._stats)



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


    # Worker 管理
    def add_worker(
        self,
        worker_id: str,
        system_prompt: str,
        whitelist: Set[str],
        max_iterations: int = 20,
    ) -> TeamWorker:
        """
        添加一个 Worker 并启动。

        whitelist 示例:
            {"read_file", "patch_file", "exec"}           # bug-fixer
            {"read_file", "static_analysis"}              # code-reviewer
            {"read_file", "write_file", "exec"}           # file-generator
        """
        with self._lock:
            if worker_id in self._workers:
                raise ValueError(f"Worker '{worker_id}' 已存在")

            worker = TeamWorker(
                worker_id=worker_id,
                model_client=self.model_client,
                config=self.config,
                system_prompt=system_prompt,
                whitelist=whitelist,
                max_iterations=max_iterations,
            )
            worker.start()
            self._workers[worker_id] = worker
            return worker

    def remove_worker(self, worker_id: str):
        with self._lock:
            worker = self._workers.pop(worker_id, None)
        if worker:
            worker.shutdown()

    def shutdown(self, worker_id: Optional[str] = None):
        """关闭 Worker。若指定 worker_id 则只关闭该 Worker，否则关闭全部。"""
        if worker_id is not None:
            self.remove_worker(worker_id)
            return
        with self._lock:
            for w in list(self._workers.values()):
                w.shutdown()
            self._workers.clear()


    # 任务委派
    def delegate(
        self,
        task_content: str,
        worker_id: Optional[str] = None,
        timeout: float = 300,
    ) -> str:
        """
        委派任务给 Worker, 阻塞等待结果。

        Args:
            task_content: 任务描述
            worker_id: 指定 Worker, None = 自动选空闲 Worker
            timeout: 超时秒数

        Returns:
            Worker 的输出结果
        """
        task = TeamTask(
            id=str(uuid.uuid4()),
            content=task_content,
            worker_id=worker_id,
        )

        with self._lock:
            self._tasks[task.id] = task

            if worker_id:
                w = self._workers.get(worker_id)
                if not w:
                    return f"[TeamLeader] Worker '{worker_id}' 不存在"
            else:
                # 自动选择: 找第一个空闲的; 全忙则用 worker 数最少的
                w = self._pick_worker()

            if w is None:
                return "[TeamLeader] 无可用 Worker"

            task.worker_id = w.worker_id

        return w.submit_and_wait(task, timeout=timeout)

    def delegate_parallel(
        self,
        tasks: List[str],
        worker_ids: Optional[List[str]] = None,
        timeout: float = 300,
    ) -> str:
        """
        并行委派多个任务, 等待全部完成后返回汇总。

        Args:
            tasks: 任务描述列表
            worker_ids: Worker 列表 (与 tasks 一一对应), None = 自动分配

        Returns:
            汇总 JSON: {"task_id": "result", ...}
        """
        if worker_ids and len(worker_ids) != len(tasks):
            return "[TeamLeader] tasks 与 worker_ids 数量不匹配"

        team_tasks = []
        for i, content in enumerate(tasks):
            wid = worker_ids[i] if worker_ids else None
            team_tasks.append(TeamTask(
                id=str(uuid.uuid4()),
                content=content,
                worker_id=wid,
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

        # 提交到线程池
        raw_results: Dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=len(team_tasks)) as pool:
            futures: Dict[str, Future] = {}
            for i, t in enumerate(team_tasks):
                wid = worker_ids[i] if worker_ids else None
                with self._lock:
                    w = self._pick_worker() if wid is None else self._workers.get(wid)
                if w is None:
                    raw_results[t.id] = json.dumps({"error": "无可用的 Worker"})
                    continue
                t.worker_id = w.worker_id
                futures[t.id] = pool.submit(w.submit_and_wait, t, timeout)

            for task_id, fut in futures.items():
                try:
                    raw_results[task_id] = fut.result(timeout=timeout + 5)
                except Exception as e:
                    raw_results[task_id] = json.dumps({"error": str(e)})

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

        summary = f"✓ Team 任务全部完成: {total_tool_uses} tool uses · {total_elapsed:.1f}s"
        event_bus.put(UIEvent("TEXT", summary))
        if engine:
            engine.queue_output(Text(summary, style="bold white"))

        return json.dumps(parsed_results, ensure_ascii=False, indent=2)

    def broadcast(
        self,
        task_content: str,
        timeout: float = 300,
    ) -> str:
        """
        广播同一任务给所有 Worker, 等待全部完成后返回汇总。

        Returns:
            {"worker_id": "result", ...}
        """
        with self._lock:
            worker_ids = list(self._workers.keys())

        if not worker_ids:
            return "[TeamLeader] 无 Worker"

        tasks = [task_content for _ in worker_ids]
        return self.delegate_parallel(tasks, worker_ids, timeout)

    # 状态查询
    def status(self) -> str:
        """返回所有 Worker 状态的 JSON"""
        with self._lock:
            workers_status = {}
            for wid, w in self._workers.items():
                workers_status[wid] = {
                    "busy": w.busy,
                    "stats": w.stats,
                }
        return json.dumps(workers_status, ensure_ascii=False, indent=2)

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
        max_iterations: int = 20,
    ) -> TeamWorker:
        return self.leader.add_worker(worker_id, system_prompt, whitelist, max_iterations)

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
    )

    orch.add_worker(
        worker_id="fixer",
        system_prompt="""你是 bug 修复专家。你的职责:
- 阅读并理解问题代码
- 使用 patch_file 精确修改
- 修改后执行验证
- 只做最小化修改, 不动无关代码""",
        whitelist={"read_file", "patch_file", "exec", "write_file"},
    )

    orch.add_worker(
        worker_id="generator",
        system_prompt="""你是代码生成专家。你的职责:
- 根据需求创建新文件或修改现有文件
- 使用 write_file 创建新文件, patch_file 修改现有文件
- 确保生成的代码风格与项目一致
- 完成后做基本验证""",
        whitelist={"read_file", "write_file", "patch_file", "exec"},
    )

    return orch
