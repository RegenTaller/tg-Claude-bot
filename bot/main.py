"""Telegram-бот: пересылает сообщения в Claude и присылает ответы обратно."""

import asyncio
import contextlib
import html
import json
import logging
import re
import time
from datetime import datetime, timezone

import anthropic
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from chatgpt_md_converter import split_html_for_telegram, telegram_format

from . import config as cfg
from .claude import ClaudeClient, Reply, error_text
from .content import UserError, batch_content
from .storage import Storage

log = logging.getLogger("bot")

BTN_SETTINGS = "⚙️ Модель и effort"
BTN_NEW = "🆕 Новый диалог"
MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_SETTINGS)]],
    resize_keyboard=True,
    is_persistent=True,
)

EDIT_INTERVAL = 1.5  # секунд между обновлениями сообщения во время генерации
STREAM_PREVIEW_LIMIT = 3900  # пока идёт генерация, показываем хвост не длиннее этого
TG_LIMIT = 4096  # максимальная длина сообщения Telegram (в единицах UTF-16)
# Альбом из нескольких фото и длинный текст, который Telegram режет на части, приходят отдельными
# сообщениями. Ждём, пока поток частей затихнет, и отправляем всё в Claude одним ходом.
COLLECT_DELAY = 1.0
COLLECT_MAX_ROUNDS = 10
REQUEST_LIMIT_BYTES = 30_000_000  # API принимает запрос до 32 МБ; оставляем запас
LONG_CONTEXT_TOKENS = 150_000  # с такого размера диалога советуем начать новый
LONG_CONTEXT_TOKENS_HAIKU = 100_000  # у Haiku 5.5 промпт длиннее 100K тарифицируется в 5 раз дороже
PURGE_INTERVAL = 24 * 3600  # раз в сутки удаляем старые брошенные диалоги

conf = cfg.load()
store = Storage(conf.db_path)
claude = ClaudeClient(conf.anthropic_api_key, conf.max_tokens)
dp = Dispatcher()
busy: set[int] = set()  # пользователи, у которых сейчас идёт запрос
pending: dict[int, list[Message]] = {}  # сообщения, которые собираются в один ход
warned_long: set[int] = set()  # диалоги, в которых уже предупредили о большом размере


# ---------- доступ и лимиты ----------

def allowed(user_id: int) -> bool:
    return user_id in conf.allowed_user_ids or user_id in conf.admin_user_ids or store.is_allowed_extra(user_id)


def is_admin(user_id: int) -> bool:
    return user_id in conf.admin_user_ids


async def deny_message(message: Message) -> None:
    await message.answer(
        "Это приватный бот. Доступа у вас нет.\n"
        f"Ваш Telegram ID: <code>{message.from_user.id}</code> — передайте его владельцу бота."
    )


def month_start() -> float:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def over_monthly_limit(user_id: int) -> bool:
    limit = conf.user_monthly_limit_usd
    return limit > 0 and not is_admin(user_id) and store.spent_since(user_id, month_start()) >= limit


# ---------- настройки ----------

def settings_text(user_id: int) -> str:
    s = store.get_settings(user_id, conf.default_model, conf.default_effort)
    return (
        f"Модель: <b>{cfg.MODELS[s['model']]}</b>\n"
        f"Effort: <b>{s['effort']}</b>\n\n"
        "Effort — сколько модель думает перед ответом: low быстрее и дешевле, max — глубже и дороже.\n"
        "Смена модели или effort посреди диалога сбрасывает кэш: следующий ответ заново оплатит всю "
        "историю по полной цене. Дешевле менять настройки сразу после /new."
    )


def settings_kb(user_id: int) -> InlineKeyboardMarkup:
    s = store.get_settings(user_id, conf.default_model, conf.default_effort)
    models = [
        InlineKeyboardButton(text=("✅ " if m == s["model"] else "") + label, callback_data=f"model:{m}")
        for m, label in cfg.MODELS.items()
    ]
    efforts = [
        InlineKeyboardButton(text=("✅ " if e == s["effort"] else "") + label, callback_data=f"effort:{e}")
        for e, label in cfg.EFFORTS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=[models[:2], models[2:], efforts[:3], efforts[3:]])


