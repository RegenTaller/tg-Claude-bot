"""Локальное хранилище на SQLite: настройки пользователей, история диалогов, расход токенов.

История хранится только добавлением (append-only): ранние сообщения никогда не правятся.
Это важно для API — блоки размышлений модели привязаны к точному префиксу диалога.
"""

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id         INTEGER PRIMARY KEY,
    model           TEXT NOT NULL,
    effort          TEXT NOT NULL,
    conversation_id INTEGER
);
CREATE TABLE IF NOT EXISTS allowed (
    user_id INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS conversations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    system_prompt TEXT NOT NULL,
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    role            TEXT NOT NULL,
    content_json    TEXT NOT NULL,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conv ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read    INTEGER NOT NULL,
    cache_write   INTEGER NOT NULL,
    created_at    REAL NOT NULL
);
"""


class Storage:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # --- доступ ---

    def is_allowed_extra(self, user_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM allowed WHERE user_id=?", (user_id,)).fetchone() is not None

    def allow(self, user_id: int) -> None:
        self.db.execute("INSERT OR IGNORE INTO allowed(user_id) VALUES (?)", (user_id,))
        self.db.commit()

    def deny(self, user_id: int) -> None:
        self.db.execute("DELETE FROM allowed WHERE user_id=?", (user_id,))
        self.db.commit()

    def allowed_list(self) -> list[int]:
        return [r[0] for r in self.db.execute("SELECT user_id FROM allowed ORDER BY user_id")]

    # --- настройки ---

    def get_settings(self, user_id: int, default_model: str, default_effort: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO users(user_id, model, effort) VALUES (?,?,?)",
                (user_id, default_model, default_effort),
            )
            self.db.commit()
            row = self.db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row

    def set_model(self, user_id: int, model: str) -> None:
        self.db.execute("UPDATE users SET model=? WHERE user_id=?", (model, user_id))
        self.db.commit()

    def set_effort(self, user_id: int, effort: str) -> None:
        self.db.execute("UPDATE users SET effort=? WHERE user_id=?", (effort, user_id))
        self.db.commit()

    # --- диалоги ---

    def new_conversation(self, user_id: int, system_prompt: str) -> int:
        cur = self.db.execute(
            "INSERT INTO conversations(user_id, system_prompt, created_at) VALUES (?,?,?)",
            (user_id, system_prompt, time.time()),
        )
        conv_id = cur.lastrowid
        self.db.execute("UPDATE users SET conversation_id=? WHERE user_id=?", (conv_id, user_id))
        self.db.commit()
        return conv_id

    def conversation(self, conv_id: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()

    def history(self, conv_id: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT role, content_json FROM messages WHERE conversation_id=? ORDER BY id", (conv_id,)
        )
        return [{"role": r["role"], "content": json.loads(r["content_json"])} for r in rows]

    def append(self, conv_id: int, role: str, content: list[dict]) -> None:
        self.db.execute(
            "INSERT INTO messages(conversation_id, role, content_json, created_at) VALUES (?,?,?,?)",
            (conv_id, role, json.dumps(content, ensure_ascii=False), time.time()),
        )
        self.db.commit()

    def message_count(self, conv_id: int) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=?", (conv_id,)
        ).fetchone()[0]

    # --- расход ---

    def add_usage(self, user_id: int, model: str, usage) -> None:
        self.db.execute(
            "INSERT INTO usage(user_id, model, input_tokens, output_tokens, cache_read, cache_write, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                user_id,
                model,
                usage.input_tokens or 0,
                usage.output_tokens or 0,
                usage.cache_read_input_tokens or 0,
                usage.cache_creation_input_tokens or 0,
                time.time(),
            ),
        )
        self.db.commit()

    def usage_since(self, since: float, user_id: int | None = None) -> list[sqlite3.Row]:
        q = (
            "SELECT user_id, model, SUM(input_tokens) i, SUM(output_tokens) o,"
            " SUM(cache_read) cr, SUM(cache_write) cw, COUNT(*) n FROM usage WHERE created_at>=?"
        )
        args: list = [since]
        if user_id is not None:
            q += " AND user_id=?"
            args.append(user_id)
        q += " GROUP BY user_id, model"
        return list(self.db.execute(q, args))
