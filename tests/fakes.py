"""Подделки для тестов: Telegram Bot API (сессия aiogram) и потоковый ответ Claude API (SSE).

Код бота работает с настоящими aiogram и anthropic SDK — подменяется только сетевой слой."""

import io
import json
from collections.abc import Callable
from datetime import datetime

import anthropic
import httpx2
from aiogram import Bot
from aiogram.client.default import Default
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import File, Message, Update
from PIL import Image

USER_ID = 42

# ---------- Telegram ----------


class FakeTelegram(BaseSession):
    """Сессия aiogram без сети: записывает вызовы Bot API и отвечает правдоподобными объектами."""

    def __init__(self, files: dict[str, bytes] | None = None):
        super().__init__()
        self.files = files or {}
        self.calls: list[TelegramMethod] = []
        self.hooks: list[Callable[[TelegramMethod], None]] = []  # могут бросить исключение Telegram
        self._next_id = 1000

    async def close(self) -> None:
        pass

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):
        for hook in self.hooks:
            hook(method)
        self.calls.append(method)
        name = type(method).__name__
        if name == "GetFile":
            return File(file_id=method.file_id, file_unique_id="u-" + method.file_id,
                        file_path=f"files/{method.file_id}")
        if name in ("SendMessage", "EditMessageText"):
            if name == "SendMessage":
                self._next_id += 1
                message_id = self._next_id
            else:
                message_id = method.message_id
            return Message.model_validate(
                {"message_id": message_id, "date": datetime.now(), "text": method.text,
                 "chat": {"id": method.chat_id, "type": "private"}},
                context={"bot": bot},
            )
        return True  # SendChatAction, SetMyCommands, AnswerCallbackQuery и т.п.

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield self.files[url.rsplit("/", 1)[-1]]

    # --- удобства для проверок ---

    def texts(self) -> list[str]:
        """Тексты всех отправленных и отредактированных сообщений по порядку."""
        return [m.text for m in self.calls if type(m).__name__ in ("SendMessage", "EditMessageText")]

    def last_text(self) -> str:
        return self.texts()[-1]


def parse_mode(method: TelegramMethod) -> str | None:
    pm = getattr(method, "parse_mode", None)
    return "HTML" if isinstance(pm, Default) else pm


def make_bot(files: dict[str, bytes] | None = None) -> tuple[Bot, FakeTelegram]:
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    session = FakeTelegram(files)
    bot = Bot("42:TEST", session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return bot, session


_update_id = 0


def update(message_id: int, *, text: str | None = None, photo: str | None = None, document: dict | None = None,
           caption: str | None = None, media_group_id: str | None = None, user_id: int = USER_ID) -> Update:
    global _update_id
    _update_id += 1
    msg: dict = {"message_id": message_id, "date": 0, "chat": {"id": user_id, "type": "private"},
                 "from": {"id": user_id, "is_bot": False, "first_name": "Тест"}}
    if text is not None:
        msg["text"] = text
    if photo is not None:
        msg["photo"] = [{"file_id": photo, "file_unique_id": "u-" + photo, "width": 100, "height": 80}]
    if document is not None:
        msg["document"] = {"file_unique_id": "u-" + document["file_id"], **document}
    if caption is not None:
        msg["caption"] = caption
    if media_group_id is not None:
        msg["media_group_id"] = media_group_id
    return Update.model_validate({"update_id": _update_id, "message": msg})


def jpeg_bytes(w: int = 64, h: int = 48) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, "JPEG")
    return buf.getvalue()


# ---------- Claude API (SSE) ----------


def sse(events: list[dict]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def msg_start(model: str, input_tokens: int = 100, cache_read: int = 0, cache_write: int = 0) -> dict:
    return {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": 1,
                  "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write}}}


def block(i: int, content_block: dict) -> dict:
    return {"type": "content_block_start", "index": i, "content_block": content_block}


def block_stop(i: int) -> dict:
    return {"type": "content_block_stop", "index": i}


def text_delta(i: int, text: str) -> dict:
    return {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": text}}


def thinking(i: int) -> list[dict]:
    return [block(i, {"type": "thinking", "thinking": "", "signature": ""}),
            {"type": "content_block_delta", "index": i, "delta": {"type": "signature_delta", "signature": "sig"}},
            block_stop(i)]


def text(i: int, value: str) -> list[dict]:
    return [block(i, {"type": "text", "text": ""}), text_delta(i, value), block_stop(i)]


def fallback(i: int, frm: str, to: str, category: str = "cyber") -> list[dict]:
    return [block(i, {"type": "fallback", "from": {"model": frm}, "to": {"model": to},
                      "trigger": {"type": "refusal", "category": category}}), block_stop(i)]


def msg_end(stop_reason: str, output_tokens: int, iterations: list[dict] | None = None,
            stop_details: dict | None = None) -> list[dict]:
    usage: dict = {"output_tokens": output_tokens}
    if iterations is not None:
        usage["iterations"] = iterations
    delta: dict = {"stop_reason": stop_reason, "stop_sequence": None}
    if stop_details is not None:
        delta["stop_details"] = stop_details
    return [{"type": "message_delta", "delta": delta, "usage": usage}, {"type": "message_stop"}]


def simple_reply(model: str, answer: str, **usage) -> bytes:
    return sse([msg_start(model, **usage), *thinking(0), *text(1, answer), *msg_end("end_turn", 20)])


class FakeClaude:
    """Подменяет HTTP-транспорт клиента anthropic: отдаёт заготовленные SSE-ответы и запоминает запросы."""

    def __init__(self, *responses: bytes | tuple[int, dict]):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.headers: list[dict] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        self.headers.append(dict(request.headers))
        body = self.responses.pop(0)
        if isinstance(body, tuple):  # (статус, json-тело ошибки)
            status, payload = body
            return httpx2.Response(status, json=payload, headers={"request-id": "req_test"})
        return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})

    def client(self) -> anthropic.AsyncAnthropic:
        return anthropic.AsyncAnthropic(
            api_key="sk-ant-test", max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self.handler)),
        )
