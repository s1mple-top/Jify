# -*- coding: utf-8 -*-
"""
跨会话历史流 — SQLite + FTS5 检索（P0 压缩黑洞修复 + P1 召回）。

设计原则：写入即落库，压缩只改标记。
  stream 表 append-only 记录**全量消息**（含 tool 消息、tool_calls 原文）——
  与 messages 表（resume 用，仅 user/assistant 文本，save 时先删后写）完全独立，
  互不影响。压缩折叠只 UPDATE active/compacted 标记位，**永不删除行**。

  两张 FTS5 索引（external content 模式，触发器同步）：
    stream_fts      — unicode61 分词（西文）
    stream_fts_bi   — CJK 双字切分（bigram）后走 unicode61，支持中文子串匹配

  为什么用 bigram 而不是 trigram：trigram 需 >=3 字，最常用的双字词
  （西安/天气/漏洞/报错）永远零命中；且 trigram 索引体积接近原文。
  bigram 把中文连续段切成重叠双字 token，双字查询走短语匹配即等价于子串
  匹配，索引体积约为 trigram 的 1/10，查询快 100 倍。
  单字中文无法用 bigram 表达，回退 LIKE 扫描。

  content_fts 列为截断版（_FTS_INDEX_CAP），避免巨型工具输出撑爆索引；
  content 列存全量，窗口读取时返回原文。content_bi 是 content 的 CJK
  bigram 派生列（供 external content 索引读取，触发器无法调用 Python）。

Schema:
  stream — (id, session_id, role, content, content_fts, content_bi, tool_name,
            created_at, active, compacted)
"""

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# FTS 索引内容上限（超长工具输出只索引前缀，存储仍保留全量）
_FTS_INDEX_CAP = 32 * 1024

# 判定查询是否含 CJK 字符（决定走哪张索引）
def _has_cjk(text: str) -> bool:
    return any('\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf'
               for ch in text)


def _cjk_bigrams(text: str) -> List[str]:
    """把 CJK 连续段切成重叠双字 token，非 CJK 字符作为断点丢弃。

    '看下西安天气' -> ['看下', '下西', '西安', '安天', '天气']
    单字段（长度 1）无法成词，直接跳过——查询侧会因此判定为单字回退。
    """
    tokens: List[str] = []
    buf: List[str] = []

    def flush() -> None:
        if len(buf) >= 2:
            for i in range(len(buf) - 1):
                tokens.append(buf[i] + buf[i + 1])
        del buf[:]

    for ch in text:
        if '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf':
            buf.append(ch)
        else:
            flush()
    flush()
    return tokens


def _bigram_text(text: str) -> str:
    """content_bi 列的存储形态：bigram token 以空格分隔，供 unicode61 分词。"""
    return " ".join(_cjk_bigrams(text))