async def show_settings(message: Message) -> None:
    await message.answer(settings_text(message.from_user.id), reply_markup=settings_kb(message.from_user.id))


# ---------- команды ----------

@dp.message(F.chat.type != ChatType.PRIVATE)
async def ignore_groups(message: Message) -> None:
    """Бот работает только в личных чатах."""


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if not allowed(message.from_user.id):
        return await deny_message(message)
    await message.answer(
        "Привет! Пишите вопрос — я передам его Claude и пришлю ответ.\n"
        "Можно присылать фото (и альбомы), PDF и текстовые файлы.\n\n"
        "/new — новый диалог\n/settings — модель и effort\n/usage — расход за 30 дней\n/id — ваш Telegram ID",
        reply_markup=MAIN_KB,
    )
    await show_settings(message)


@dp.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@dp.message(Command("settings"))
@dp.message(F.text == BTN_SETTINGS)
async def cmd_settings(message: Message) -> None:
    if not allowed(message.from_user.id):
        return await deny_message(message)
    await show_settings(message)


@dp.message(Command("new"))
@dp.message(F.text == BTN_NEW)
async def cmd_new(message: Message) -> None:
    if not allowed(message.from_user.id):
        return await deny_message(message)
    store.get_settings(message.from_user.id, conf.default_model, conf.default_effort)
    store.new_conversation(message.from_user.id, conf.system_prompt)
    await message.answer("Начат новый диалог. Предыдущий контекст забыт.", reply_markup=MAIN_KB)


@dp.message(Command("usage"))
async def cmd_usage(message: Message) -> None:
    uid = message.from_user.id
    if not allowed(uid):
        return await deny_message(message)
    admin = is_admin(uid)
    rows = store.usage_since(time.time() - 30 * 86400, None if admin else uid)
    lines, total, unpriced = [], 0.0, 0
    for r in rows:
        total += r["cost"]
        unpriced += r["unpriced"]
        who = f"{r['user_id']} · " if admin else ""
        extra = f" (+{r['unpriced']} без известной цены)" if r["unpriced"] else ""
        lines.append(f"{who}{cfg.MODELS.get(r['model'], r['model'])}: {r['n']} обращ., ≈${r['cost']:.2f}{extra}")
    if lines:
        lines.append(f"\nИтого за 30 дней ≈ <b>${total:.2f}</b> (оценка; точный счёт — в Anthropic Console)")
        if unpriced:
            lines.append("Для части обращений цена модели неизвестна боту — они в сумму не вошли.")
    else:
        lines.append("За 30 дней запросов не было.")
    if conf.user_monthly_limit_usd > 0 and not admin:
        spent = store.spent_since(uid, month_start())
        lines.append(f"\nВ этом месяце: ≈${spent:.2f} из ${conf.user_monthly_limit_usd:.2f}")
    await message.answer("\n".join(lines))


@dp.message(Command("allow"))
async def cmd_allow(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id):
        return
    if not command.args or not command.args.strip().isdigit():
        ids = ", ".join(map(str, store.allowed_list())) or "нет"
        return await message.answer(f"Использование: /allow &lt;telegram_id&gt;\nДобавлены командой: {ids}")
    store.allow(int(command.args.strip()))
    await message.answer("Готово, доступ выдан.")


@dp.message(Command("deny"))
async def cmd_deny(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id):
        return
    if not command.args or not command.args.strip().isdigit():
        return await message.answer("Использование: /deny &lt;telegram_id&gt;")
    store.deny(int(command.args.strip()))
    await message.answer("Доступ отозван (если ID не прописан в .env).")


