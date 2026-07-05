# -*- coding: utf-8 -*-
"""
SQLite 对话持久化 - 单文件数据库，零依赖。

Schema:
  sessions  — 会话元信息（id, 时间, 身份, 模型）
  messages  — 完整消息记录（角色, 内容, tool_calls, 序号）

会话 key: 8 位 hex (uuid4 前 8 字符)，如 "a3f8b2c1"
"""

import sqlite3
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

# 数据库文件放在项目根目录
_DB_PATH = Path.home() / ".jify" / "jify_history.db"


class ConversationDB:
    """对话数据库 — 会话 CRUD + 消息存取"""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = str(db_path or _DB_PATH)
        self._init_tables()


    # 初始化
    def _init_tables(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id            TEXT PRIMARY KEY,
                    created_at    TEXT NOT NULL,
                    ended_at      TEXT,
                    jify_name     TEXT,
                    model         TEXT,
                    message_count INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id        TEXT NOT NULL,
                    role              TEXT NOT NULL,
                    content           TEXT NOT NULL,
                    tool_calls        TEXT,
                    tool_call_id      TEXT,
                    reasoning_content TEXT,
                    created_at        TEXT NOT NULL,
                    seq               INTEGER NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            # 加速按会话查询消息
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id, seq)
            """)
            conn.commit()


    # 会话操作
    def create_session(self, jify_name: str = "", model: str = "") -> str:
        """创建新会话，返回 session_id"""
        session_id = uuid.uuid4().hex[:8]
        now = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO sessions (id, created_at, jify_name, model) "
                "VALUES (?, ?, ?, ?)",
                (session_id, now, jify_name, model),
            )
            conn.commit()
        return session_id

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """获取会话元信息"""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT id, created_at, ended_at, jify_name, model, message_count "
                "FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "created_at": row[1],
            "ended_at": row[2],
            "jify_name": row[3],
            "model": row[4],
            "message_count": row[5],
        }

    def list_sessions(self, limit: int = 20) -> List[Dict[str, Any]]:
        """列出最近会话"""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id, created_at, ended_at, jify_name, model, message_count "
                "FROM sessions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": r[0],
                "created_at": r[1],
                "ended_at": r[2],
                "jify_name": r[3],
                "model": r[4],
                "message_count": r[5],
            }
            for r in rows
        ]

    def close_session(self, session_id: str) -> None:
        """标记会话结束时间"""
        now = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (now, session_id),
            )
            conn.commit()


    # 消息存储
    def save_messages(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        jify_name: str = "",
        model: str = "",
    ) -> None:
        """
        批量保存消息列表。先删旧消息再全量写入（幂等）。

        messages 格式: [{"role", "content", "tool_calls"?, "tool_call_id"?, "reasoning_content"?}, ...]
        """
        now = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            # 更新或创建 session
            conn.execute(
                "INSERT INTO sessions (id, created_at, ended_at, jify_name, model, message_count) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "ended_at = excluded.ended_at, "
                "message_count = excluded.message_count",
                (session_id, now, now, jify_name, model, len(messages)),
            )
            # 清除旧消息
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            # 批量插入
            rows = []
            for i, msg in enumerate(messages):
                rows.append((
                    session_id,
                    msg.get("role", ""),
                    msg.get("content", ""),
                    json.dumps(msg["tool_calls"]) if msg.get("tool_calls") else None,
                    msg.get("tool_call_id"),
                    msg.get("reasoning_content"),
                    now,
                    i,
                ))
            conn.executemany(
                "INSERT INTO messages "
                "(session_id, role, content, tool_calls, tool_call_id, reasoning_content, created_at, seq) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()

    def load_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """加载会话的所有消息，按 seq 排序"""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT role, content, tool_calls, tool_call_id, reasoning_content "
                "FROM messages WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        result = []
        for row in rows:
            role, content, tc_json, tc_id, reasoning = row
            msg: Dict[str, Any] = {
                "role": role,
                "content": content,
            }
            if tc_json:
                msg["tool_calls"] = json.loads(tc_json)
            if tc_id:
                msg["tool_call_id"] = tc_id
            if reasoning:
                msg["reasoning_content"] = reasoning
            result.append(msg)
        return result



# 模块级便捷函数
_db_instance: Optional[ConversationDB] = None


def get_db() -> ConversationDB:
    """获取全局数据库实例（懒加载）"""
    global _db_instance
    if _db_instance is None:
        _db_instance = ConversationDB()
    return _db_instance
