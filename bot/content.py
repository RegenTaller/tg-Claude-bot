"""Превращает сообщения Telegram (текст, фото, PDF, текстовые файлы) в блоки содержимого для Claude API."""

import asyncio
import base64
import io

from aiogram import Bot
from aiogram.types import Document, Message
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # лимит Bot API на скачивание файлов ботом
DOWNLOAD_TIMEOUT = 120  # секунд на скачивание одного файла из Telegram
# Текстовый файл целиком попадает в историю и пересылается с каждым следующим сообщением,
# поэтому большие логи быстро становятся дорогими. 300 КБ — это примерно 75–150 тыс. токенов.
MAX_TEXT_FILE_BYTES = 300 * 1024
# Если в запросе больше 20 картинок (считая картинки из прошлых ходов), API требует, чтобы каждая
# была не больше 2000 px по любой стороне. История не правится задним числом, поэтому уменьшаем сразу.
MAX_IMAGE_SIDE = 2000
# API принимает картинку до 10 МБ в base64; держим запас, чтобы весь запрос оставался под 32 МБ.
MAX_IMAGE_B64_BYTES = 5 * 1024 * 1024

TEXT_EXTENSIONS = (".txt", ".md", ".py", ".c", ".cpp", ".h", ".hpp", ".json", ".csv", ".log",
                   ".yaml", ".yml", ".toml", ".ini", ".xml", ".html", ".js", ".ts", ".sh", ".rs", ".go")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
# Форматы, которые API принимает как есть (определяются по содержимому файла, а не по имени).
_API_IMAGE_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp"}


class UserError(Exception):
    """Ошибка, текст которой можно показать пользователю."""


def _b64_len(n: int) -> int:
    return 4 * ((n + 2) // 3)


def _flatten(img: Image.Image) -> Image.Image:
    """Приводит картинку к RGB для JPEG; прозрачные области заливает белым."""
    if img.mode in ("RGB", "L"):
        return img
    if img.mode in ("RGBA", "LA", "P", "PA"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, "white")
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    return img.convert("RGB")


def prepare_image(data: bytes) -> tuple[str, bytes]:
    """Возвращает (media_type, байты) картинки, уложенной в лимиты API.

    Подходящую картинку отдаёт как есть; слишком большую или в неподдерживаемом формате
    уменьшает до MAX_IMAGE_SIDE и перекодирует в JPEG."""
    try:
        img = Image.open(io.BytesIO(data))
        fmt, (w, h) = img.format, img.size
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError):
        raise UserError("Не удалось прочитать изображение. Подходят JPEG, PNG, GIF и WebP; "
                        "фото с iPhone (HEIC) пришлите как обычное фото, а не файлом.")

    media_type = _API_IMAGE_FORMATS.get(fmt)
    if media_type and max(w, h) <= MAX_IMAGE_SIDE and _b64_len(len(data)) <= MAX_IMAGE_B64_BYTES:
        return media_type, data

    try:
        img = ImageOps.exif_transpose(img)  # телефоны хранят поворот в EXIF; при перекодировании он потеряется
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        img = _flatten(img)
        for quality in (90, 80, 65, 50):
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality)
            if _b64_len(buf.tell()) <= MAX_IMAGE_B64_BYTES:
                return "image/jpeg", buf.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise UserError("Не удалось обработать изображение. Попробуйте прислать его как обычное фото.")
    raise UserError("Изображение слишком большое даже после сжатия.")


def _image_block(media_type: str, data: bytes) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode()},
    }


async def _download(bot: Bot, file_id: str) -> bytes:
    return (await bot.download(file_id, timeout=DOWNLOAD_TIMEOUT)).read()


async def _image(bot: Bot, file_id: str) -> dict:
    data = await _download(bot, file_id)
    media_type, data = await asyncio.to_thread(prepare_image, data)  # Pillow не должен блокировать бота
    return _image_block(media_type, data)


async def _document(bot: Bot, doc: Document) -> dict:
    if doc.file_size and doc.file_size > MAX_DOWNLOAD_BYTES:
        raise UserError("Файл больше 20 МБ — Telegram не даёт ботам скачивать такие.")
    name = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    is_pdf = mime == "application/pdf" or name.endswith(".pdf")
    is_image = mime.startswith("image/") or name.endswith(IMAGE_EXTENSIONS)
    is_text = mime.startswith("text/") or name.endswith(TEXT_EXTENSIONS)
    too_big_text = (f"Текстовый файл больше {MAX_TEXT_FILE_BYTES // 1024} КБ. Он пересылался бы в Claude "
                    "с каждым следующим сообщением и дорого стоил бы. Пришлите нужный фрагмент.")
    if not (is_pdf or is_image or is_text):
        raise UserError("Этот тип файла пока не поддерживается. Подходят фото, PDF и текстовые файлы.")
    if is_text and not (is_pdf or is_image) and doc.file_size and doc.file_size > MAX_TEXT_FILE_BYTES:
        raise UserError(too_big_text)

    if is_image:
        return await _image(bot, doc.file_id)
    data = await _download(bot, doc.file_id)
    if is_pdf:
        if b"%PDF-" not in data[:1024]:
            raise UserError("Файл назван как PDF, но не похож на PDF.")
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(data).decode()},
            "title": doc.file_name or "document.pdf",
        }
    if len(data) > MAX_TEXT_FILE_BYTES:
        raise UserError(too_big_text)
    try:
        body = data.decode("utf-8")
    except UnicodeDecodeError:
        body = data.decode("cp1251", errors="replace")
    return {"type": "text", "text": f"Файл {doc.file_name}:\n\n{body}"}


async def message_blocks(message: Message, bot: Bot) -> list[dict]:
    """Блоки содержимого одного сообщения Telegram. UserError — что-то не так с присланным."""
    blocks: list[dict] = []
    if message.photo:
        blocks.append(await _image(bot, message.photo[-1].file_id))  # самое большое разрешение
    elif message.document:
        blocks.append(await _document(bot, message.document))
    elif message.text:
        blocks.append({"type": "text", "text": message.text})
    else:
        raise UserError("Пока понимаю только текст, фото, PDF и текстовые файлы.")
    if message.caption:
        blocks.append({"type": "text", "text": message.caption})
    return blocks


async def batch_content(messages: list[Message], bot: Bot) -> list[dict]:
    """Содержимое одного хода из нескольких сообщений (альбом, длинный текст, разрезанный Telegram).

    Файлы и картинки идут первыми, затем текст: так рекомендует документация API."""
    blocks: list[dict] = []
    for m in messages:
        blocks += await message_blocks(m, bot)
    return sorted(blocks, key=lambda b: b["type"] == "text")  # sorted устойчив: порядок внутри групп сохранится
