"""Обращение к Claude API: потоковый ответ, fallback при отказе, подготовка истории."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import anthropic

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
class Reply:
    text: str
    content: list[dict] | None  # что сохранить в историю; None — не сохранять (отказ)
    stop_reason: str | None
    refusal_category: str | None
    usage: object
    served_by: str


def _history_content(blocks: list[dict]) -> list[dict]:
    """Готовит ответ модели к сохранению в историю по правилам API для fallback-ответов."""
    last_fb = max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=-1)
    out = []
    for i, b in enumerate(blocks):
        t = b.get("type")
        if t == "fallback":
            continue
        if i < last_fb and t in _DROP_BEFORE_FALLBACK:
            continue
        out.append(b)
    # API не принимает пустой или пробельный текстовый блок в конце ассистентского сообщения.
    while out and out[-1].get("type") == "text" and not out[-1].get("text", "").strip():
        out.pop()
    if out and out[-1].get("type") == "text":
        out[-1] = {**out[-1], "text": out[-1]["text"].rstrip()}
    return out


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

        blocks = [b.model_dump(mode="json", exclude_none=True) for b in final.content]
        refusal_category = None
        content: list[dict] | None = _history_content(blocks)
        if final.stop_reason == "refusal":
            details = getattr(final, "stop_details", None)
            refusal_category = getattr(details, "category", None) if details else None
            content = None

        final_text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text") or text
        return Reply(
            text=final_text,
            content=content or None,
            stop_reason=final.stop_reason,
            refusal_category=refusal_category,
            usage=final.usage,
            served_by=final.model,
        )