class StreamStore:
    """全量消息流存储 — 追加写入 + FTS 检索 + 压缩标记。"""

    def __init__(self, db_path: Path):
        self.db_path = str(db_path)
        self._fts_bi_enabled = False
        self._init_tables()

    def _connect(self) -> sqlite3.Connection:
        # WAL 是持久在库文件中的属性，只需在 _init_tables 设置一次；
        # 每连接重复 PRAGMA 会带来冗余写入。timeout 保证并发写时不立即报锁。
        return sqlite3.connect(self.db_path, timeout=10.0)

    def _init_tables(self) -> None:
        with closing(sqlite3.connect(self.db_path, timeout=10.0)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stream (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id  TEXT NOT NULL,
                    role        TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    content_fts TEXT NOT NULL,
                    content_bi  TEXT,
                    tool_name   TEXT,
                    created_at  TEXT NOT NULL,
                    active      INTEGER NOT NULL DEFAULT 1,
                    compacted   INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_stream_session
                ON stream(session_id, id)
            """)
            # 西文索引（unicode61）
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS stream_fts USING fts5(
                    content_fts, tool_name,
                    content='stream', content_rowid='id',
                    tokenize='unicode61'
                )
            """)
            self._migrate_to_bigram(conn)
            # 触发器：写入/删除/更新同步西文索引
            # UPDATE 触发器带 WHEN：mark_compacted 只改标记位不重写索引
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS stream_fts_ai AFTER INSERT ON stream BEGIN
                  INSERT INTO stream_fts(rowid, content_fts, tool_name)
                  VALUES (new.id, new.content_fts, new.tool_name);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS stream_fts_ad AFTER DELETE ON stream BEGIN
                  INSERT INTO stream_fts(stream_fts, rowid, content_fts, tool_name)
                  VALUES ('delete', old.id, old.content_fts, old.tool_name);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS stream_fts_au AFTER UPDATE OF content_fts, tool_name ON stream BEGIN
                  INSERT INTO stream_fts(stream_fts, rowid, content_fts, tool_name)
                  VALUES ('delete', old.id, old.content_fts, old.tool_name);
                  INSERT INTO stream_fts(rowid, content_fts, tool_name)
                  VALUES (new.id, new.content_fts, new.tool_name);
                END
            """)
            if self._fts_bi_enabled:
                conn.execute("""
                    CREATE TRIGGER IF NOT EXISTS stream_fts_bi_ai AFTER INSERT ON stream BEGIN
                      INSERT INTO stream_fts_bi(rowid, content_bi)
                      VALUES (new.id, new.content_bi);
                    END
                """)
                conn.execute("""
                    CREATE TRIGGER IF NOT EXISTS stream_fts_bi_ad AFTER DELETE ON stream BEGIN
                      INSERT INTO stream_fts_bi(stream_fts_bi, rowid, content_bi)
                      VALUES ('delete', old.id, old.content_bi);
                    END
                """)
                conn.execute("""
                    CREATE TRIGGER IF NOT EXISTS stream_fts_bi_au AFTER UPDATE OF content_bi ON stream BEGIN
                      INSERT INTO stream_fts_bi(stream_fts_bi, rowid, content_bi)
                      VALUES ('delete', old.id, old.content_bi);
                      INSERT INTO stream_fts_bi(rowid, content_bi)
                      VALUES (new.id, new.content_bi);
                    END
                """)
            conn.commit()

    def _migrate_to_bigram(self, conn: sqlite3.Connection) -> None:
        """建立 CJK bigram 索引，并把老库从 trigram 迁移过来（幂等）。

        切换索引类型必须回填派生列并重建索引，因此这里做成一次性 upgrade：
        只有发现待回填的行才 rebuild，正常启动零额外开销。
        """
        cols = {r[1] for r in conn.execute("PRAGMA table_info(stream)")}
        if "content_bi" not in cols:
            conn.execute("ALTER TABLE stream ADD COLUMN content_bi TEXT")

        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS stream_fts_bi USING fts5(
                    content_bi,
                    content='stream', content_rowid='id',
                    tokenize='unicode61'
                )
            """)
            self._fts_bi_enabled = True
        except sqlite3.OperationalError:
            return

        # 回填派生列（老库行 content_bi 为 NULL；新库无行，此段为空操作）
        pending = conn.execute(
            "SELECT id, content_fts FROM stream WHERE content_bi IS NULL"
        ).fetchall()
        if pending:
            conn.executemany(
                "UPDATE stream SET content_bi=? WHERE id=?",
                [(_bigram_text(t), i) for i, t in pending],
            )

        # trigram 表已被 bigram 取代，清掉历史遗留（索引体积约为 bigram 的 10 倍）
        conn.execute("DROP TRIGGER IF EXISTS stream_fts_cjk_ai")
        conn.execute("DROP TRIGGER IF EXISTS stream_fts_cjk_ad")
        conn.execute("DROP TRIGGER IF EXISTS stream_fts_cjk_au")
        conn.execute("DROP TABLE IF EXISTS stream_fts_cjk")

        if pending:
            conn.execute("INSERT INTO stream_fts_bi(stream_fts_bi) VALUES('rebuild')")

    # ── 写入 ─────────────────────────────────────────────

    def append(self, session_id: str, role: str, content: str,
               tool_name: str = "") -> Optional[int]:
        """追加一条消息到流，返回 stream id（失败返回 None）。

        被 agent_loop 在每条消息 append 时调用；任何异常由调用方吞掉，
        不影响主对话流程。
        """
        now = datetime.now().isoformat()
        content = content or ""
        indexed = content[:_FTS_INDEX_CAP]
        try:
            with closing(self._connect()) as conn, conn:
                cur = conn.execute(
                    "INSERT INTO stream "
                    "(session_id, role, content, content_fts, content_bi, tool_name, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (session_id, role, content, indexed, _bigram_text(indexed),
                     tool_name or None, now),
                )
                conn.commit()
                return cur.lastrowid
        except sqlite3.Error:
            return None

    def mark_compacted(self, session_id: str, stream_ids: List[int]) -> None:
        """P0 标记位：把被压缩折叠的消息标记 active=0, compacted=1。

        只 UPDATE 不删除——原文仍在库里，检索默认放行 compacted=1 的行。
        """
        if not stream_ids:
            return
        try:
            with closing(self._connect()) as conn, conn:
                conn.executemany(
                    "UPDATE stream SET active=0, compacted=1 "
                    "WHERE session_id=? AND id=? AND active=1",
                    [(session_id, i) for i in stream_ids],
                )
                conn.commit()
        except sqlite3.Error:
            pass

    # ── 检索 ─────────────────────────────────────────────

    @staticmethod
    def _make_snippet(content: str, words: List[str], width: int = 30) -> str:
        """从原文截取命中词周围的片段（FTS 的 snippet() 只能取索引列，
        而 bigram 索引列存的是切分后的 token，不能直接展示）。"""
        low = content.lower()
        pos = -1
        for w in words:
            p = low.find(w.lower())
            if p >= 0 and (pos < 0 or p < pos):
                pos = p
        if pos < 0:
            head = content[:width * 2].replace("\n", " ")
            return head + ("…" if len(content) > width * 2 else "")
        start = max(0, pos - width)
        end = min(len(content), pos + width)
        return (("…" if start else "")
                + content[start:end].replace("\n", " ")
                + ("…" if end < len(content) else ""))

    def _search_fts(self, conn: sqlite3.Connection, fts_table: str, match: str,
                    exclude_session: Optional[str], limit: int) -> List[tuple]:
        """对单张 FTS 表执行 BM25 查询。

        返回 (id, session_id, content, compacted, role, tool_name, created_at)。
        """
        sql = (
            "SELECT s.id, s.session_id, s.content, s.compacted, "
            "s.role, s.tool_name, s.created_at "
            f"FROM {fts_table} f JOIN stream s ON s.id = f.rowid "
            f"WHERE {fts_table} MATCH ? "
        )
        params: List[Any] = [match]
        if exclude_session:
            sql += "AND s.session_id <> ? "
            params.append(exclude_session)
        sql += "ORDER BY rank LIMIT ?"
        params.append(limit)
        return conn.execute(sql, params).fetchall()

    def _search_like(self, conn: sqlite3.Connection, words: List[str],
                     exclude_session: Optional[str], limit: int) -> List[tuple]:
        """bigram 无法表达的单字中文，回退 LIKE 子串扫描。

        线性复杂度，当前库量级为毫秒级；行数上限防止极端膨胀时卡死。
        """
        sql = ("SELECT s.id, s.session_id, s.content, s.compacted, "
               "s.role, s.tool_name, s.created_at "
               "FROM stream s WHERE ")
        params: List[Any] = []
        if exclude_session:
            sql += "s.session_id <> ? AND "
            params.append(exclude_session)
        sql += " AND ".join("s.content LIKE ?" for _ in words)
        sql += " ORDER BY s.id DESC LIMIT ?"
        params.extend("%" + w + "%" for w in words)
        params.append(limit)
        return conn.execute(sql, params).fetchall()

    def search(self, query: str, limit: int = 10,
               exclude_session: Optional[str] = None) -> List[Dict[str, Any]]:
        """FTS5 BM25 检索。

        按词的语言分流：含 CJK 的词切 bigram 后走 stream_fts_bi（短语匹配
        等价于子串匹配），其余词走 stream_fts（unicode61）。只有单字中文
        无法用 bigram 表达时才回退 LIKE。

        返回按会话去重后的命中列表（每会话取最佳命中），
        字段: session_id, id, role, tool_name, snippet, created_at, compacted。
        """
        query = (query or "").strip()
        if not query:
            return []
        words = [w for w in query.split() if w.strip()]
        if not words:
            return []

        bi_parts: List[str] = []
        uni_parts: List[str] = []
        lone_cjk: List[str] = []
        for w in words:
            toks = _cjk_bigrams(w)
            if toks:
                bi_parts.append('"' + " ".join(toks) + '"')
            elif _has_cjk(w):
                lone_cjk.append(w)      # 单字中文，bigram 无解
            else:
                uni_parts.append('"' + w.replace('"', '""') + '"')

        if not bi_parts and not uni_parts and lone_cjk:
            lookup = [w.lower() for w in lone_cjk]
            try:
                with closing(self._connect()) as conn, conn:
                    rows = self._search_like(conn, lone_cjk, exclude_session, limit * 3)
            except sqlite3.Error:
                return []
        else:
            rows = []
            try:
                with closing(self._connect()) as conn, conn:
                    if bi_parts:
                        rows.extend(self._search_fts(
                            conn, "stream_fts_bi", " OR ".join(bi_parts),
                            exclude_session, min(limit * 3, 90)))
                    if uni_parts:
                        rows.extend(self._search_fts(
                            conn, "stream_fts", " OR ".join(uni_parts),
                            exclude_session, min(limit * 3, 90)))
            except sqlite3.Error:
                return []
            lookup = [w.lower() for w in words]

        # 按会话去重，保留每会话第一条（rank 最优；混合查询时 bigram 命中优先）
        seen: set = set()
        results = []
        for r in rows:
            sid = r[1]
            if sid in seen:
                continue
            seen.add(sid)
            results.append({
                "id": r[0], "session_id": sid,
                "role": r[4], "tool_name": r[5], "created_at": r[6],
                "compacted": bool(r[3]),
                "snippet": self._make_snippet(r[2] or "", lookup),
            })
            if len(results) >= limit:
                break
        return results

    def window(self, session_id: str, around_id: int,
               before: int = 5, after: int = 5) -> List[Dict[str, Any]]:
        """读取会话内 anchor 消息前后各 N 条的原文窗口（scroll 模式用）。

        id 是跨会话共享的自增主键，不能直接 id±N 取区间——那样窗口会在会话
        边界被截断、跨会话漏条。这里按会话内相对位置取前后两段再合并。
        """
        _COLS = ("SELECT id, role, content, tool_name, compacted, created_at "
                 "FROM stream WHERE session_id=? AND id {} ? ORDER BY id {} LIMIT ?")
        try:
            with closing(self._connect()) as conn, conn:
                head = conn.execute(_COLS.format("<=", "DESC"),
                                    (session_id, around_id, before + 1)).fetchall()
                tail = conn.execute(_COLS.format(">", "ASC"),
                                    (session_id, around_id, after)).fetchall()
        except sqlite3.Error:
            return []
        rows = list(reversed(head)) + tail
        return [
            {"id": r[0], "role": r[1], "content": r[2], "tool_name": r[3],
             "compacted": bool(r[4]), "created_at": r[5]}
            for r in rows
        ]

    def session_heads(self, session_id: str, n: int = 2) -> List[str]:
        """会话最前 n 条 user 消息（browse 预览用）。"""
        try:
            with closing(self._connect()) as conn, conn:
                rows = conn.execute(
                    "SELECT content FROM stream "
                    "WHERE session_id=? AND role='user' ORDER BY id LIMIT ?",
                    (session_id, n),
                ).fetchall()
        except sqlite3.Error:
            return []
        return [r[0][:120] for r in rows if r[0]]

    def session_message_count(self, session_id: str) -> int:
        try:
            with closing(self._connect()) as conn, conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM stream WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                return row[0] if row else 0
        except sqlite3.Error:
            return 0

    def id_range(self, session_id: str) -> Optional[tuple]:
        """该会话在 stream 表中的 (min_id, max_id)；无消息返回 None。"""
        try:
            with closing(self._connect()) as conn, conn:
                row = conn.execute(
                    "SELECT MIN(id), MAX(id) FROM stream WHERE session_id=?",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error:
            return None
        if not row or row[1] is None:
            return None
        return (row[0], row[1])


# 模块级单例（与 ConversationDB 共用同一个 db 文件，独立实例互不干扰）
_stream_instance: Optional[StreamStore] = None

# 当前活跃会话 id（agent_loop 写入，session_search 读取用于默认排除当前会话）
_current_session: Optional[str] = None


def set_current_session(session_id: Optional[str]) -> None:
    global _current_session
    _current_session = session_id


def get_current_session() -> Optional[str]:
    return _current_session


def get_stream_store(db_path: Optional[Path] = None) -> StreamStore:
    """获取全局 StreamStore 实例（懒加载，默认与 ConversationDB 同库）。"""
    global _stream_instance
    if _stream_instance is None:
        if db_path is None:
            from storage.db import _DB_PATH
            db_path = _DB_PATH
        _stream_instance = StreamStore(db_path)
    return _stream_instance
