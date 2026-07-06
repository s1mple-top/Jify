#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_p2p JifyP能力
"""

import socket
import os
import threading
import time
import json
import struct
import argparse
import tempfile
import glob
import readline
import queue
import uuid as uuid_module
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from typing import Optional, Any, Dict, Callable

from event_bus import event_bus, UIEvent


# 帧协议：4 字节大端长度前缀 + payload
HEADER_SIZE = 4  # uint32 big-endian
RECV_BUFFER_MAX_SIZE = 16 * 1024 * 1024  # 16MB，防止恶意/异常 peer 撑爆内存
RECV_BUFFER_IDLE_TIMEOUT = 30  # 30秒无新数据则清理半帧缓冲


def pack_frame(payload: bytes) -> bytes:
    """给 payload 加上 4 字节大端长度头，返回完整帧。"""
    return struct.pack('>I', len(payload)) + payload


def send_frame(sock: socket.socket, payload: bytes) -> None:
    """发送一个完整帧（长度头 + payload）。"""
    sock.sendall(pack_frame(payload))


def parse_frames(buffer: bytearray):
    """从缓冲区中提取完整帧。返回 (frames: list[bytes], remaining: bytearray)。

    缓冲区中可能包含 0 个、1 个或多个完整帧。
    不完整的帧数据保留在 remaining 中。
    """
    frames = []
    while len(buffer) >= HEADER_SIZE:
        payload_len = struct.unpack('>I', buffer[:HEADER_SIZE])[0]
        total_len = HEADER_SIZE + payload_len
        if len(buffer) < total_len:
            break  # 帧不完整，等更多数据
        frame = bytes(buffer[HEADER_SIZE:total_len])
        frames.append(frame)
        del buffer[:total_len]
    return frames, buffer


# 线程本地存储：标记当前是否正在处理 P2P 请求
_p2p_tls = threading.local()


# 约定存放所有智能体socket的目录（所有智能体共享）
AGENTS_DIR = os.path.join(tempfile.gettempdir(), "agents")
os.makedirs(AGENTS_DIR, exist_ok=True)

def get_socket_path(name):
    return os.path.join(AGENTS_DIR, f"agent_{name}.sock")


@dataclass
class P2PMessage:
    """P2P 消息结构 v2 - 支持结构化上下文与对话关联 - 协议面向任务型"""
    # 必填：身份与路由
    id: str                            # uuid4，全局唯一
    sender: str                        # 发送方名称
    type: str                          # "task" | "result" | "error" | "heartbeat"
    content: Any                       # 消息体

    # 可选：对话关联
    target: Optional[str] = None       # None = 广播
    conversation_id: Optional[str] = None  # 对话 ID
    reply_to: Optional[str] = None     # 回复哪条消息的 ID

    # 可选：富上下文
    context: Optional[Dict] = field(default_factory=dict)

    # 可选：元数据
    timestamp: float = field(default_factory=time.time)
    ttl: Optional[int] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "P2PMessage":
        data = json.loads(raw)
        return cls(**data)


def build_prompt_from_message(p2p_msg: P2PMessage) -> str:
    """将结构化 P2P 消息转为 LLM 可理解的 prompt 文本"""
    parts = []

    parts.append("发送方: {}".format(p2p_msg.sender))
    if p2p_msg.reply_to:
        parts.append("(这是对消息 {}... 的回复)".format(p2p_msg.reply_to[:8]))
    parts.append("")

    ctx = p2p_msg.context or {}
    if ctx.get("history"):
        parts.append("--- 对话历史 ---")
        for h in ctx["history"][-5:]:
            parts.append("[{}]: {}".format(h.get("role", "?"), h.get("content", "")))
        parts.append("--- 历史结束 ---")
        parts.append("")

    task = ctx.get("task", {})
    if task.get("intent"):
        parts.append("任务意图: {}".format(task["intent"]))
    if task.get("files"):
        parts.append("关联文件: {}".format(", ".join(task["files"])))

    caller = ctx.get("caller_info", {})
    if caller.get("agent_name"):
        parts.append("调用方: {}".format(caller["agent_name"]))
        if caller.get("capabilities"):
            parts.append("调用方能力: {}".format(", ".join(caller["capabilities"])))

    if len(parts) > 0:
        parts.append("")

    parts.append("请求内容: {}".format(p2p_msg.content))

    parts.append("")
    parts.append("--- ⚠️ P2P 协作协议（必须遵守）---")
    parts.append("你的文字回复将被自动发送给调用方 {}，请直接输出任务结果。".format(p2p_msg.sender))
    parts.append("禁止使用 p2p_send 向调用方发送确认、感谢或追问——你的文字回复就是最终交付物。")
    parts.append("完成任务后，直接输出结果文本即可，不需要额外的确认步骤。")

    return "\n".join(parts)


class AutoAgent:
    def __init__(self, my_name):
        self.my_name = my_name
        self.my_addr = get_socket_path(my_name)
        self.peers = {}          # {peer_name: socket}
        self.lock = threading.Lock()
        self.running = True
        self._agent_loop = None  # 外部注入的 AgentLoop 实例

        # 心跳参数
        self.heartbeat_interval = 5   # 每5秒发一次ping
        self.heartbeat_timeout = 8    # 8秒未收到pong则判定死亡
        self.last_recv_time = {}      # {peer_name: last_recv_time}

        # 接收缓冲区：{peer_name: bytearray}，用于帧协议解析
        self.recv_buffers = {}
        # 接收缓冲区最后追加时间：{peer_name: timestamp}，用于空闲超时清理
        self._buffer_last_append = {}

        # 异步处理队列与线程池
        self.reply_queue = queue.Queue()
        self.executor = ThreadPoolExecutor(max_workers=4)  # 处理请求的线程池

        # 发送队列：串行化对每个 peer 的发送，避免并发写入同一 socket
        self.send_queues = {}           # {peer_name: queue.Queue}
        self.send_queues_lock = threading.Lock()

        # 待回复跟踪：{conversation_id: {"event": threading.Event, "response": {}}}
        self._pending_replies = {}
        self._pending_replies_lock = threading.Lock()

        # 异步回复存储：{task_id: {"event": threading.Event, "response": str}}
        self._async_replies = {}
        self._async_replies_lock = threading.Lock()

        self._passive_peers: set = set()

    def start(self):
        # 清理可能残留的旧socket文件
        try:
            os.unlink(self.my_addr)
        except OSError:
            pass

        # 探测其他 sock 文件是否存活，清理死连接残留
        self._cleanup_stale_sockets()

        # 创建监听socket
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.my_addr)
        self.listener.listen(10)
        # print("[{}] 监听 {}".format(self.my_name, self.my_addr))

        # 启动各个后台线程
        threading.Thread(target=self.accept_connections, daemon=True).start()
        threading.Thread(target=self.scan_peers, daemon=True).start()
        # threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        threading.Thread(target=self.handle_receive, daemon=True).start()
        # threading.Thread(target=self.reply_worker, daemon=True).start()

        # 主线程处理用户输入
        # self.input_loop()


    def get_all_peer_names(self):
        """扫描约定目录，返回除自己以外的所有智能体名字"""
        pattern = os.path.join(AGENTS_DIR, "agent_*.sock")
        # print(pattern)
        paths = glob.glob(pattern)
        names = []
        for path in paths:
            base = os.path.basename(path)
            if base.startswith("agent_") and base.endswith(".sock"):
                name = base[6:-5]   # 去掉前缀和后缀
                if name != self.my_name:
                    names.append(name)
        return names

    def _cleanup_stale_sockets(self):
        """扫描 AGENTS_DIR 下所有 sock 文件，发 ping 探测存活，无 pong 回应则删除残留。

        仅在 start() 中调用一次，用于清理 Jify 非正常退出遗留的死 sock 文件。
        """
        pattern = os.path.join(AGENTS_DIR, "agent_*.sock")
        for path in glob.glob(pattern):
            base = os.path.basename(path)
            if not (base.startswith("agent_") and base.endswith(".sock")):
                continue
            name = base[6:-5]
            if name == self.my_name:
                continue

            sock = None
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(2)
                sock.connect(path)

                ping = P2PMessage(
                    id=str(uuid_module.uuid4()),
                    sender=self.my_name,
                    type="ping",
                    content="",
                )
                send_frame(sock, ping.to_json().encode())

                # 等待 pong
                sock.settimeout(2)
                buffer = bytearray()
                got_pong = False
                deadline = time.time() + 2
                while time.time() < deadline:
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    buffer.extend(chunk)
                    frames, buffer = parse_frames(buffer)
                    for raw in frames:
                        try:
                            msg = P2PMessage.from_json(raw.decode())
                            if msg.type == "pong":
                                got_pong = True
                                break
                        except Exception:
                            pass
                    if got_pong:
                        break

                if not got_pong:
                    raise Exception("no pong response")
            except Exception:
                # 无回应或连接失败，删除残留 sock 文件
                try:
                    os.unlink(path)
                except OSError:
                    pass
            finally:
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass

    def connect_to_peer(self, peer_name):
        """主动连接一个对端，若成功则加入peers字典"""
        peer_addr = get_socket_path(peer_name)
        # 重试3次，每次间隔0.5秒
        for attempt in range(3):
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(3)
                sock.connect(peer_addr)
                sock.settimeout(None)   # 恢复阻塞模式
                with self.lock:
                    if peer_name not in self.peers:
                        self.peers[peer_name] = sock
                        self.last_recv_time[peer_name] = time.time()
                    else:
                        sock.close()
                        return True  # 已有连接，无需重试
                # 发送欢迎消息，接收方通过此消息识别本 agent 的身份
                welcome = P2PMessage(
                    id=str(uuid_module.uuid4()),
                    sender=self.my_name,
                    type="system",
                    content="{} 已加入".format(self.my_name),
                )
                try:
                    send_frame(sock, welcome.to_json().encode())
                except:
                    pass
                return True
            except Exception:
                if attempt < 2:
                    time.sleep(0.5)
                continue
        return False

    def _get_send_queue(self, peer_name):
        """获取或创建指定 peer 的发送队列"""
        with self.send_queues_lock:
            if peer_name not in self.send_queues:
                self.send_queues[peer_name] = queue.Queue()
            return self.send_queues[peer_name]

    def _enqueue_send(self, peer_name, data):
        """将发送任务加入队列，由后台 worker 执行"""
        q = self._get_send_queue(peer_name)
        q.put(data)
        # 确保有 worker 在处理这个队列
        with self.send_queues_lock:
            if not hasattr(self, '_send_workers'):
                self._send_workers = {}
            if peer_name not in self._send_workers or not self._send_workers[peer_name].is_alive():
                t = threading.Thread(target=self._send_worker, args=(peer_name,), daemon=True)
                self._send_workers[peer_name] = t
                t.start()

    def _send_worker(self, peer_name):
        """后台 worker，串行处理对单个 peer 的发送"""
        q = self._get_send_queue(peer_name)
        while self.running:
            try:
                data = q.get(timeout=1)
                self._do_send(peer_name, data)
                q.task_done()
            except queue.Empty:
                continue
            except Exception:
                pass

    def _do_send(self, peer_name, data):
        """实际执行发送，带锁保护"""
        with self.lock:
            sock = self.peers.get(peer_name)
            if sock is None:
                print("[{}] 发送失败，智能体 {} 不在线".format(self.my_name, peer_name))
                return False
            # 整个发送过程持有锁，避免竞态
            try:
                send_frame(sock, data.encode())
                return True
            except Exception as e:
                print("[{}] 发送失败给 {}: {}".format(self.my_name, peer_name, e))
                self._remove_peer_locked(peer_name, sock)
                return False

    def scan_peers(self):
        """定期扫描目录，发现新智能体并主动连接；检测消失的智能体并断开"""
        known_peers = set()
        while self.running:
            current_peers = set(self.get_all_peer_names())
            # 新出现的 -> 连接
            for peer in current_peers - known_peers:
                self.connect_to_peer(peer)
            # # 检查已连接但可能断开的 peer，尝试重连
            # with self.lock:
            #     for peer in list(self.peers.keys()):
            #         sock = self.peers[peer]
            #         # 检测 socket 是否已断开（通过非阻塞 recv）
            #         try:
            #             sock.settimeout(0.01)
            #             data = sock.recv(4096, socket.MSG_DONTWAIT)
            #             sock.settimeout(None)
            #             if data:
            #                 # 收到数据，说明对方还活着，放入待处理（实际由 handle_receive 处理）
            #                 pass
            #         except socket.timeout:
            #             sock.settimeout(None)
            #             # 超时不代表断开，继续保持连接
            #             continue
            #         except (OSError, Exception):
            #             # socket 已断开或出错，移除
            #             # sock.close()
            #             # del self.peers[peer]
            #             # if peer in self.last_recv_time:
            #             #     del self.last_recv_time[peer]
            #             # print("[{}] 检测到智能体 {} 连接已断开".format(self.my_name, peer))
            #             # # 尝试重连
            #             # self.connect_to_peer(peer)
            #             continue
            # 消失的（文件已删除）-> 断开连接
            with self.lock:
                for peer in list(self.peers.keys()):
                    if peer not in current_peers:
                        # 跳过被动接入的纯客户端（无 socket 文件），否则 reply 会静默丢失
                        if peer in self._passive_peers:
                            continue
                        if peer in self.peers:
                            self.peers[peer].close()
                            del self.peers[peer]
                        if peer in self.last_recv_time:
                            del self.last_recv_time[peer]
                        # 清理该 peer 的相关资源
                        self.recv_buffers.pop(peer, None)
                        self._buffer_last_append.pop(peer, None)
                        with self.send_queues_lock:
                            self.send_queues.pop(peer, None)
            known_peers = current_peers
            time.sleep(1)

    def accept_connections(self):
        """接受其他智能体发起的连接，并从第一条消息中提取对方名字"""
        while self.running:
            try:
                conn, _ = self.listener.accept()
                # 设置短超时以接收第一条消息（注册消息）
                conn.settimeout(3)
                buffer = bytearray()
                data = None
                try:
                    while True:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        buffer.extend(chunk)
                        frames, buffer = parse_frames(buffer)
                        if frames:
                            data = frames[0]
                            break
                except socket.timeout:
                    pass
                conn.settimeout(None)
                if not data:
                    conn.close()
                    continue
                # 尝试解析为 P2PMessage，兼容旧格式裸 dict
                try:
                    p2p_msg = P2PMessage.from_json(data.decode())
                    sender = p2p_msg.sender
                except Exception:
                    msg = json.loads(data.decode())
                    sender = msg.get("sender")
                if sender and sender != self.my_name:
                    with self.lock:
                        if sender not in self.peers:
                            self.peers[sender] = conn
                            self.last_recv_time[sender] = time.time()
                            self._passive_peers.add(sender)
                        else:
                            conn.close()   # 已有连接，关闭多余连接
                            continue
                    # 把 accept 阶段读取到的剩余帧（如紧跟在 welcome 后面的 task）重新打包
                    # 注入 recv_buffers，由 handle_receive 统一处理。
                    if len(frames) > 1:
                        import struct as _struct
                        for f in frames[1:]:
                            prefix = _struct.pack('>I', len(f))
                            buffer = bytearray(prefix) + bytearray(f) + buffer
                    if buffer:
                        self.recv_buffers[sender] = buffer
                        self._buffer_last_append[sender] = time.time()
                else:
                    conn.close()
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print("[{}] accept error: {}".format(self.my_name, e))
                break

    # 暂时不启用
    # def send_to_all(self, content):
    #     """广播消息给所有已连接的对端"""
    #     msg = json.dumps({
    #         "type": "chat",
    #         "sender": self.my_name,
    #         "content": content,
    #         "timestamp": time.time()
    #     })
    #     with self.lock:
    #         for peer, sock in list(self.peers.items()):
    #             try:
    #                 sock.sendall(msg.encode())
    #             except Exception:
    #                 sock.close()
    #                 del self.peers[peer]
    #                 if peer in self.last_recv_time:
    #                     del self.last_recv_time[peer]
    #                 # print("[{}] 断开与 {} 的连接（发送失败）".format(self.my_name, peer))

    def handle_receive(self):
        """轮询所有已连接socket，接收消息并处理（帧协议：长度前缀 + payload）

        同时处理 recv_buffers 中由 accept_connections 注入的预读数据，
        确保即使 recv 暂时返回空，已缓冲的帧也不会被忽略。
        """
        _last_cleanup = time.time()
        while self.running:
            now = time.time()
            with self.lock:
                peers_copy = list(self.peers.items())
            for peer, sock in peers_copy:
                try:
                    sock.settimeout(0.1)
                    chunk = sock.recv(4096)
                    sock.settimeout(None)
                except socket.timeout:
                    chunk = b''
                except Exception:
                    self._remove_peer(peer)
                    continue

                # 合并 recv 新数据与 accept_connections 注入的预读缓冲
                buf = self.recv_buffers.get(peer)
                if chunk:
                    if buf is None:
                        buf = bytearray()
                        self.recv_buffers[peer] = buf
                    buf.extend(chunk)
                    self._buffer_last_append[peer] = now

                if buf is None or len(buf) == 0:
                    continue

                # 大小上限防御：超过限制则断开该 peer
                if len(buf) > RECV_BUFFER_MAX_SIZE:
                    self._remove_peer(peer)
                    continue

                # 尝试从缓冲区中提取完整帧
                frames, self.recv_buffers[peer] = parse_frames(buf)

                # 更新该对端的最后收包时间
                with self.lock:
                    if peer in self.last_recv_time:
                        self.last_recv_time[peer] = now

                for raw in frames:
                    self._process_one_frame(peer, sock, raw)

            # 周期性清理空闲的半帧缓冲区（每 10 秒一次）
            if now - _last_cleanup > 10:
                self._cleanup_idle_buffers(now)
                _last_cleanup = now

            time.sleep(0.1)

    def _process_one_frame(self, peer, sock, raw: bytes):
        """处理一个完整的帧（已去除长度头，raw 是 payload 字节）

        统一使用 P2PMessage 解析，兼容旧格式裸 dict。
        """
        try:
            p2p_msg = P2PMessage.from_json(raw.decode())
        except Exception:
            # 兼容旧格式裸 dict
            try:
                msg = json.loads(raw.decode())
            except Exception:
                return
            p2p_msg = P2PMessage(
                id=msg.get("id", str(uuid_module.uuid4())),
                sender=msg.get("sender", peer),
                type=msg.get("type", ""),
                content=msg.get("content", ""),
                conversation_id=msg.get("conversation_id"),
                reply_to=msg.get("reply_to"),
                context=msg.get("context", {}),
            )

        msg_type = p2p_msg.type

        # ping / pong
        if msg_type == "ping":
            pong = P2PMessage(
                id=str(uuid_module.uuid4()),
                sender=self.my_name,
                type="pong",
                content="",
            )
            try:
                send_frame(sock, pong.to_json().encode())
            except:
                self._remove_peer(peer)
        elif msg_type == "pong":
            pass

        # task / chat（触发 agent 处理）
        elif msg_type in ("chat", "task"):
            # 显示收到的消息
            event_bus.put(UIEvent("TEXT",
                "\n[{}]: {}\n[你]: ".format(p2p_msg.sender, p2p_msg.content)))

            # 异步处理请求
            self.executor.submit(self._handle_request, p2p_msg)

        # result（回复通知）
        elif msg_type == "result":
            conv_id = p2p_msg.conversation_id
            if conv_id:
                with self._pending_replies_lock:
                    pending = self._pending_replies.get(conv_id)
                if pending:
                    pending["response"]["response"] = p2p_msg.content or ""
                    pending["event"].set()
            event_bus.put(UIEvent("TEXT",
                "\n[结果-{}]: {}\n".format(p2p_msg.sender, p2p_msg.content)))

        # error
        elif msg_type == "error":
            conv_id = p2p_msg.conversation_id
            if conv_id:
                with self._pending_replies_lock:
                    pending = self._pending_replies.get(conv_id)
                if pending:
                    pending["response"]["response"] = "[错误] " + (p2p_msg.content or "")
                    pending["event"].set()

        # # system
        # elif msg_type == "system":
        #     pass

    def _handle_request(self, p2p_msg):
        """在线程池中执行智能体核心处理逻辑，处理完后发送回复"""
        # 忙碌检查：若当前正忙且是 task/chat 请求，直接回复忙，不放入处理队列
        # 这里会有一个极小的bug，因为is_p2p_busy是在从队列拿消息之后set，如果极端情况下有多个p2p请求过来，依然会全部塞入队列里
        if is_p2p_busy() and p2p_msg.type in ("task", "chat"):
            if p2p_msg.sender and p2p_msg.conversation_id:
                busy_reply = P2PMessage(
                    id=str(uuid_module.uuid4()),
                    sender=self.my_name,
                    target=p2p_msg.sender,
                    type="error",
                    content="当前智能体正忙，请稍后再试",
                    conversation_id=p2p_msg.conversation_id,
                    reply_to=p2p_msg.id,
                )
                self._enqueue_send(p2p_msg.sender, busy_reply.to_json())
            return

        # 优先走外部注入的处理器（CLI 注册的 handler）
        handler = get_request_handler()
        if handler is not None:
            try:
                response = handler(p2p_msg) # 放入队列等待执行，由app里的消费线程去消费
            except Exception as e:
                response = f"[处理异常] {e}"

            if response and p2p_msg.sender and p2p_msg.conversation_id:
                reply_type = "error" if response.startswith("[处理异常]") else "result"
                reply = P2PMessage(
                    id=str(uuid_module.uuid4()),
                    sender=self.my_name,
                    target=p2p_msg.sender,
                    type=reply_type,
                    content=response,
                    conversation_id=p2p_msg.conversation_id,
                    reply_to=p2p_msg.id,
                )
                self._enqueue_send(p2p_msg.sender, reply.to_json())

            # 后处理回调：回复已发送，触发任务续接
            post_cb = get_and_clear_post_handler_callback()
            if post_cb:
                try:
                    post_cb()
                except Exception:
                    pass
            return

        # 回退：默认处理逻辑
        _p2p_tls.is_processing = True
        _p2p_tls.request_sender = p2p_msg.sender
        try:
            response = self.process_request(p2p_msg)

            if response and p2p_msg.sender and p2p_msg.conversation_id:
                reply = P2PMessage(
                    id=str(uuid_module.uuid4()),
                    sender=self.my_name,
                    target=p2p_msg.sender,
                    type="result", # 直接声明结果
                    content=response,
                    conversation_id=p2p_msg.conversation_id,
                    reply_to=p2p_msg.id,
                )
                self._enqueue_send(p2p_msg.sender, reply.to_json())

        except Exception as e:
            if p2p_msg.sender and p2p_msg.conversation_id:
                error_reply = P2PMessage(
                    id=str(uuid_module.uuid4()),
                    sender=self.my_name,
                    target=p2p_msg.sender,
                    type="error",
                    content=str(e),
                    conversation_id=p2p_msg.conversation_id,
                    reply_to=p2p_msg.id,
                )
                self._enqueue_send(p2p_msg.sender, error_reply.to_json())
        finally:
            _p2p_tls.is_processing = False
            _p2p_tls.request_sender = None

    def _build_prompt_from_message(self, p2p_msg: P2PMessage) -> str:
        return build_prompt_from_message(p2p_msg)

    def process_request(self, p2p_msg):
        """
          智能体核心处理函数（可重写或替换）
          参数: p2p_msg - P2PMessage 对象
          返回: 回复字符串

          默认行为需要外部注入的 AgentLoop 实例。
          如果未调用 set_agent_loop()，则抛出错误。
        """
        if self._agent_loop is None:
            raise RuntimeError(
                "P2PManager.process_request 需要绑定 AgentLoop 实例，"
                "请先调用 P2PManager.set_agent_loop(agent_loop)。"
                "CLI 环境下由 set_request_handler 接管，无需调用此方法。"
            )
        prompt = self._build_prompt_from_message(p2p_msg)
        result = self._agent_loop.chat(prompt, urgent=False, wait=True)
        return result
    # def reply_worker(self):
    #     """后台线程，监控回复队列并发送回复"""
    #     while self.running:
    #         try:
    #             item = self.reply_queue.get(timeout=1)
    #             target = item["target"]
    #             response = item["response"]
    #             self.send_reply(target, response)
    #         except queue.Empty:
    #             continue
    #         except Exception as e:
    #             # print("[错误] 回复worker异常: {}".format(e))
    #             continue

    # def send_reply(self, target_name, content):
    #     """专门用于回复消息（自动标记 private）"""
    #     msg = json.dumps({
    #         "type": "chat",
    #         "sender": self.my_name,
    #         "content": content,
    #         "timestamp": time.time(),
    #         "private": True
    #     })
    #     self._enqueue_send(target_name, msg)

    def _remove_peer(self, peer):
        with self.lock:
            if peer in self.peers:
                self.peers[peer].close()
                del self.peers[peer]
            if peer in self.last_recv_time:
                del self.last_recv_time[peer]
            self._passive_peers.discard(peer)
        self.recv_buffers.pop(peer, None)
        self._buffer_last_append.pop(peer, None)

    def _remove_peer_locked(self, peer, sock):
        """在持有锁的情况下移除 peer（sock 已断开）"""
        if peer in self.peers and self.peers[peer] is sock:
            del self.peers[peer]
        if peer in self.last_recv_time:
            del self.last_recv_time[peer]
        self._passive_peers.discard(peer)
        self.recv_buffers.pop(peer, None)
        self._buffer_last_append.pop(peer, None)

    def _cleanup_idle_buffers(self, now: float):
        """清理超过空闲时限的半帧缓冲区，防止内存泄漏。

        只清理 recv_buffers 中确实有残留数据（不完整帧）的 peer。
        缓冲区为空的 peer 说明数据已完整处理，连接依然存活，不应被误杀。
        """
        stale_peers = []
        for peer, last_ts in list(self._buffer_last_append.items()):
            if now - last_ts > RECV_BUFFER_IDLE_TIMEOUT:
                buf = self.recv_buffers.get(peer)
                if buf and len(buf) > 0:
                    # 缓冲区非空但长时间无新数据 → 半帧残留，清理
                    stale_peers.append(peer)
                else:
                    # 缓冲区已空，数据完整处理完毕，仅移除时间戳记录
                    self._buffer_last_append.pop(peer, None)
                    self.recv_buffers.pop(peer, None)
        for peer in stale_peers:
            self.recv_buffers.pop(peer, None)
            self._buffer_last_append.pop(peer, None)
            self._remove_peer(peer)

    # def heartbeat_loop(self):
    #     """定期发送ping，并检查心跳超时"""
    #     while self.running:
    #         time.sleep(self.heartbeat_interval)
    #         now = time.time()
    #         with self.lock:
    #             for peer, sock in list(self.peers.items()):
    #                 try:
    #                     ping = json.dumps({"type": "ping", "sender": self.my_name})
    #                     sock.sendall(ping.encode())
    #                 except Exception:
    #                     self._remove_peer(peer)
    #                     continue
    #                 last = self.last_recv_time.get(peer, 0)
    #                 if now - last > self.heartbeat_timeout:
    #                     print("[{}] 心跳超时，断开 {}".format(self.my_name, peer))
    #                     self._remove_peer(peer)

    # def input_loop(self):
    #     print("\n=== 智能体 {} 已启动 ===".format(self.my_name))
    #     print("输入格式：")
    #     print("  普通文本：广播给所有人")
    #     print("  @名字 消息：私聊指定智能体")
    #     print("  /quit：退出\n")
    #     while self.running:
    #         try:
    #             text = input("[你]: ")
    #             if text.strip() == "/quit":
    #                 break
    #             if not text:
    #                 continue
    #             self.parse_and_send(text)
    #         except KeyboardInterrupt:
    #             break
    #     self.shutdown()

    # def parse_and_send(self, text):
    #     """解析用户输入，支持广播或私聊"""
    #     text = text.strip()
    #     if text.startswith('@'):
    #         parts = text.split(' ', 1)
    #         if len(parts) < 2:
    #             print("[提示] 格式错误：@对方名字 消息内容")
    #             return
    #         target = parts[0][1:]
    #         content = parts[1]
    #         self.send_to_one(target, content)
    #     # else:
    #     #     self.send_to_all(text)

    def send_to_one(self, target_name, content,
                    conversation_id: str = None,
                    reply_to: str = None,
                    context: dict = None,
                    timeout: int = 30,
                    msg_type: str = "task"):
        """发送结构化消息给指定智能体，等待回复

        Args:
            target_name: 目标智能体名称
            content: 消息内容（字符串 or 结构化数据）
            conversation_id: 对话 ID（None 则自动生成）
            reply_to: 回复某条消息的 ID
            context: 富上下文（history, task 等）
            timeout: 等待回复超时秒数
            msg_type: 消息类型 — "task" 会触发对方 agent 处理, "result" 仅展示

        Returns:
            对方的回复内容字符串
        """
        with self.lock:
            exists = target_name in self.peers
        if not exists:
            event_bus.put(UIEvent("TEXT", "[错误] 智能体 {} 不在线或未连接".format(target_name)))
            return "[错误] 智能体 {} 不在线或未连接".format(target_name)

        conv_id = conversation_id or str(uuid_module.uuid4())

        p2p_msg = P2PMessage(
            id=str(uuid_module.uuid4()),
            sender=self.my_name,
            target=target_name,
            type=msg_type,
            content=content,
            conversation_id=conv_id,
            reply_to=reply_to,
            context=context or {},
        )

        # 注册待回复事件
        reply_event = threading.Event()
        reply_container = {}
        with self._pending_replies_lock:
            self._pending_replies[conv_id] = {
                "event": reply_event,
                "response": reply_container,
            }

        self._enqueue_send(target_name, p2p_msg.to_json())

        # 等待回复
        if reply_event.wait(timeout=timeout):
            with self._pending_replies_lock:
                self._pending_replies.pop(conv_id, None)
            result_text = reply_container.get("response", "")
            # ═══ 包裹为最终交付物标记，防止 A 收到后再向 B 发确认 ═══
            return (
                "📬 **{} 的任务结果（最终交付物）**\n"
                "{}\n"
                "> ⚡ 上述是 {} 的最终交付物。请直接整合后向用户汇报，"
                "不要再向 {} 发送确认或感谢消息。"
            ).format(target_name, result_text, target_name, target_name)
        else:
            with self._pending_replies_lock:
                self._pending_replies.pop(conv_id, None)
            return "[超时] {} 未在 {} 秒内回复".format(target_name, timeout)

    def send_to_one_async(self, target_name, content,
                          conversation_id: str = None,
                          reply_to: str = None,
                          context: dict = None,
                          msg_type: str = "task") -> str:
        """发送结构化消息给指定智能体，不等待回复，立即返回 task_id。

        Args:
            target_name: 目标智能体名称
            content: 消息内容
            conversation_id: 对话 ID（None 则自动生成）
            reply_to: 回复某条消息的 ID
            context: 富上下文
            msg_type: 消息类型

        Returns:
            task_id 字符串，用于后续 p2p_check 轮询
        """
        with self.lock:
            exists = target_name in self.peers
        if not exists:
            return json.dumps({
                "status": "错误",
                "message": "智能体 {} 不在线或未连接".format(target_name),
                "task_id": None,
            })

        conv_id = conversation_id or str(uuid_module.uuid4())
        task_id = str(uuid_module.uuid4())

        p2p_msg = P2PMessage(
            id=task_id,
            sender=self.my_name,
            target=target_name,
            type=msg_type,
            content=content,
            conversation_id=conv_id,
            reply_to=reply_to,
            context=context or {},
        )

        # 注册异步等待事件
        reply_event = threading.Event()
        reply_container = {}
        with self._async_replies_lock:
            self._async_replies[task_id] = {
                "event": reply_event,
                "response": reply_container,
            }
        # 也注册到 _pending_replies（handle_receive 通过 conv_id 路由 result）
        with self._pending_replies_lock:
            self._pending_replies[conv_id] = {
                "event": reply_event,
                "response": reply_container,
            }

        self._enqueue_send(target_name, p2p_msg.to_json())

        return json.dumps({
            "status": "已发送",
            "task_id": task_id,
            "提示": "稍后使用 p2p_check('{}') 获取 {} 的回复".format(task_id, target_name),
        }, ensure_ascii=False)

    def get_async_reply(self, task_id: str) -> Optional[str]:
        """非阻塞查询异步任务的回复。

        Args:
            task_id: send_to_one_async 返回的 task_id

        Returns:
            回复字符串（如果已收到），否则 None
        """
        with self._async_replies_lock:
            entry = self._async_replies.get(task_id)
        if entry is None:
            return json.dumps({
                "status": "错误",
                "message": "无效的 task_id: {}".format(task_id),
            }, ensure_ascii=False)

        # 非阻塞检查
        if entry["event"].is_set():
            with self._async_replies_lock:
                self._async_replies.pop(task_id, None)
            return entry["response"].get("response", "")

        return None  # 还没好

    def shutdown(self):
        self.running = False
        self.executor.shutdown(wait=False)
        with self.lock:
            for sock in self.peers.values():
                sock.close()
            self.peers.clear()
        self.recv_buffers.clear()
        self._buffer_last_append.clear()
        self._passive_peers.clear()
        if self.listener:
            self.listener.close()
        try:
            os.unlink(self.my_addr)
        except OSError:
            pass
        # print("[{}] 已退出".format(self.my_name))



# 全局单例管理
_p2p_instance: Optional[AutoAgent] = None
_p2p_lock = threading.Lock()


def init_p2p(name: str) -> AutoAgent:
    """
    初始化 P2P 单例

    Args:
        name: 当前 Jify 的名称

    Returns:
        JifyP2P 实例
    """
    global _p2p_instance
    with _p2p_lock:
        if _p2p_instance is None:
            _p2p_instance = AutoAgent(name)
            _p2p_instance.start()
        return _p2p_instance



def get_p2p() -> Optional[AutoAgent]:
    """获取 P2P 单例（可能为 None）"""
    return _p2p_instance


def is_processing_p2p_request() -> bool:
    """当前线程是否正在处理 P2P 请求（_handle_request 上下文中）"""
    return getattr(_p2p_tls, 'is_processing', False)


# 全局忙碌标志 — 设置后 P2P 收到 task/chat 请求会直接返回“正忙”，不放入处理队列
_p2p_busy = False
_p2p_busy_lock = threading.Lock()


def set_p2p_busy(busy: bool) -> None:
    """设置 P2P 忙碌标志。busy=True 时，收到的 task/chat 请求会被直接拒绝。"""
    global _p2p_busy
    with _p2p_busy_lock:
        _p2p_busy = busy


def is_p2p_busy() -> bool:
    with _p2p_busy_lock:
        return _p2p_busy


# 全局请求处理器 — 允许 CLI 注入自己的 AgentLoop
_request_handler: Optional[Callable] = None
_handler_lock = threading.Lock()


def set_request_handler(handler: Callable) -> None:
    """注入自定义 P2P 请求处理器。

    handler 签名为 (P2PMessage) -> Optional[str]：
    - 返回 str: 作为回复发送给请求方
    - 返回 None: 回退到默认 AutoAgent.process_request 处理

    CLI 可借此将 P2P 请求路由到自己的 AgentLoop + CLIConsole，
    实现与用户直接交互一致的流式渲染效果。
    """
    global _request_handler
    with _handler_lock:
        _request_handler = handler


def get_request_handler() -> Optional[Callable]:
    with _handler_lock:
        return _request_handler


def set_agent_loop(agent_loop) -> None:
    """注入 AgentLoop 实例，供 process_request 回退逻辑使用。

    CLI 环境下由 set_request_handler 接管，此方法可省略。
    非 CLI 环境（如直接使用 P2PManager）需要调用此方法绑定 AgentLoop。
    """
    global _p2p_instance
    if _p2p_instance:
        _p2p_instance._agent_loop = agent_loop



# 后处理回调 — 允许 CLI 在回复 A 之后自动续接被中断的任务
_post_handler_callback: Optional[Callable] = None
_post_handler_lock = threading.Lock()


def set_post_handler_callback(callback: Optional[Callable]) -> None:
    """注入 P2P 请求处理完成后的回调（在回复已发送之后执行）。

    callback 签名为 () -> None，在 _handle_request 发送回复之后调用。
    用于 CLI 自动续接被 P2P 请求打断的用户任务。
    回调执行一次后自动清除。
    """
    global _post_handler_callback
    with _post_handler_lock:
        _post_handler_callback = callback


def get_and_clear_post_handler_callback() -> Optional[Callable]:
    """获取并清除后处理回调（一次性消费）。"""
    global _post_handler_callback
    with _post_handler_lock:
        cb = _post_handler_callback
        _post_handler_callback = None
        return cb


def stop_p2p():
    """停止 P2P 服务"""
    global _p2p_instance
    with _p2p_lock:
        if _p2p_instance:
            _p2p_instance.shutdown()
            _p2p_instance = None




def main():
    parser = argparse.ArgumentParser(description="自动发现同级UDS智能体")
    parser.add_argument("--name", required=True, help="自己的名字")
    args = parser.parse_args()
    agent = AutoAgent(args.name)
    agent.start()

if __name__ == "__main__":
    main()
