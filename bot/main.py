"""Telegram-бот: пересылает сообщения в Claude и присылает ответы обратно."""

import asyncio
import base64
import html
import logging
import time

import anthropic
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
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
from .claude import ClaudeClient
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
MAX_FILE_BYTES = 20 * 1024 * 1024  # лимит Bot API на скачивание файлов
TEXT_EXTENSIONS = (".txt", ".md", ".py", ".c", ".cpp", ".h", ".hpp", ".json", ".csv", ".log",
                   ".yaml", ".yml", ".toml", ".ini", ".xml", ".html", ".js", ".ts", ".sh", ".rs", ".go")

conf = cfg.load()
store = Storage(conf.db_path)
claude = ClaudeClient(conf.anthropic_api_key, conf.max_tokens)
dp = Dispatcher()
busy: set[int] = set()  # пользователи, у которых сейчас идёт запрос


# ---------- доступ ----------

def allowed(user_id: int) -> bool:
    return user_id in conf.allowed_user_ids or user_id in conf.admin_user_ids or store.is_allowed_extra(user_id)


def is_admin(user_id: int) -> bool:
    return user_id in conf.admin_user_ids


async def deny_message(message: Message) -> None:
    await message.answer(
        "Это приватный бот. Доступа у вас нет.\n"
        f"Ваш Telegram ID: <code>{message.from_user.id}</code> — передайте его владельцу бота."
    )


# ---------- настройки ----------

def settings_text(user_id: int) -> str:
    s = store.get_settings(user_id, conf.default_model, conf.default_effort)
    return (
        f"Модель: <b>{cfg.MODELS[s['model']]}</b>\n"
        f"Effort: <b>{s['effort']}</b>\n\n"
        "Effort — сколько модель думает перед ответом: low быстрее и дешевле, max — глубже и дороже."
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
        "Можно присылать фото, PDF и текстовые файлы.\n\n"
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
    rows = store.usage_since(time.time() - 30 * 86400, None if is_admin(uid) else uid)
    if not rows:
        return await message.answer("За 30 дней запросов не было.")
    lines, total = [], 0.0
    for r in rows:
        pin, pout = cfg.PRICES.get(r["model"], (0, 0))
        # Оценка: запись в кэш ×1.25, чтение из кэша ×0.1 от цены входа. Точные цифры — в Anthropic Console.
        cost = (r["i"] * pin + r["cw"] * pin * 1.25 + r["cr"] * pin * 0.1 + r["o"] * pout) / 1e6
        total += cost
        who = f"{r['user_id']} · " if is_admin(uid) else ""
        lines.append(f"{who}{cfg.MODELS.get(r['model'], r['model'])}: {r['n']} запр., ≈${cost:.2f}")
    lines.append(f"\nИтого ≈ <b>${total:.2f}</b> (оценка; точный счёт — в Anthropic Console)")
    await message.answer("\n".join(lines))


@dp.message(Command("allow"))
async def cmd_allow(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id):
        return
    if not command.args or not command.args.strip().lstrip("-").isdigit():
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
    store.get_settings(uid, conf.default_model, conf.default_effort)
    if kind == "model" and value in cfg.MODELS:
        store.set_model(uid, value)
    elif kind == "effort" and value in cfg.EFFORTS:
        store.set_effort(uid, value)
    try:
        await cb.message.edit_text(settings_text(uid), reply_markup=settings_kb(uid))
    except TelegramBadRequest:
        pass  # ничего не изменилось
    await cb.answer("Сохранено")


# ---------- основной обработчик ----------

async def build_user_content(message: Message, bot: Bot) -> list[dict] | str:
    """Собирает содержимое сообщения для API. Строка — текст ошибки для пользователя."""
    content: list[dict] = []
    caption = message.caption or ""

    if message.photo:
        photo = message.photo[-1]  # самое большое разрешение
        data = await bot.download(photo.file_id)
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": base64.b64encode(data.read()).decode()},
        })
    elif message.document:
        doc = message.document
        if doc.file_size and doc.file_size > MAX_FILE_BYTES:
            return "Файл больше 20 МБ — Telegram не даёт ботам скачивать такие."
        name = (doc.file_name or "").lower()
        mime = doc.mime_type or ""
        data = (await bot.download(doc.file_id)).read()
        if mime == "application/pdf" or name.endswith(".pdf"):
            content.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf",
                           "data": base64.b64encode(data).decode()},
                "title": doc.file_name or "document.pdf",
            })
        elif mime in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode()},
            })
        elif mime.startswith("text/") or name.endswith(TEXT_EXTENSIONS):
            try:
                body = data.decode("utf-8")
            except UnicodeDecodeError:
                body = data.decode("cp1251", errors="replace")
            content.append({"type": "text", "text": f"Файл {doc.file_name}:\n\n{body}"})
        else:
            return "Этот тип файла пока не поддерживается. Подходят фото, PDF и текстовые файлы."
    elif message.text:
        content.append({"type": "text", "text": message.text})
    else:
        return "Пока понимаю только текст, фото, PDF и текстовые файлы."

    if caption:
        content.append({"type": "text", "text": caption})
    return content


