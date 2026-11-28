"""SQLite-хранилище. Объём маленький (до 30 диалогов в день), поэтому одно соединение под замком."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ig_user_id TEXT NOT NULL UNIQUE,
    username TEXT,
    status TEXT NOT NULL DEFAULT 'bot',          -- bot | manager
    scope TEXT NOT NULL DEFAULT 'unknown',       -- unknown | tattoo | personal | ignored
    review_required INTEGER NOT NULL DEFAULT 0,  -- старый контакт: ответы бота только черновиком
    needs_disclosure INTEGER NOT NULL DEFAULT 1,
    last_client_msg_at REAL,
    last_activity_at REAL,
    created_at REAL NOT NULL,
    llm_history TEXT NOT NULL DEFAULT '[]',      -- только дописывается (preserved thinking)
    pending_notes TEXT NOT NULL DEFAULT '[]'     -- заметки для модели к следующему ходу
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    mid TEXT UNIQUE,
    author TEXT NOT NULL,                        -- client | bot | manager
    text TEXT NOT NULL DEFAULT '',
    attachments TEXT NOT NULL DEFAULT '[]',
    processed INTEGER NOT NULL DEFAULT 0,        -- для client: уже передано модели
    echo_mid TEXT,                               -- для bot: номер эха, которым Instagram подтвердил отправку
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at);
CREATE TABLE IF NOT EXISTS processed_events (
    key TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',      -- pending | sent | edited | discarded | superseded | expired
    tg_message_id INTEGER,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tg_pending_edits (
    tg_prompt_message_id INTEGER PRIMARY KEY,
    draft_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    data TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested',    -- requested | confirmed
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS personal_contacts (
    key TEXT PRIMARY KEY                         -- id:<IGSID> или user:<username>
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class Conversation:
    id: int
    ig_user_id: str
    username: str | None
    status: str
    scope: str
    review_required: bool
    needs_disclosure: bool
    last_client_msg_at: float | None
    last_activity_at: float | None
    created_at: float
    llm_history: list[dict[str, Any]]
    pending_notes: list[str]


def _conv(row: sqlite3.Row | None) -> Conversation | None:
    if row is None:
        return None
    return Conversation(
        id=row["id"],
        ig_user_id=row["ig_user_id"],
        username=row["username"],
        status=row["status"],
        scope=row["scope"],
        review_required=bool(row["review_required"]),
        needs_disclosure=bool(row["needs_disclosure"]),
        last_client_msg_at=row["last_client_msg_at"],
        last_activity_at=row["last_activity_at"],
        created_at=row["created_at"],
        llm_history=json.loads(row["llm_history"]),
        pending_notes=json.loads(row["pending_notes"]),
    )


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Дописывает колонки, появившиеся после первого запуска (база на Railway уже существует)."""
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "echo_mid" not in columns:
            self._conn.execute("ALTER TABLE messages ADD COLUMN echo_mid TEXT")
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(conversations)").fetchall()}
        if "review_required" not in columns:
            self._conn.execute("ALTER TABLE conversations ADD COLUMN review_required INTEGER NOT NULL DEFAULT 0")

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ---- дедупликация ----
    def mark_event(self, key: str) -> bool:
        """True, если событие новое. False, если уже обрабатывали."""
        try:
            self.execute("INSERT INTO processed_events(key, created_at) VALUES (?, ?)", (key, time.time()))
            return True
        except sqlite3.IntegrityError:
            return False

    # ---- диалоги ----
    def get_or_create_conversation(self, ig_user_id: str) -> Conversation:
        now = time.time()
        self.execute(
            "INSERT OR IGNORE INTO conversations(ig_user_id, created_at, last_activity_at) VALUES (?, ?, ?)",
            (ig_user_id, now, now),
        )
        return _conv(self.one("SELECT * FROM conversations WHERE ig_user_id = ?", (ig_user_id,)))  # type: ignore[return-value]

    def get_conversation(self, conversation_id: int) -> Conversation | None:
        return _conv(self.one("SELECT * FROM conversations WHERE id = ?", (conversation_id,)))

    def find_conversation(self, ig_user_id: str) -> Conversation | None:
        return _conv(self.one("SELECT * FROM conversations WHERE ig_user_id = ?", (ig_user_id,)))

    def update_conversation(self, conversation_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols, values = [], []
        for key, value in fields.items():
            if key in {"llm_history", "pending_notes"}:
                value = json.dumps(value, ensure_ascii=False)
            elif key in {"needs_disclosure", "review_required"}:
                value = int(bool(value))
            cols.append(f"{key} = ?")
            values.append(value)
        values.append(conversation_id)
        self.execute(f"UPDATE conversations SET {', '.join(cols)} WHERE id = ?", tuple(values))

    def append_history(self, conversation_id: int, new_messages: list[dict[str, Any]]) -> None:
        with self._lock:
            conv = self.get_conversation(conversation_id)
            assert conv is not None
            self.update_conversation(conversation_id, llm_history=conv.llm_history + new_messages)

    def add_note(self, conversation_id: int, note: str) -> None:
        with self._lock:
            conv = self.get_conversation(conversation_id)
            assert conv is not None
            self.update_conversation(conversation_id, pending_notes=conv.pending_notes + [note])

    # ---- сообщения ----
    def add_message(
        self,
        conversation_id: int,
        author: str,
        text: str,
        mid: str | None = None,
        attachments: list[dict] | None = None,
        created_at: float | None = None,
        processed: bool = False,
    ) -> int:
        cur = self.execute(
            "INSERT INTO messages(conversation_id, mid, author, text, attachments, processed, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                mid,
                author,
                text,
                json.dumps(attachments or [], ensure_ascii=False),
                int(processed),
                created_at or time.time(),
            ),
        )
        return int(cur.lastrowid)

    def unprocessed_client_messages(self, conversation_id: int) -> list[sqlite3.Row]:
        return self.all(
            "SELECT * FROM messages WHERE conversation_id = ? AND author = 'client' AND processed = 0 ORDER BY created_at, id",
            (conversation_id,),
        )

    def mark_processed(self, ids: list[int]) -> None:
        for mid_id in ids:
            self.execute("UPDATE messages SET processed = 1 WHERE id = ?", (mid_id,))

    def recent_messages(self, conversation_id: int, limit: int = 8) -> list[sqlite3.Row]:
        rows = self.all(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (conversation_id, limit),
        )
        return list(reversed(rows))

    # ---- личные контакты мастера: бот их не трогает ----
    def is_personal_contact(self, ig_user_id: str, username: str | None) -> bool:
        keys = [f"id:{ig_user_id}"] + ([f"user:{username.lower().lstrip('@')}"] if username else [])
        return any(self.one("SELECT 1 FROM personal_contacts WHERE key = ?", (k,)) for k in keys)

    def add_personal_contact(self, key: str) -> None:
        self.execute("INSERT OR IGNORE INTO personal_contacts(key) VALUES (?)", (key,))

    def forget_conversation_content(self, conversation_id: int) -> None:
        """Диалог не про тату: не храним из него ни текста, ни истории."""
        self.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        self.execute("DELETE FROM drafts WHERE conversation_id = ?", (conversation_id,))
        self.update_conversation(conversation_id, llm_history=[], pending_notes=[], username=None)

    # ---- kv ----
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ---- расход ----
    def record_usage(
        self,
        conversation_id: int | None,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_write_tokens: int,
        cache_read_tokens: int,
        cost_usd: float,
    ) -> None:
        self.execute(
            "INSERT INTO usage(conversation_id, model, input_tokens, output_tokens, cache_write_tokens, "
            "cache_read_tokens, cost_usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost_usd, time.time()),
        )

    def spend_since(self, since: float) -> float:
        row = self.one("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM usage WHERE created_at >= ?", (since,))
        return float(row["s"]) if row else 0.0