@dp.callback_query(F.data.startswith("model:") | F.data.startswith("effort:"))
async def on_setting(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    if not allowed(uid):
        return await cb.answer("Нет доступа", show_alert=True)
    kind, value = cb.data.split(":", 1)
    s = store.get_settings(uid, conf.default_model, conf.default_effort)
    changed = False
    if kind == "model" and value in cfg.MODELS and value != s["model"]:
        store.set_model(uid, value)
        changed = True
    elif kind == "effort" and value in cfg.EFFORTS and value != s["effort"]:
        store.set_effort(uid, value)
        changed = True
    # Сообщение старше 48 часов Telegram отдаёт как недоступное — его уже нельзя отредактировать.
    if changed and isinstance(cb.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await cb.message.edit_text(settings_text(uid), reply_markup=settings_kb(uid))
    mid_dialog = changed and s["conversation_id"] and store.message_count(s["conversation_id"]) > 0
    await cb.answer("Сохранено. Кэш этого диалога сбросится: следующий ответ дороже обычного."
                    if mid_dialog else "Сохранено")


# ---------- отправка ответа ----------

def render(text: str) -> list[str]:
    """Markdown от Claude → HTML для Telegram, порезанный на куски.
    Запас до 4096 — потому что Telegram считает длину в UTF-16, а эмодзи занимают по две единицы."""
    return split_html_for_telegram(telegram_format(text), trim_empty_leading_lines=True, max_length=4000)


def plain_chunks(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Режет простой текст на куски, которые Telegram примет (длина считается в UTF-16)."""
    chunks, cur, size = [], [], 0
    for ch in text:
        n = 2 if ord(ch) > 0xFFFF else 1
        if size + n > limit:
            chunks.append("".join(cur))
            cur, size = [], 0
        cur.append(ch)
        size += n
    if cur:
        chunks.append("".join(cur))
    return chunks


_TAG = re.compile(r"<[^>]+>")


async def tg_retry(call):
    """Выполняет вызов Telegram API, пережидая flood control (TelegramRetryAfter)."""
    for attempt in range(3):
        try:
            return await call()
        except TelegramRetryAfter as e:
            if attempt == 2:
                raise
            await asyncio.sleep(e.retry_after)


async def edit(msg: Message, html_text: str) -> None:
    await tg_retry(lambda: msg.edit_text(html_text))


async def safe_edit(msg: Message, html_text: str) -> float:
    """Обновляет черновик ответа. Возвращает, сколько секунд не трогать сообщение (flood control)."""
    try:
        await msg.edit_text(html_text)
    except TelegramRetryAfter as e:
        return float(e.retry_after)  # не ждём здесь, чтобы не тормозить чтение ответа Claude
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            log.debug("черновик не обновлён: %s", e)
    return 0.0


async def send_chunk(placeholder: Message, chunk_html: str, replace: bool) -> None:
    """Отправляет кусок ответа: первый заменяет «Claude думает…», остальные — новыми сообщениями.
    Если Telegram не принял HTML, отправляет этот кусок обычным текстом."""
    try:
        if replace:
            await tg_retry(lambda: placeholder.edit_text(chunk_html))
        else:
            await tg_retry(lambda: placeholder.answer(chunk_html))
        return
    except TelegramBadRequest as e:
        log.warning("Telegram не принял HTML (%s), отправляю кусок обычным текстом", e)
    for i, part in enumerate(plain_chunks(html.unescape(_TAG.sub("", chunk_html))) or ["(пустой ответ)"]):
        if replace and i == 0:
            try:
                await tg_retry(lambda p=part: placeholder.edit_text(p, parse_mode=None))
                continue
            except TelegramBadRequest:
                pass
        await tg_retry(lambda p=part: placeholder.answer(p, parse_mode=None))


async def send_final(placeholder: Message, text: str) -> None:
    """Заменяет «думает…» на итоговый ответ; длинный ответ отправляется несколькими сообщениями."""
    for i, chunk in enumerate(render(text) or ["(пустой ответ)"]):
        await send_chunk(placeholder, chunk, replace=(i == 0))


# ---------- основной обработчик ----------

def payload_bytes(system: str, messages: list[dict]) -> int:
    """Оценка размера тела запроса к API (с запасом: не-ASCII символы считаются как \\uXXXX)."""
    return len(json.dumps({"system": system, "messages": messages}))


def record_usage(user_id: int, reply: Reply) -> None:
    for a in reply.attempts:
        cost = cfg.cost_usd(a.model, a.input_tokens, a.output_tokens, a.cache_read, a.cache_write)
        if cost is None:
            log.warning("Нет цены для модели %s — расход не попадёт в сумму /usage", a.model)
        store.add_usage(user_id, a.model, a.input_tokens, a.output_tokens, a.cache_read, a.cache_write, cost)


def notes_for(reply: Reply, conv_id: int) -> list[str]:
    """Пометки под ответом: обрезан, ответила резервная модель, диалог стал большим."""
    notes = []
    if reply.stop_reason == "max_tokens":
        notes.append("_(ответ обрезан по лимиту длины)_")
    if reply.fell_back:
        notes.append(f"_(ответила резервная модель {reply.served_by})_")
    prompt = reply.attempts[-1].prompt_tokens
    limit = LONG_CONTEXT_TOKENS_HAIKU if reply.served_by.startswith("claude-haiku") else LONG_CONTEXT_TOKENS
    if prompt >= limit and conv_id not in warned_long:
        warned_long.add(conv_id)
        notes.append(f"_(в диалоге уже ≈{prompt // 1000} тыс. токенов, и каждое сообщение пересылает его "
                     "целиком. Если тема сменилась — начните новый: /new)_")
    return notes


async def answer_batch(batch: list[Message], bot: Bot) -> None:
    """Один ход диалога: собрать содержимое, спросить Claude, показать и сохранить ответ."""
    first = batch[0]
    uid = first.from_user.id
    if over_monthly_limit(uid):
        return await first.answer(
            f"Лимит расходов на этот месяц (${conf.user_monthly_limit_usd:.2f}) исчерпан. "
            "Он обнулится 1-го числа; если нужно больше — напишите владельцу бота."
        )
    try:
        user_content = await batch_content(batch, bot)
    except UserError as e:
        return await first.answer(str(e))

    settings = store.get_settings(uid, conf.default_model, conf.default_effort)
    conv_id = settings["conversation_id"] or store.new_conversation(uid, conf.system_prompt)
    conv = store.conversation(conv_id)
    messages = store.history(conv_id) + [{"role": "user", "content": user_content}]
    if payload_bytes(conv["system_prompt"], messages) > REQUEST_LIMIT_BYTES:
        return await first.answer(
            "Диалог стал слишком большим из-за файлов: запрос к Claude не может быть больше 32 МБ. "
            "Начните новый диалог (/new) и пришлите файл ещё раз."
        )

    placeholder = await first.answer("⏳ <i>Claude думает…</i>")
    shown = ""  # текст, который уже успели показать пользователю
    next_edit = 0.0

    async def on_text(partial: str) -> None:
        nonlocal shown, next_edit
        shown = partial
        now = time.monotonic()
        if now < next_edit:
            return
        next_edit = now + EDIT_INTERVAL
        pause = await safe_edit(placeholder, html.escape(partial[-STREAM_PREVIEW_LIMIT:]) + " ▍")
        if pause:
            next_edit = time.monotonic() + pause

    try:
        reply = await claude.ask(
            model=settings["model"],
            effort=settings["effort"],
            system=conv["system_prompt"],
            messages=messages,
            on_text=on_text,
        )
    except anthropic.APIError as e:
        log.error("Ошибка Claude API: %r, request_id=%s", e, getattr(e, "request_id", None))
        text = error_text(e, settings["model"])
        if shown:  # часть ответа уже была на экране — не прячем её
            text = f"{html.escape(shown[-3000:])}\n\n⚠️ Ответ прерван. {text}"
        return await edit(placeholder, text)

    record_usage(uid, reply)

    if reply.stop_reason == "refusal":
        cat = f" (категория: {reply.refusal_category})" if reply.refusal_category else ""
        return await edit(placeholder, f"Claude отказался отвечать на этот запрос{cat}. Попробуйте переформулировать.")
    if reply.stop_reason == "model_context_window_exceeded":
        return await edit(placeholder, "Диалог не помещается в контекст модели, ответ оборван и не сохранён. "
                                       "Начните новый: /new")
    if reply.content is None:
        if reply.stop_reason == "max_tokens":
            why = ("Модель израсходовала весь лимит длины ответа на размышления и не успела ответить. "
                   "Попробуйте effort пониже или повторите вопрос.")
        else:
            why = "Модель вернула пустой ответ. Повторите вопрос или выберите effort пониже."
        return await edit(placeholder, why + " Этот ход не сохранён в диалоге.")

    store.append(conv_id, "user", user_content)
    store.append(conv_id, "assistant", reply.content)
    await send_final(placeholder, "\n\n".join([reply.text, *notes_for(reply, conv_id)]))


@dp.message()
async def on_message(message: Message, bot: Bot) -> None:
    uid = message.from_user.id
    if not allowed(uid):
        return await deny_message(message)
    if uid in pending:  # продолжение альбома или длинного текста — добавляем к собираемому ходу
        pending[uid].append(message)
        return
    if uid in busy:
        return await message.answer(
            "Подождите, предыдущий ответ ещё генерируется. Это сообщение не отправлено — "
            "пришлите его ещё раз после ответа."
        )

    batch = pending[uid] = [message]
    busy.add(uid)
    try:
        with contextlib.suppress(TelegramBadRequest, TelegramRetryAfter):
            await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        for _ in range(COLLECT_MAX_ROUNDS):
            n = len(batch)
            await asyncio.sleep(COLLECT_DELAY)
            if len(batch) == n:
                break
        del pending[uid]
        batch.sort(key=lambda m: m.message_id)
        await answer_batch(batch, bot)
    except Exception:
        log.exception("Сбой при обработке сообщения пользователя %s", uid)
        with contextlib.suppress(Exception):
            await message.answer("Внутренняя ошибка бота. Подробности — в логах сервера.")
    finally:
        pending.pop(uid, None)
        busy.discard(uid)


# ---------- запуск ----------

async def purge_loop() -> None:
    """Раз в сутки удаляет историю брошенных диалогов, чтобы база с файлами не росла бесконечно."""
    while True:
        try:
            n = store.purge_old_conversations(time.time() - conf.history_retention_days * 86400)
            if n:
                log.info("Удалено %d сообщений из старых диалогов", n)
        except Exception:
            log.exception("Не удалось почистить старые диалоги")
        await asyncio.sleep(PURGE_INTERVAL)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not conf.allowed_user_ids and not conf.admin_user_ids:
        log.warning("ALLOWED_USER_IDS и ADMIN_USER_IDS пусты: бот никому не ответит, кроме /id")
    if not conf.admin_user_ids:
        log.warning("ADMIN_USER_IDS пуст: команды /allow и /deny никому не доступны")
    bot = Bot(conf.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        await bot.set_my_commands([
            BotCommand(command="new", description="Новый диалог"),
            BotCommand(command="settings", description="Модель и effort"),
            BotCommand(command="usage", description="Расход за 30 дней"),
            BotCommand(command="id", description="Мой Telegram ID"),
        ])
    except TelegramNetworkError as e:
        # Меню команд — удобство, а не необходимость: без него бот работает. Если же Telegram недоступен
        # совсем, start_polling ниже завершится ошибкой, и Docker/systemd перезапустит бота.
        log.warning("Не удалось задать меню команд: %s", e)
    purge = asyncio.create_task(purge_loop()) if conf.history_retention_days else None
    try:
        await dp.start_polling(bot)
    finally:
        if purge:
            purge.cancel()


if __name__ == "__main__":
    asyncio.run(main())
