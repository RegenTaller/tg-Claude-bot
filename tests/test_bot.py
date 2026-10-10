"""Сквозные проверки: настоящий диспетчер aiogram + настоящий SDK anthropic, без сети."""

import asyncio
import dataclasses

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Update
from fakes import (
    USER_ID,
    FakeClaude,
    fallback,
    jpeg_bytes,
    make_bot,
    msg_end,
    msg_start,
    parse_mode,
    simple_reply,
    sse,
    text,
    thinking,
    update,
)


async def feed(app, bot, *updates):
    """Скармливает апдейты одновременно — как aiogram при long polling (handle_as_tasks=True)."""
    await asyncio.gather(*(app.dp.feed_update(bot, u) for u in updates))


async def test_text_message_roundtrip(app, use_claude):
    fake = use_claude(FakeClaude(simple_reply("claude-opus-5-5", "**Привет**", input_tokens=1000)))
    bot, tg = make_bot()
    await feed(app, bot, update(1, text="Как дела?"))

    assert fake.requests[0]["messages"] == [{"role": "user", "content": [{"type": "text", "text": "Как дела?"}]}]
    assert tg.last_text() == "<b>Привет</b>"
    conv = app.store.get_settings(USER_ID, "", "")["conversation_id"]
    assert [m["role"] for m in app.store.history(conv)] == ["user", "assistant"]
    assert app.store.spent_since(USER_ID, 0) > 0


async def test_album_becomes_one_turn(app, use_claude):
    fake = use_claude(FakeClaude(simple_reply("claude-opus-5-5", "Три котика")))
    bot, tg = make_bot({"p1": jpeg_bytes(), "p2": jpeg_bytes(), "p3": jpeg_bytes()})
    await feed(app, bot,
               update(1, photo="p1", caption="Сравни", media_group_id="g"),
               update(2, photo="p2", media_group_id="g"),
               update(3, photo="p3", media_group_id="g"))

    assert len(fake.requests) == 1
    blocks = fake.requests[0]["messages"][-1]["content"]
    assert [b["type"] for b in blocks] == ["image", "image", "image", "text"]
    assert not any("не отправлено" in t for t in tg.texts())


async def test_long_text_split_by_telegram_is_joined(app, use_claude):
    fake = use_claude(FakeClaude(simple_reply("claude-opus-5-5", "ok")))
    bot, _ = make_bot()
    await feed(app, bot, update(1, text="часть 1"), update(2, text="часть 2"))
    texts = [b["text"] for b in fake.requests[0]["messages"][-1]["content"]]
    assert texts == ["часть 1", "часть 2"]


async def test_message_while_busy_is_rejected_explicitly(app, use_claude):
    fake = use_claude(FakeClaude())
    bot, tg = make_bot()
    app.busy.add(USER_ID)
    await feed(app, bot, update(1, text="ещё вопрос"))
    assert "не отправлено" in tg.last_text()
    assert fake.requests == []


async def test_stranger_gets_id_and_no_api_call(app, use_claude):
    fake = use_claude(FakeClaude())
    bot, tg = make_bot()
    await feed(app, bot, update(1, text="привет", user_id=777))
    assert "777" in tg.last_text()
    assert fake.requests == []


async def test_oversized_dialog_is_refused_before_api(app, use_claude):
    """api-1: история с файлами больше 32 МБ — объясняем и не шлём запрос, который точно вернёт 413."""
    fake = use_claude(FakeClaude())
    bot, tg = make_bot()
    app.store.get_settings(USER_ID, app.conf.default_model, app.conf.default_effort)
    conv = app.store.new_conversation(USER_ID, "sys")
    big = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "A" * 31_000_000}}
    app.store.append(conv, "user", [big, {"type": "text", "text": "прочитай"}])
    app.store.append(conv, "assistant", [{"type": "text", "text": "прочитал"}])

    await feed(app, bot, update(1, text="а теперь?"))
    assert fake.requests == []
    assert "32 МБ" in tg.last_text() and "/new" in tg.last_text()


async def test_thinking_only_reply_is_explained_and_not_stored(app, use_claude):
    """api-5: модель всё потратила на размышления — честно говорим об этом и не засоряем историю."""
    use_claude(FakeClaude(sse([msg_start("claude-opus-5-5"), *thinking(0), *msg_end("max_tokens", 64000)])))
    bot, tg = make_bot()
    await feed(app, bot, update(1, text="сложный вопрос"))
    assert "размышления" in tg.last_text()
    conv = app.store.get_settings(USER_ID, "", "")["conversation_id"]
    assert app.store.history(conv) == []


