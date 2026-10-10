"""Обращение к Claude API: потоковый ответ, fallback при отказе, подготовка истории, тексты ошибок."""

import html
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import anthropic
import httpx2

from .config import MODELS_WITH_FALLBACK

log = logging.getLogger(__name__)

# Серверный fallback: если классификатор безопасности отклонит запрос, API сам повторит его
# на рекомендованной модели внутри того же вызова.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Управление привязкой блоков размышлений: при смене модели или любом расхождении истории
# API отбросит несовместимые блоки вместо ошибки 400.
BINDING_BETA = "thinking-binding-controls-2026-08-01"

# Блоки, которые нельзя возвращать в историю, если они стоят до последнего блока `fallback`.
_DROP_BEFORE_FALLBACK = {"thinking", "redacted_thinking", "tool_use"}


@dataclass
class Attempt:
    """Одна попытка модели внутри запроса. При fallback их несколько, и каждая оплачивается."""
    model: str
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_write: int

    @property
    def prompt_tokens(self) -> int:
        """Полный размер промпта: и некэшированная часть, и прочитанная/записанная в кэш."""
        return self.input_tokens + self.cache_read + self.cache_write


@dataclass
class Reply:
    text: str
    content: list[dict] | None  # что сохранить в историю; None — не сохранять
    stop_reason: str | None
    refusal_category: str | None
    attempts: list[Attempt]  # расход по каждой попытке — для учёта стоимости
    served_by: str  # модель, которая написала итоговый ответ
    fell_back: bool  # ответила резервная модель (сейчас или по «липкой» маршрутизации)


def _history_content(blocks: list[dict]) -> list[dict]:
    """Готовит ответ модели к сохранению в историю. Пустой список — сохранять нечего."""
    last_fb = max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=-1)
    out = []
    for i, b in enumerate(blocks):
        t = b.get("type")
        if t == "fallback":
            continue
        if i < last_fb and t in _DROP_BEFORE_FALLBACK:
            continue
        # API не принимает пустые и пробельные текстовые блоки ни в одном месте сообщения.
        # Проверка привязки размышлений такие блоки игнорирует, так что удалять их безопасно.
        if t == "text" and not b.get("text", "").strip():
            continue
        out.append(b)
    if not any(b.get("type") == "text" for b in out):
        return []  # модель ничего не сказала (например, весь бюджет ушёл на размышления)
    if out[-1].get("type") == "text":
        out[-1] = {**out[-1], "text": out[-1]["text"].rstrip()}
    return out


def _attempts(usage, served_by: str, requested: str) -> list[Attempt]:
    """Расход по попыткам. Верхнеуровневый usage покрывает только попытку, давшую ответ,
    а usage.iterations — каждую, включая отклонённую модель при fallback."""
    out = []
    for it in getattr(usage, "iterations", None) or []:
        out.append(Attempt(
            model=getattr(it, "model", None) or requested,
            input_tokens=getattr(it, "input_tokens", 0) or 0,
            output_tokens=getattr(it, "output_tokens", 0) or 0,
            cache_read=getattr(it, "cache_read_input_tokens", 0) or 0,
            cache_write=getattr(it, "cache_creation_input_tokens", 0) or 0,
        ))
    if out:
        return out
    return [Attempt(
        model=served_by,
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        cache_read=usage.cache_read_input_tokens or 0,
        cache_write=usage.cache_creation_input_tokens or 0,
    )]


def _request_of(e: httpx2.TransportError) -> httpx2.Request:
    try:
        return e.request
    except RuntimeError:  # у исключения может не быть привязанного запроса
        return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


