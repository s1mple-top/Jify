# -*- coding: utf-8 -*-
"""Subagent 执行器 — 复用 model_client，同步执行，contextvars 工具隔离，暂时不考虑异步执行"""

import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

from tools.registry import registry, _subagent_whitelist
from tools.call_context import is_cancelled
from context_shed import persist_large_result, shed_tool_outputs

# 线程级 subagent 统计：由 SubagentRunner.run() 写入，工具侧 _print_done 读取
_subagent_stats: Dict[int, Dict] = {}

_CANCELLED_RESULT = "[subagent] 任务已被取消，未执行完。"

# 收到同队成员的 stop 信号时的返回文本。与 _CANCELLED_RESULT 分开：
# 前者是被动中断（用户/超时），后者是主动停手（目标已达成，再跑是浪费）。
_STOPPED_RESULT = (
    "[subagent] 收到同队成员的停止信号，已中止当前任务。"
    "这通常意味着目标已由队友达成，不要重试同一条路径。"
)


_SKIPPED_TOOL_RESULT = "[Skipped — task interrupted before this tool ran]"


def _repair_dangling_tool_calls(messages: List[Dict]) -> int:
    """就地补齐被中断遗留的悬空 tool_calls，返回补齐条数。

    cancel/stop 落在「assistant 消息已 append、本批 tool 结果没写全」的窗口时，
    序列会永久非法：OpenAI 兼容网关要求 assistant.tool_calls 后紧跟等量 tool
    消息，Anthropic 要求每个 tool_use 都有 tool_result，两者都直接 400。
    而这段历史会作为下一任务的上下文再次发送，坏消息不会自行消失。

    只按 tool_call_id 配对，不触碰已有 content。与主 Agent 的
    _repair_dangling_tool_calls 同一策略，区别是这里处理 dict 形态的历史。
    """
    repaired: List[Dict] = []
    missing_total = 0
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        repaired.append(msg)
        i += 1
        if msg.get("role") != "assistant":
            continue
        expected = [
            tc.get("id")
            for tc in (msg.get("tool_calls") or [])
            if isinstance(tc, dict) and tc.get("id")
        ]
        if not expected:
            continue

        # 收集紧随其后的 tool 结果，已应答的 id 记账
        answered = set()
        while i < n and messages[i].get("role") == "tool":
            tm = messages[i]
            if tm.get("tool_call_id"):
                answered.add(tm["tool_call_id"])
            repaired.append(tm)
            i += 1

        for tc_id in expected:
            if tc_id not in answered:
                repaired.append({
                    "role": "tool",
                    "content": _SKIPPED_TOOL_RESULT,
                    "tool_call_id": tc_id,
                })
                missing_total += 1

    if missing_total:
        messages[:] = repaired
    return missing_total


def reset_subagent_stats() -> None:
    """清空全部 subagent 统计残留。

    通常由 _print_done 在跑完后 pop 掉自己那条；被中断的调用没有走到那里，
    会在线程 ident 维度留下死记录。轮次边界（持有 agent_lock，无并发 subagent）
    调用本函数统一收敛。
    """
    _subagent_stats.clear()