async def test_monthly_limit_blocks_regular_user(app, use_claude, monkeypatch):
    monkeypatch.setattr(app, "conf", dataclasses.replace(app.conf, user_monthly_limit_usd=1.0))
    fake = use_claude(FakeClaude())
    app.store.add_usage(USER_ID, "claude-opus-5-5", 0, 0, 0, 0, 1.5)
    bot, tg = make_bot()
    await feed(app, bot, update(1, text="вопрос"))
    assert "Лимит расходов" in tg.last_text()
    assert fake.requests == []


async def test_fallback_reply_is_marked_and_both_attempts_are_billed(app, use_claude):
    use_claude(FakeClaude(sse([
        msg_start("claude-opus-5-5"),
        *fallback(0, "claude-opus-5-5", "claude-opus-4-8"),
        *text(1, "Ответ"),
        *msg_end("end_turn", 10, iterations=[
            {"type": "message", "model": "claude-opus-5-5", "input_tokens": 1000, "output_tokens": 0,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 1000, "output_tokens": 10,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        ]),
    ])))
    bot, tg = make_bot()
    await feed(app, bot, update(1, text="вопрос"))
    assert "резервная модель claude-opus-4-8" in tg.last_text()
    models = {r["model"]: r for r in app.store.usage_since(0)}
    assert set(models) == {"claude-opus-5-5", "claude-opus-4-8"}
    assert models["claude-opus-4-8"]["cost"] > 0  # не «бесплатно», как было раньше


async def test_mid_stream_error_keeps_partial_text(app, use_claude):
    use_claude(FakeClaude(sse([msg_start("claude-opus-5-5"), *text(0, "Половина ответа"),
                               {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}])))
    bot, tg = make_bot()
    await feed(app, bot, update(1, text="вопрос"))
    final = tg.last_text()
    assert "Половина ответа" in final and "перегружен" in final
    assert "ошибку 200" not in final


async def test_html_rejected_by_telegram_falls_back_to_plain_text(app, use_claude):
    use_claude(FakeClaude(simple_reply("claude-opus-5-5", "**Жирный** текст")))
    bot, tg = make_bot()

    def reject_final_html(method):
        if (type(method).__name__ == "EditMessageText" and parse_mode(method) == "HTML"
                and not method.text.endswith("▍")):
            raise TelegramBadRequest(method=method, message="Bad Request: can't parse entities")

    tg.hooks.append(reject_final_html)
    await feed(app, bot, update(1, text="вопрос"))
    last = tg.calls[-1]
    assert parse_mode(last) is None and last.text == "Жирный текст"


async def test_long_dialog_warning_is_shown_once(app, use_claude):
    use_claude(FakeClaude(simple_reply("claude-opus-5-5", "раз", cache_read=200_000),
                          simple_reply("claude-opus-5-5", "два", cache_read=200_000)))
    bot, tg = make_bot()
    await feed(app, bot, update(1, text="вопрос 1"))
    assert "тыс. токенов" in tg.last_text()
    await feed(app, bot, update(2, text="вопрос 2"))
    assert "тыс. токенов" not in tg.last_text()


async def test_callback_on_old_inaccessible_message_does_not_crash(app):
    bot, tg = make_bot()
    upd = Update.model_validate({"update_id": 999, "callback_query": {
        "id": "cb1", "chat_instance": "ci", "data": "effort:high",
        "from": {"id": USER_ID, "is_bot": False, "first_name": "Тест"},
        "message": {"chat": {"id": USER_ID, "type": "private"}, "message_id": 5, "date": 0},
    }})
    await app.dp.feed_update(bot, upd)
    assert app.store.get_settings(USER_ID, "", "")["effort"] == "high"
    assert type(tg.calls[-1]).__name__ == "AnswerCallbackQuery"


async def test_setting_change_mid_dialog_warns_about_cache(app):
    bot, tg = make_bot()
    app.store.get_settings(USER_ID, app.conf.default_model, app.conf.default_effort)
    conv = app.store.new_conversation(USER_ID, "sys")
    app.store.append(conv, "user", [{"type": "text", "text": "x"}])
    upd = Update.model_validate({"update_id": 1000, "callback_query": {
        "id": "cb2", "chat_instance": "ci", "data": "model:claude-sonnet-5-5",
        "from": {"id": USER_ID, "is_bot": False, "first_name": "Тест"},
    }})
    await app.dp.feed_update(bot, upd)
    assert "Кэш" in tg.calls[-1].text


def test_plain_chunks_respect_utf16_limit(app):
    chunks = app.plain_chunks("😀" * 3000 + "a" * 10)
    assert all(len(c.encode("utf-16-le")) // 2 <= app.TG_LIMIT for c in chunks)
    assert "".join(chunks) == "😀" * 3000 + "a" * 10