def render(text: str) -> list[str]:
    """Markdown от Claude → HTML для Telegram, порезанный на куски по 4096 символов."""
    return split_html_for_telegram(telegram_format(text), trim_empty_leading_lines=True)


async def safe_edit(msg: Message, html_text: str) -> None:
    try:
        await msg.edit_text(html_text)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return
        # Если форматирование сломалось на середине генерации — показываем как обычный текст.
        try:
            await msg.edit_text(html.escape(html_text), parse_mode=None)
        except TelegramBadRequest:
            pass


async def send_final(placeholder: Message, text: str) -> None:
    """Заменяет «печатает…» на итоговый ответ; длинный ответ отправляется несколькими сообщениями."""
    try:
        chunks = render(text) or ["(пустой ответ)"]
        await placeholder.edit_text(chunks[0])
        for chunk in chunks[1:]:
            await placeholder.answer(chunk)
    except TelegramBadRequest:
        log.warning("HTML не принят Telegram, отправляю обычным текстом")
        plain = [text[i:i + 4000] for i in range(0, len(text), 4000)] or ["(пустой ответ)"]
        await placeholder.edit_text(plain[0], parse_mode=None)
        for chunk in plain[1:]:
            await placeholder.answer(chunk, parse_mode=None)


@dp.message()
async def on_message(message: Message, bot: Bot) -> None:
    uid = message.from_user.id
    if not allowed(uid):
        return await deny_message(message)
    if uid in busy:
        return await message.answer("Подождите, предыдущий ответ ещё генерируется.")

    settings = store.get_settings(uid, conf.default_model, conf.default_effort)
    conv_id = settings["conversation_id"] or store.new_conversation(uid, conf.system_prompt)
    conv = store.conversation(conv_id)

    busy.add(uid)
    try:
        user_content = await build_user_content(message, bot)
        if isinstance(user_content, str):
            return await message.answer(user_content)

        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        placeholder = await message.answer("⏳ <i>Claude думает…</i>")
        last_edit = 0.0

        async def on_text(partial: str) -> None:
            nonlocal last_edit
            now = time.monotonic()
            if now - last_edit < EDIT_INTERVAL:
                return
            last_edit = now
            tail = partial[-STREAM_PREVIEW_LIMIT:]
            await safe_edit(placeholder, html.escape(tail) + " ▍")

        history = store.history(conv_id)
        messages = history + [{"role": "user", "content": user_content}]
        try:
            reply = await claude.ask(
                model=settings["model"],
                effort=settings["effort"],
                system=conv["system_prompt"],
                messages=messages,
                on_text=on_text,
            )
        except anthropic.RateLimitError:
            return await placeholder.edit_text("Превышен лимит запросов API. Попробуйте через минуту.")
        except anthropic.BadRequestError as e:
            log.exception("bad request")
            msg = str(getattr(e, "message", e))
            hint = ""
            if "credit balance" in msg.lower():
                hint = "\nПохоже, на балансе Anthropic Console закончились деньги."
            elif "too long" in msg.lower() or "context" in msg.lower():
                hint = "\nДиалог стал слишком длинным — начните новый: /new"
            return await placeholder.edit_text(f"Ошибка запроса к Claude.{hint}\n<code>{html.escape(msg[:500])}</code>")
        except anthropic.AuthenticationError:
            return await placeholder.edit_text("API-ключ Anthropic недействителен. Проверьте ANTHROPIC_API_KEY.")
        except anthropic.APIStatusError as e:
            log.exception("api status error")
            return await placeholder.edit_text(f"Claude API вернул ошибку {e.status_code}. Попробуйте ещё раз.")
        except anthropic.APIConnectionError:
            return await placeholder.edit_text("Нет связи с Claude API. Попробуйте ещё раз.")

        store.add_usage(uid, reply.served_by, reply.usage)

        if reply.stop_reason == "refusal":
            cat = f" (категория: {reply.refusal_category})" if reply.refusal_category else ""
            return await placeholder.edit_text(
                f"Claude отказался отвечать на этот запрос{cat}. Попробуйте переформулировать."
            )

        if reply.content:
            store.append(conv_id, "user", user_content)
            store.append(conv_id, "assistant", reply.content)

        text = reply.text
        if reply.stop_reason == "max_tokens":
            text += "\n\n_(ответ обрезан по лимиту длины)_"
        if reply.served_by != settings["model"]:
            text += f"\n\n_(ответила резервная модель {reply.served_by})_"
        await send_final(placeholder, text)
    finally:
        busy.discard(uid)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not conf.allowed_user_ids and not conf.admin_user_ids:
        log.warning("ALLOWED_USER_IDS пуст: бот никому не ответит, кроме /id")
    bot = Bot(conf.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands([
        BotCommand(command="new", description="Новый диалог"),
        BotCommand(command="settings", description="Модель и effort"),
        BotCommand(command="usage", description="Расход за 30 дней"),
        BotCommand(command="id", description="Мой Telegram ID"),
    ])
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