class ClaudeClient:
    def __init__(self, api_key: str, max_tokens: int):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.max_tokens = max_tokens

    async def ask(
        self,
        *,
        model: str,
        effort: str,
        system: str,
        messages: list[dict],
        on_text: Callable[[str], Awaitable[None]],
    ) -> Reply:
        """Отправляет историю, по мере генерации вызывает on_text(весь_текст_на_данный_момент)."""
        betas = [BINDING_BETA]
        extra = {}
        if model in MODELS_WITH_FALLBACK:
            betas.append(FALLBACK_BETA)
            extra["fallbacks"] = "default"

        text = ""
        try:
            async with self.client.beta.messages.stream(
                model=model,
                max_tokens=self.max_tokens,
                system=system,
                messages=messages,
                thinking={"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
                output_config={"effort": effort},
                cache_control={"type": "ephemeral"},  # кэширование истории: повторные ходы дешевле
                betas=betas,
                **extra,
            ) as stream:
                async for event in stream:
                    if event.type == "content_block_start" and event.content_block.type == "fallback":
                        # Модель передумала посередине ответа, дальше отвечает fallback-модель.
                        text += "\n\n"
                    elif event.type == "content_block_delta" and event.delta.type == "text_delta":
                        text += event.delta.text
                        await on_text(text)
                final = await stream.get_final_message()
        except httpx2.TransportError as e:
            # Обрыв связи посреди ответа SDK пропускает как исключение httpx2 — приводим к ошибке SDK.
            raise anthropic.APIConnectionError(message=f"Связь оборвалась посреди ответа: {e!r}",
                                               request=_request_of(e)) from e

        blocks = [b.model_dump(mode="json", exclude_none=True) for b in final.content]
        refusal_category = None
        content: list[dict] | None = _history_content(blocks) or None
        if final.stop_reason == "refusal":
            details = getattr(final, "stop_details", None)
            refusal_category = getattr(details, "category", None) if details else None
            content = None
        elif final.stop_reason == "model_context_window_exceeded":
            content = None  # ответ оборван из-за переполнения контекста — такой ход не сохраняем

        iterations = getattr(final.usage, "iterations", None) or []
        final_text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text") or text
        return Reply(
            text=final_text,
            content=content,
            stop_reason=final.stop_reason,
            refusal_category=refusal_category,
            attempts=_attempts(final.usage, final.model, model),
            served_by=final.model,
            fell_back=any(getattr(it, "type", None) == "fallback_message" for it in iterations),
        )


def _error_code(e: anthropic.APIStatusError) -> str | None:
    """error.details.error_code из тела ошибки, например enforced_spend_limit_reached."""
    body = e.body if isinstance(e.body, dict) else {}
    err = body.get("error") if isinstance(body.get("error"), dict) else body
    details = err.get("details") if isinstance(err, dict) else None
    return details.get("error_code") if isinstance(details, dict) else None


def error_text(e: Exception, model: str) -> str:
    """Понятное пользователю описание ошибки API (HTML для Telegram)."""
    if isinstance(e, anthropic.APIConnectionError):  # сюда же относится таймаут
        return "Нет связи с Claude API. Попробуйте ещё раз."
    if not isinstance(e, anthropic.APIStatusError):
        return "Внутренняя ошибка бота. Подробности — в логах."

    msg = str(getattr(e, "message", "") or e)
    low = msg.lower()
    raw = f"\n<code>{html.escape(msg[:500])}</code>"

    # Ошибка, пришедшая посреди стрима, имеет HTTP-статус 200, поэтому сначала смотрим тип из тела ответа.
    if e.type == "overloaded_error" or isinstance(e, anthropic.OverloadedError):
        return "Claude сейчас перегружен. Повторите через минуту или выберите другую модель в /settings."
    if isinstance(e, anthropic.RequestTooLargeError) or e.type == "request_too_large":
        return ("Запрос больше лимита API (32 МБ): в диалоге накопилось много файлов. "
                "Начните новый диалог: /new — и пришлите нужный файл ещё раз.")
    if isinstance(e, anthropic.AuthenticationError):
        return "API-ключ Anthropic недействителен. Проверьте ANTHROPIC_API_KEY в файле .env на сервере."
    if isinstance(e, anthropic.PermissionDeniedError):
        return "Anthropic отказал в доступе (403): модель или регион недоступны для этого аккаунта." + raw
    if isinstance(e, anthropic.NotFoundError):
        return f"Модель {html.escape(model)} недоступна для этого аккаунта (404). Выберите другую в /settings."
    if isinstance(e, anthropic.RateLimitError):
        if _error_code(e) == "enforced_spend_limit_reached":
            return ("Организация Anthropic исчерпала месячный лимит расходов своего тарифного уровня. "
                    "До начала следующего месяца запросы не пройдут (лимит можно поднять в Anthropic Console).")
        return "Превышен лимит запросов API. Попробуйте через минуту."
    if isinstance(e, anthropic.BadRequestError):
        if "credit balance" in low:
            hint = "На балансе Anthropic Console закончились деньги — пополните его в разделе Billing."
        elif "usage limits" in low:
            hint = "Достигнут лимит расходов, установленный в Anthropic Console (Billing → Spend limits)."
        elif "unsupported countries" in low or "unsupported region" in low:
            hint = "Anthropic не обслуживает страну, из которой идёт запрос (см. docs/deploy.md)."
        elif "prompt is too long" in low or "context window" in low:
            hint = "Диалог или файл не помещается в контекст модели. Начните новый диалог: /new"
        elif "many-image" in low:
            hint = "В диалоге слишком много картинок. Начните новый диалог: /new"
        else:
            hint = "Ошибка запроса к Claude."
        return hint + raw
    if e.status_code >= 500 or e.type == "api_error":
        return "Ошибка на стороне Anthropic. Повторите позже."
    return f"Claude API вернул ошибку ({html.escape(str(e.type or e.status_code))}). Попробуйте ещё раз."
