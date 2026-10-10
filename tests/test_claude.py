"""ClaudeClient и тексты ошибок: настоящий SDK anthropic, подменён только HTTP-транспорт."""

import anthropic
import httpx2
import pytest
from fakes import FakeClaude, fallback, msg_end, msg_start, simple_reply, sse, text, thinking

from bot.claude import ClaudeClient, _history_content, error_text


async def ask(fake: FakeClaude, model: str = "claude-opus-5-5", effort: str = "medium") -> object:
    client = ClaudeClient("sk-ant-test", 64000)
    client.client = fake.client()
    seen: list[str] = []

    async def on_text(partial: str) -> None:
        seen.append(partial)

    reply = await client.ask(model=model, effort=effort, system="sys",
                             messages=[{"role": "user", "content": "q"}], on_text=on_text)
    reply.seen = seen
    return reply


async def test_request_shape_opus_has_fallback_and_caching():
    fake = FakeClaude(simple_reply("claude-opus-5-5", "Привет"))
    reply = await ask(fake)
    body = fake.requests[0]
    assert body["model"] == "claude-opus-5-5"
    assert body["max_tokens"] == 64000
    assert body["stream"] is True
    assert body["fallbacks"] == "default"
    assert body["output_config"] == {"effort": "medium"}
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["thinking"]["type"] == "adaptive"
    betas = fake.headers[0]["anthropic-beta"]
    assert "server-side-fallback-2026-07-01" in betas and "thinking-binding-controls-2026-08-01" in betas
    assert reply.text == "Привет"
    assert reply.seen[-1] == "Привет"
    assert [b["type"] for b in reply.content] == ["thinking", "text"]
    assert not reply.fell_back


async def test_haiku_has_no_fallback_parameter():
    # У Haiku 5.5 нет серверного fallback: список моделей вернул бы 400.
    fake = FakeClaude(simple_reply("claude-haiku-5-5", "ok"))
    await ask(fake, model="claude-haiku-5-5")
    assert "fallbacks" not in fake.requests[0]
    assert "server-side-fallback" not in fake.headers[0]["anthropic-beta"]


