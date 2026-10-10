"""Настройки бота. Всё читается из переменных окружения (файл .env)."""

import os
from dataclasses import dataclass

# Модели, которые можно выбрать кнопками. Ключ — ID модели в API, значение — подпись на кнопке.
MODELS: dict[str, str] = {
    "claude-opus-5-5": "Opus 5.5",
    "claude-sonnet-5-5": "Sonnet 5.5",
    "claude-haiku-5-5": "Haiku 5.5",
    "claude-fable-5-1": "Fable 5.1",
}

# Цена за 1M токенов (вход, выход) в USD — только для подсчёта примерной стоимости в /usage.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.0, 50.0),
}

EFFORTS: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

# Модели, у которых есть серверный fallback при отказе классификатора безопасности.
# У Haiku 5.5 его нет: там отказ просто показывается пользователю.
MODELS_WITH_FALLBACK = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"}


def _ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


@dataclass(frozen=True)
class Config:
    telegram_token: str
    anthropic_api_key: str
    allowed_user_ids: set[int]
    admin_user_ids: set[int]
    default_model: str
    default_effort: str
    max_tokens: int
    db_path: str
    system_prompt: str


def load() -> Config:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not token or not key:
        raise SystemExit("Нужны TELEGRAM_BOT_TOKEN и ANTHROPIC_API_KEY (см. .env.example)")

    allowed = _ids(os.environ.get("ALLOWED_USER_IDS", ""))
    admins = _ids(os.environ.get("ADMIN_USER_IDS", "")) or allowed

    model = os.environ.get("DEFAULT_MODEL", "claude-opus-5-5")
    effort = os.environ.get("DEFAULT_EFFORT", "medium")
    if model not in MODELS:
        raise SystemExit(f"DEFAULT_MODEL должен быть одним из: {', '.join(MODELS)}")
    if effort not in EFFORTS:
        raise SystemExit(f"DEFAULT_EFFORT должен быть одним из: {', '.join(EFFORTS)}")

    return Config(
        telegram_token=token,
        anthropic_api_key=key,
        allowed_user_ids=allowed,
        admin_user_ids=admins,
        default_model=model,
        default_effort=effort,
        max_tokens=int(os.environ.get("MAX_TOKENS", "32000")),
        db_path=os.environ.get("DB_PATH", "/data/bot.sqlite3"),
        system_prompt=os.environ.get(
            "SYSTEM_PROMPT",
            "You are Claude, an AI assistant made by Anthropic. The user is chatting with you "
            "through a private Telegram bot. Reply in the user's language. Telegram renders "
            "basic Markdown (bold, italic, inline code, code blocks, links); tables are not "
            "rendered, so prefer lists over tables.",
        ),
    )