class SubagentRunner:
    """
    简化版 AgentLoop，在同一进程内同步执行，通过 contextvars 限制可用工具。

    使用方式:
        runner = SubagentRunner(model_client, config)
        result = runner.run(
            task="审查 xxxx.py",
            system_prompt="你是一个代码审查 subagent...",
            whitelist_schemas=[...],
            whitelist_names={"read_file", "static_analysis"},
            max_iterations=20,
        )
    """

    def __init__(self, model_client: Any, config: Any):
        self.model_client = model_client
        self.config = config

    def run(
        self,
        task: str,
        system_prompt: str,
        whitelist_schemas: List[Dict],
        whitelist_names: Set[str],
        max_iterations: int = 100,
        on_progress: Optional[Callable[[str, Dict], None]] = None,
        interrupt_event: Optional[threading.Event] = None,
        history: Optional[List[Dict]] = None,
        out_history: Optional[List] = None,
        inbox: Optional[Callable[[], List[Dict]]] = None,
        has_inbox: Optional[Callable[[], bool]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        out_meta: Optional[Dict] = None,
    ) -> str:
        """
        执行 subagent 任务并返回最终文本结果。

        Args:
            history: 传入的历史消息（不含 system），用于跨任务保留上下文记忆。
            out_history: 可变列表，任务结束后写入本轮完整消息（不含 system），
                供调用方（如 TeamWorker）保存为下一次的历史。
            inbox: 可选回调，返回「待插入的团队消息」列表（每项是 OpenAI 格式的
                {"role": "user", "content": ...}）。传 None（默认）时行为与之前完全一致。
                传了之后在两处生效：每轮迭代开头插入、流式响应中途触发抢占。
                回调内部应自带消费语义（取走即清空），本函数每轮只调一次。
            has_inbox: 与 inbox 配套的轻量谓词，只判断「有没有待取消息」不做消费。
                流式抢占用它探测；为 None 时退化为不抢占（只在迭代开头插入）。
            should_stop: 可选回调，返回 True 表示「本轮必须终止，不要再继续」。
                与 inbox 的区别是终止而非续跑：命中后直接返回（不重采样、
                不插入消息内容），用于同队成员宣告「目标已达成，停手」。
                两处检查：每轮迭代开头、流式响应每个 chunk。
            out_meta: 可变 dict，结束后写入结构化终态。当前字段：
                limit_hit (bool) -- 是否因打满 max_iterations 而退出
                iterations (int) -- 实际消耗的迭代轮数
                调用方必须通过 out_meta 判断 limit_hit，不能靠匹配返回文本：
                「达到最大迭代次数」的文案是给人看的，改一次就会静默失效。

        Returns:
            最终回复文本；若达到最大迭代次数，返回带提示的文本。
        """
        token = _subagent_whitelist.set(whitelist_names)
        _start = time.time()
        tool_uses = 0
        recv_est = 0
        messages: List[Dict] = []
        # 本线程上一次运行可能残留统计（走非 CLI 预执行路径时无消费者 pop，
        # 或线程 ident 被复用），先清掉，避免读到上一轮的耗时 / token 数
        _subagent_stats.pop(threading.get_ident(), None)
        # 级 0 落盘文件名需要会话标识；subagent 无长期 session，用线程+时刻区分
        session_id = f"subagent-{threading.get_ident()}-{int(_start)}"
        # 必须在 try 之前初始化：finally 会读它，而 try 首行就可能抛异常
        iterations_done = 0
        try:
            messages.append({"role": "system", "content": system_prompt})
            if history:
                messages.extend(history)
            messages.append({"role": "user", "content": task})

            # 与主 Agent 对齐：sent 为累计发送量（本轮新增内容原始字符数累加），
            # 而非当前上下文全量大小；初始上下文不计入 sent。
            sent_est = 0
            if on_progress:
                on_progress("token_update", {"sent": sent_est, "recv": recv_est})

            last_content = ""

            def _record_stats() -> None:
                _subagent_stats[threading.get_ident()] = {
                    "tool_uses": tool_uses,
                    "elapsed": time.time() - _start,
                    "sent_est": sent_est,
                    "recv_est": recv_est,
                }

            def _bail_if_cancelled() -> Optional[str]:
                """取消信号命中时收尾并返回终止文案；未命中返回 None。

                只在自然边界调用：每轮迭代开头、流式响应每个 chunk、每次工具执行前。
                检查点之间的阻塞（单次模型响应、单个长工具）仍需等它自己结束。

                cancel 命中时仍要再查一次 stop：两者可能先后到达，而 stop 携带
                「成果在谁手上」这一唯一线索。若因为 cancel 先置位就直接返回
                CANCELLED，stop 会被永久屏蔽，主 Agent 只看到 interrupted，
                不知道成果已在队友那里，于是把已完成的方向重做一遍。
                """
                if not is_cancelled(interrupt_event):
                    return None
                if should_stop is not None and should_stop():
                    # stop 与 cancel 同时成立时以 stop 为准（与 _execute 的终态
                    # 优先级一致）：stopped 可回收成果，interrupted 不能。
                    _record_stats()
                    if out_meta is not None:
                        out_meta["stopped"] = True
                    return _STOPPED_RESULT
                _record_stats()
                return _CANCELLED_RESULT

            # 抢占次数上限：每条 trigger 消息都重采样一次会白付已生成的 token，
            # 连续被打断时退化回普通排队（等到下一轮迭代开头再插入）。
            max_preempts = 3
            preempt_count = 0

            for _ in range(max_iterations):
                # 迭代轮数由循环内累加（iterations_done 已在 try 之前初始化）
                iterations_done += 1
                # 过程回传：把已消耗轮数同步出去，供调用方在任务仍 running 时
                # 判断「正常推进」还是「卡死」。只在终点回传（out_meta）的话，
                # 运行期间外部读到的永远是初始值 0，等于没有这个指标。
                if on_progress:
                    on_progress("iteration", {"iterations": iterations_done})
                # 中断检查：主 Agent Ctrl+C / 调用超时 / team 取消 都汇总到 is_cancelled
                halted = _bail_if_cancelled()
                if halted is not None:
                    return halted

                # 停止信号检查：同队成员宣告目标达成。与 is_cancelled 分开，
                # 因为两者要落到不同的终态（stopped vs cancelled），
                # 调用方据此决定是「任务成功但被叫停」还是「被动中断」。
                if should_stop is not None and should_stop():
                    _record_stats()
                    if out_meta is not None:
                        out_meta["stopped"] = True
                    return _STOPPED_RESULT

                # 团队消息插入点 1：每轮迭代开头。上一轮产出的 tool_calls 已全部
                # 执行完，此处插入不会破坏 assistant/tool 的配对关系。
                if inbox is not None:
                    incoming = inbox()
                    if incoming:
                        messages.extend(incoming)
                        sent_est += sum(len(m.get("content") or "") for m in incoming)

                raw_response = self.model_client.chat(
                    messages=messages,
                    tool_schemas=whitelist_schemas,
                    model=self.config.model,
                    stream=True,
                )

                content = ""
                tool_call_chunks: Dict[int, Dict] = {}
                stream_cancelled = False
                stream_preempted = False
                stream_stopped = False

                for chunk in raw_response:
                    # 模型流式响应可能持续很久，必须在内层也检查，否则取消要等整段输出完
                    # 先看 stop 再看 cancel：两者同时成立时 stop 的信息量更大
                    # （能定位成果来源），因此不能被先到的 cancel 屏蔽。
                    if should_stop is not None and should_stop():
                        stream_stopped = True
                        break
                    if is_cancelled(interrupt_event):
                        stream_cancelled = True
                        break
                    # 团队消息插入点 2：流式响应中途抢占。只探测不消费 ——
                    # 真正取走留给下一次迭代开头，避免本轮丢弃后消息丢失。
                    if (has_inbox is not None
                            and preempt_count < max_preempts
                            and has_inbox()):
                        stream_preempted = True
                        break
                    if chunk.content:
                        content += chunk.content
                        recv_est += len(chunk.content)
                        if on_progress:
                            on_progress("text", {"text": chunk.content})
                            on_progress("token_update", {"sent": sent_est, "recv": recv_est})

                    if chunk.thinking:
                        # thinking（推理）同样发给模型，按原始字符数计入 recv，与主 Agent 口径一致
                        recv_est += len(chunk.thinking)
                        if on_progress:
                            on_progress("thinking", {"text": chunk.thinking})
                            on_progress("token_update", {"sent": sent_est, "recv": recv_est})

                    if chunk.tool_call_deltas:
                        tool_token_delta = 0
                        for tc in chunk.tool_call_deltas:
                            idx = tc.index
                            if idx not in tool_call_chunks:
                                tool_call_chunks[idx] = {
                                    "id": "",
                                    "function": {"name": "", "arguments": ""},
                                }
                            if tc.id:
                                tool_call_chunks[idx]["id"] = tc.id
                            if tc.name:
                                tool_call_chunks[idx]["function"]["name"] = tc.name
                                tool_token_delta += len(tc.name)
                            if tc.arguments:
                                tool_call_chunks[idx]["function"]["arguments"] += tc.arguments
                                tool_token_delta += len(tc.arguments)
                        # 工具调用的 name/arguments 同样是模型输出，按原始字符数计入 recv，
                        # 与主 Agent 的 _on_tool_token 口径一致；逐 delta 上报会频繁刷新
                        # 状态行，这里每个 chunk 汇总上报一次
                        if tool_token_delta:
                            recv_est += tool_token_delta
                            if on_progress:
                                on_progress("token_update", {"sent": sent_est, "recv": recv_est})

                if stream_cancelled or stream_preempted or stream_stopped:
                    # 显式关闭生成器，立即释放底层 HTTP 连接，不等 GC
                    close = getattr(raw_response, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:
                            pass
                    if stream_stopped:
                        # 停止：整轮丢弃，不重采样。stop 的语义就是「别再跑了」，
                        # 若像抢占那样 continue，worker 会带着消息再想一轮，
                        # 反而违背发出方的意图。
                        _record_stats()
                        if out_meta is not None:
                            out_meta["stopped"] = True
                        return _STOPPED_RESULT
                    if stream_preempted:
                        # 抢占：整轮丢弃（content 与半截 tool_call_chunks 一律不入历史，
                        # 残缺 tool_calls 无法靠 _sanitize 修复语义）。消息留给下一次
                        # 迭代开头插入，然后重新采样。
                        preempt_count += 1
                        continue
                    _record_stats()
                    return _CANCELLED_RESULT

                last_content = content

                # 组装 tool_calls（一次性构建，同时用于 sent_est 更新和后续消息追加）
                tool_calls = [
                    {
                        "id": tool_call_chunks[idx]["id"],
                        "type": "function",
                        "function": {
                            "name": tool_call_chunks[idx]["function"]["name"],
                            "arguments": tool_call_chunks[idx]["function"]["arguments"],
                        },
                    }
                    for idx in sorted(tool_call_chunks.keys())
                ]

                if not tool_calls:
                    _record_stats()
                    return content

                # 上行前把非法 arguments 换成 "{}"：被拒绝执行的调用（见下方 parse_err）
                # 若带着半截串留在历史里，之后每轮请求都会被校验 arguments 的网关 400
                # 拒掉。tool_calls 原对象不动，下面的执行判定仍按原始串拒绝。
                from agent_loop import _sanitize_tool_calls_for_transport
                assistant_msg = {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": _sanitize_tool_calls_for_transport(tool_calls),
                }
                messages.append(assistant_msg)
                # assistant 正文在下一轮会发送给模型，按原始字符数计入 sent（与主 Agent 一致）
                sent_est += len(content)

                for tc in tool_calls:
                    # 逐工具检查：前一个工具可能跑了很久，期间外层已放弃等待
                    halted = _bail_if_cancelled()
                    if halted is not None:
                        return halted

                    tool_name = tc["function"]["name"]
                    try:
                        tool_args = json.loads(tc["function"]["arguments"])
                        parse_err = ""
                    except (json.JSONDecodeError, TypeError) as e:
                        tool_args = {}
                        parse_err = str(e)

                    tool_uses += 1
                    if on_progress:
                        on_progress("tool_start", {"name": tool_name, "args": tool_args})
                    # 参数非法不降级成 {} 执行（等于空参调用有副作用工具），回错误让模型重发
                    result = (json.dumps({"error": f"工具参数不是合法 JSON，已拒绝执行: {parse_err}"},
                                         ensure_ascii=False)
                              if parse_err else registry.dispatch(tool_name, tool_args))
                    if on_progress:
                        on_progress("tool_end", {"name": tool_name, "result": result[:200]})
                    # 级 0 摄入闸门：单条超限即落盘，上下文只留 preview，
                    # 避免巨型 exec 输出「先进上下文再被压缩」
                    content = persist_large_result(
                        session_id,
                        tool_name,
                        tc["id"],
                        result,
                        getattr(self.config, "tool_result_max_chars", 50000),
                        getattr(self.config, "tool_result_preview_chars", 2000),
                    )
                    tool_msg = {
                        "role": "tool",
                        "content": content,
                        "tool_call_id": tc["id"],
                    }
                    messages.append(tool_msg)
                    # 每追加一条 tool 结果消息，按实际进入上下文的字符数计入发送量（与主 Agent 一致）
                    sent_est += len(content)

                # 黑板插入点 2：本批工具全部执行完之后。
                # 必须放在 for tc 循环之外：协议要求 assistant 的 tool_calls 之后
                # 连续跟同样数量的 tool 消息，插在中间会破坏配对（Anthropic 直接 400）。
                # 相比等下一轮迭代开头，这里能让队友的进展更早进入模型上下文。
                if inbox is not None:
                    delta = inbox()
                    if delta:
                        messages.extend(delta)
                        sent_est += sum(len(m.get("content") or "") for m in delta)

                # 级 1 减重：上下文超阈值时清空早期工具结果（零 LLM、零失败风险）。
                # subagent 生命周期短，不做旁路摘要；require_converge=False 让它
                # 不受「清完仍超阈值就别动」限制——没有摘要兜底，能清多少算多少。
                try:
                    shed_tool_outputs(messages, self.config, require_converge=False)
                except Exception:
                    pass

                if on_progress:
                    on_progress("token_update", {"sent": sent_est, "recv": recv_est})

            _record_stats()
            if out_meta is not None:
                out_meta["limit_hit"] = True
                out_meta["iterations"] = iterations_done
            return (
                f"[subagent] 达到最大迭代次数 ({max_iterations})，最后回复:\n{last_content}"
            )
        finally:
            # 中断可能正好落在「assistant 已 append、本批 tool 结果没写全」的窗口
            # （逐工具检查点、工具自身抛错、stop 命中都在这个循环里返回）。
            # 不补齐的话这段历史会作为下一任务的上下文再次发送，网关直接 400，
            # 且坏消息不会自行消失。放在 finally 是因为这里是历史唯一的写出口。
            _repair_dangling_tool_calls(messages)
            if out_meta is not None:
                # 任何返回路径（含正常收敛、取消、异常）都补齐终态。
                # limit_hit 默认 False：只有上面那条兜底分支会置 True，
                # 若已置 True 则保留（finally 在 return 之后执行）。
                # stopped 同理：只在命中 stop 信号时置 True。
                out_meta.setdefault("limit_hit", False)
                out_meta.setdefault("stopped", False)
                out_meta["iterations"] = iterations_done
            if out_history is not None:
                out_history[:] = [m for m in messages if m.get("role") != "system"]
            _subagent_whitelist.reset(token)