async def test_fallback_usage_counts_every_attempt():
    """api-3: отклонённая попытка тоже оплачивается — расход берём из usage.iterations."""
    fake = FakeClaude(sse([
        msg_start("claude-opus-5-5"),
        *thinking(0),
        *text(1, "Нач"),
        *fallback(2, "claude-opus-5-5", "claude-opus-4-8"),
        *thinking(3),
        *text(4, "ало ответа"),
        *msg_end("end_turn", 50, iterations=[
            {"type": "message", "model": "claude-opus-5-5", "input_tokens": 100, "output_tokens": 4000,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 120, "output_tokens": 50,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        ]),
    ]))
    reply = await ask(fake)
    assert reply.fell_back
    assert reply.served_by == "claude-opus-4-8"
    assert [(a.model, a.output_tokens) for a in reply.attempts] == [("claude-opus-5-5", 4000), ("claude-opus-4-8", 50)]
    # thinking до блока fallback выброшен, текст до него сохранён, сам маркер fallback не хранится
    assert [b["type"] for b in reply.content] == ["text", "thinking", "text"]
    assert reply.text == "Начало ответа"


async def test_empty_text_block_before_fallback_is_not_stored():
    """api-9: пустой текстовый блок посреди ответа API отклонит при следующем запросе."""
    fake = FakeClaude(sse([
        msg_start("claude-sonnet-5-5"),
        *thinking(0),
        *text(1, ""),
        *fallback(2, "claude-sonnet-5-5", "claude-sonnet-5"),
        *text(3, "Ответ"),
        *msg_end("end_turn", 10),
    ]))
    reply = await ask(fake, model="claude-sonnet-5-5")
    assert [b for b in reply.content if b["type"] == "text"] == [{"type": "text", "text": "Ответ"}]


def test_history_content_drops_whitespace_text_anywhere():
    blocks = [{"type": "text", "text": "  "}, {"type": "thinking", "thinking": "", "signature": "s"},
              {"type": "text", "text": "x \n"}]
    assert _history_content(blocks) == [{"type": "thinking", "thinking": "", "signature": "s"},
                                        {"type": "text", "text": "x"}]


async def test_thinking_only_reply_is_not_stored():
    """api-5: весь бюджет ушёл на размышления — сохранять в историю нечего."""
    fake = FakeClaude(sse([msg_start("claude-fable-5-1"), *thinking(0), *msg_end("max_tokens", 64000)]))
    reply = await ask(fake, model="claude-fable-5-1", effort="max")
    assert reply.content is None
    assert reply.stop_reason == "max_tokens"
    assert reply.text == ""


async def test_context_window_exceeded_is_not_stored():
    fake = FakeClaude(sse([msg_start("claude-opus-5-5"), *text(0, "обрыв"),
                           *msg_end("model_context_window_exceeded", 5)]))
    reply = await ask(fake)
    assert reply.content is None
    assert reply.stop_reason == "model_context_window_exceeded"


async def test_refusal_is_not_stored_and_has_category():
    fake = FakeClaude(sse([msg_start("claude-haiku-5-5"), *text(0, "Част"),
                           *msg_end("refusal", 3, stop_details={"type": "refusal", "category": "cyber"})]))
    reply = await ask(fake, model="claude-haiku-5-5")
    assert reply.content is None
    assert reply.refusal_category == "cyber"


async def test_mid_stream_overload_is_recognised():
    """api-6: ошибка посреди стрима приходит с HTTP-статусом 200 — тип берём из тела."""
    fake = FakeClaude(sse([msg_start("claude-opus-5-5"), *text(0, "пол"),
                           {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}]))
    with pytest.raises(anthropic.APIStatusError) as exc:
        await ask(fake)
    assert exc.value.status_code == 200
    assert "перегружен" in error_text(exc.value, "claude-opus-5-5")


def _status_error(status: int, body: dict) -> anthropic.APIStatusError:
    client = anthropic.AsyncAnthropic(api_key="x")
    response = httpx2.Response(status, json=body, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    return client._make_status_error(str(body), body=body, response=response)


@pytest.mark.parametrize("status,body,expected", [
    (413, {"type": "error", "error": {"type": "request_too_large", "message": "too large"}}, "/new"),
    (429, {"type": "error", "error": {"type": "rate_limit_error", "message": "limit",
                                      "details": {"error_code": "enforced_spend_limit_reached"}}},
     "месячный лимит"),
    (429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}, "через минуту"),
    (400, {"type": "error", "error": {"type": "invalid_request_error",
                                      "message": "Your credit balance is too low to access the Anthropic API."}},
     "закончились деньги"),
    (400, {"type": "error", "error": {"type": "invalid_request_error",
                                      "message": "You have reached your specified API usage limits."}},
     "Spend limits"),
    (400, {"type": "error", "error": {"type": "invalid_request_error", "message": "prompt is too long: 1200000"}},
     "/new"),
    (404, {"type": "error", "error": {"type": "not_found_error", "message": "model: claude-x"}}, "недоступна"),
    (403, {"type": "error", "error": {"type": "permission_error", "message": "region"}}, "403"),
    (401, {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}, "ANTHROPIC_API_KEY"),
    (529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}, "перегружен"),
    (500, {"type": "error", "error": {"type": "api_error", "message": "oops"}}, "стороне Anthropic"),
])
def test_error_texts(status, body, expected):
    assert expected in error_text(_status_error(status, body), "claude-opus-5-5")


def test_error_text_escapes_api_message():
    e = _status_error(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "<b>x</b>"}})
    assert "&lt;b&gt;" in error_text(e, "m") and "<b>x" not in error_text(e, "m")


def test_connection_error_text():
    e = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))
    assert "Нет связи" in error_text(e, "m")


class _CutStream(httpx2.AsyncByteStream):
    async def __aiter__(self):
        yield sse([msg_start("claude-opus-5-5"), *text(0, "пол")])
        raise httpx2.ReadError("connection reset")


async def test_connection_drop_mid_stream_becomes_sdk_error():
    """Обрыв TCP посреди ответа SDK отдаёт как httpx2.ReadError — бот должен видеть APIConnectionError."""
    client = ClaudeClient("sk-ant-test", 1000)
    client.client = anthropic.AsyncAnthropic(
        api_key="k", max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=_CutStream()))),
    )
    seen = []

    async def on_text(partial):
        seen.append(partial)

    with pytest.raises(anthropic.APIConnectionError) as exc:
        await client.ask(model="claude-opus-5-5", effort="low", system="s",
                         messages=[{"role": "user", "content": "q"}], on_text=on_text)
    assert seen == ["пол"]
    assert "Нет связи" in error_text(exc.value, "claude-opus-5-5")
