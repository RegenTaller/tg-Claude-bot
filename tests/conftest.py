import dataclasses
import os
import sys
import tempfile
from pathlib import Path

import pytest

# bot.main читает настройки при импорте, поэтому окружение задаём до любого импорта бота.
os.environ.update({
    "TELEGRAM_BOT_TOKEN": "42:TEST",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "ALLOWED_USER_IDS": "42",
    "ADMIN_USER_IDS": "1",
    "DB_PATH": os.path.join(tempfile.mkdtemp(), "import.sqlite3"),
})
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def app(monkeypatch, tmp_path):
    """Модуль bot.main с чистой базой, без задержки сбора сообщений и без чужого состояния."""
    from bot import main
    from bot.storage import Storage

    monkeypatch.setattr(main, "store", Storage(str(tmp_path / "bot.sqlite3")))
    monkeypatch.setattr(main, "COLLECT_DELAY", 0.05)
    monkeypatch.setattr(main, "conf", dataclasses.replace(main.conf, user_monthly_limit_usd=0))
    main.busy.clear()
    main.pending.clear()
    main.warned_long.clear()
    return main


@pytest.fixture
def use_claude(app, monkeypatch):
    """Подключает FakeClaude к боту: use_claude(FakeClaude(...))."""
    def install(fake):
        monkeypatch.setattr(app.claude, "client", fake.client())
        return fake
    return install
