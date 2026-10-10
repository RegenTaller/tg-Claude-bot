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

# Цены в USD за 1M токенов: (вход, выход, чтение из кэша) — только для оценки расхода в /usage.
# Запись в кэш (TTL 5 минут) стоит 1.25 × цены входа у всех моделей.
# Цены на октябрь 2026; точный счёт — в Anthropic Console.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    # Резервные модели серверного fallback: на них API переключается, если основная модель отказала.
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
}
CACHE_WRITE_MULTIPLIER = 1.25

# Haiku 5.5 тарифицирует весь запрос по второй сетке, если промпт длиннее 100K токенов.
LONG_PROMPT_PRICES: dict[str, tuple[int, tuple[float, float, float]]] = {
    "claude-haiku-5-5": (100_000, (0.50, 2.50, 0.05)),
}


def _price_key(model: str) -> str | None:
    """Ключ из PRICES для модели. Самый длинный префикс, чтобы claude-opus-5-5 не спутать с claude-opus-5."""
    keys = [k for k in PRICES if model == k or model.startswith(k + "-")]
    return max(keys, key=len) if keys else None


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> float | None:
    """Оценка стоимости одного обращения к модели. None — цена модели неизвестна."""
    key = _price_key(model)
    if key is None:
        return None
    pin, pout, pread = PRICES[key]
    if key in LONG_PROMPT_PRICES:
        threshold, long_prices = LONG_PROMPT_PRICES[key]
        if input_tokens + cache_read + cache_write > threshold:
            pin, pout, pread = long_prices
    return (
        input_tokens * pin
        + cache_write * pin * CACHE_WRITE_MULTIPLIER
        + cache_read * pread
        + output_tokens * pout
    ) / 1e6


def _ids(name: str) -> set[int]:
    raw = os.environ.get(name, "")
    try:
        return {int(x) for x in raw.replace(" ", "").split(",") if x}
    except ValueError:
        raise SystemExit(f"{name}: нужны числовые Telegram ID через запятую, например 123456789,987654321")


def _int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name, "").strip() or str(default)
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name} должен быть целым числом, сейчас: {raw!r}")
    if not lo <= value <= hi:
        raise SystemExit(f"{name} должен быть от {lo} до {hi}, сейчас: {value}")
    return value


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip() or str(default)
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"{name} должен быть числом, сейчас: {raw!r}")
    if value < 0:
        raise SystemExit(f"{name} не может быть отрицательным")
    return value


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
    user_monthly_limit_usd: float  # 0 — без лимита; админов не касается
    history_retention_days: int  # 0 — хранить старые диалоги вечно


def load() -> Config:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not token or not key:
        raise SystemExit("Нужны TELEGRAM_BOT_TOKEN и ANTHROPIC_API_KEY (см. .env.example)")

    model = os.environ.get("DEFAULT_MODEL", "").strip() or "claude-opus-5-5"
    effort = os.environ.get("DEFAULT_EFFORT", "").strip() or "medium"
    if model not in MODELS:
        raise SystemExit(f"DEFAULT_MODEL должен быть одним из: {', '.join(MODELS)}")
    if effort not in EFFORTS:
        raise SystemExit(f"DEFAULT_EFFORT должен быть одним из: {', '.join(EFFORTS)}")

    return Config(
        telegram_token=token,
        anthropic_api_key=key,
        allowed_user_ids=_ids("ALLOWED_USER_IDS"),
        admin_user_ids=_ids("ADMIN_USER_IDS"),
        default_model=model,
        default_effort=effort,
        # Размышления модели тоже расходуют max_tokens, поэтому запас нужен с учётом thinking.
        max_tokens=_int("MAX_TOKENS", 64000, 1024, 128000),
        db_path=os.environ.get("DB_PATH", "").strip() or "/data/bot.sqlite3",
        system_prompt=os.environ.get("SYSTEM_PROMPT", "").strip() or (
            "You are Claude, an AI assistant made by Anthropic. The user is chatting with you "
            "through a private Telegram bot. Reply in the user's language. Telegram renders "
            "basic Markdown (bold, italic, inline code, code blocks, links); tables are not "
            "rendered, so prefer lists over tables."
        ),
        user_monthly_limit_usd=_float("USER_MONTHLY_LIMIT_USD", 0),
        history_retention_days=_int("HISTORY_RETENTION_DAYS", 30, 0, 36500),
    )
