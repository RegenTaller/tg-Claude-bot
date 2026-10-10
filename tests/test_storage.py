import sqlite3
import time

import pytest

from bot.storage import Storage


def test_old_database_is_migrated_and_costs_backfilled(tmp_path):
    path = str(tmp_path / "old.sqlite3")
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cache_read INTEGER NOT NULL,
            cache_write INTEGER NOT NULL, created_at REAL NOT NULL);
        INSERT INTO usage(user_id, model, input_tokens, output_tokens, cache_read, cache_write, created_at)
            VALUES (7, 'claude-opus-5-5', 1000000, 0, 0, 0, strftime('%s','now'));
    """)
    db.commit()
    db.close()

    store = Storage(path)
    rows = store.usage_since(0)
    assert rows[0]["cost"] == pytest.approx(4.0)
    assert rows[0]["unpriced"] == 0


def test_usage_sums_cost_and_counts_unpriced(tmp_path):
    store = Storage(str(tmp_path / "db.sqlite3"))
    store.add_usage(1, "claude-opus-5-5", 10, 10, 0, 0, 0.5)
    store.add_usage(1, "claude-opus-5-5", 10, 10, 0, 0, 0.25)
    store.add_usage(1, "claude-mystery", 10, 10, 0, 0, None)
    store.add_usage(2, "claude-opus-5-5", 10, 10, 0, 0, 1.0)
    rows = {(r["user_id"], r["model"]): r for r in store.usage_since(0)}
    assert rows[(1, "claude-opus-5-5")]["cost"] == pytest.approx(0.75)
    assert rows[(1, "claude-opus-5-5")]["n"] == 2
    assert rows[(1, "claude-mystery")]["unpriced"] == 1
    assert store.spent_since(1, 0) == pytest.approx(0.75)
    assert store.spent_since(1, time.time() + 10) == 0


def test_purge_keeps_current_and_recent_conversations(tmp_path):
    store = Storage(str(tmp_path / "db.sqlite3"))
    store.get_settings(1, "m", "e")
    old = store.new_conversation(1, "s")
    store.append(old, "user", [{"type": "text", "text": "старый"}])
    current = store.new_conversation(1, "s")  # old больше не текущий
    store.append(current, "user", [{"type": "text", "text": "текущий"}])
    store.get_settings(2, "m", "e")
    recent = store.new_conversation(2, "s")
    store.append(recent, "user", [{"type": "text", "text": "свежий"}])
    store.new_conversation(2, "s")  # recent брошен, но сообщения в нём свежие

    assert store.purge_old_conversations(time.time() - 3600) == 0  # всё моложе часа — не трогаем
    assert store.purge_old_conversations(time.time() + 1) == 2  # брошенные old и recent
    assert store.message_count(old) == 0
    assert store.message_count(recent) == 0
    assert store.message_count(current) == 1  # текущий диалог не удаляется никогда


def test_history_roundtrip_keeps_blocks_exactly(tmp_path):
    store = Storage(str(tmp_path / "db.sqlite3"))
    store.get_settings(1, "m", "e")
    conv = store.new_conversation(1, "s")
    content = [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": "Привет  мир"}]
    store.append(conv, "assistant", content)
    assert store.history(conv) == [{"role": "assistant", "content": content}]
