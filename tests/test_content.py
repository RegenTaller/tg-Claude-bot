import base64
import io
import os

import pytest
from fakes import jpeg_bytes, make_bot, update
from PIL import Image

from bot import content
from bot.content import MAX_IMAGE_B64_BYTES, MAX_IMAGE_SIDE, UserError, batch_content, prepare_image


def png_bytes(w: int, h: int, mode: str = "RGB") -> bytes:
    buf = io.BytesIO()
    color = (0, 0, 255, 0) if mode == "RGBA" else (0, 0, 255)
    Image.new(mode, (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def test_small_supported_image_passes_unchanged():
    data = jpeg_bytes()
    assert prepare_image(data) == ("image/jpeg", data)


def test_media_type_comes_from_content_not_from_name():
    data = png_bytes(10, 10)
    assert prepare_image(data)[0] == "image/png"


def test_large_image_is_downscaled_to_2000px():
    """Картинка больше 2000 px сломала бы диалоги, где больше 20 картинок (лимит API для many-image)."""
    media_type, data = prepare_image(png_bytes(3000, 1500))
    img = Image.open(io.BytesIO(data))
    assert media_type == "image/jpeg"
    assert max(img.size) == MAX_IMAGE_SIDE


def test_heavy_image_is_recompressed_under_limit():
    """api-4: картинка, которая в base64 больше лимита, ужимается, а не уходит в API с ошибкой 400."""
    noise = Image.frombytes("RGB", (1900, 1900), os.urandom(1900 * 1900 * 3))
    buf = io.BytesIO()
    noise.save(buf, "PNG")
    assert len(base64.b64encode(buf.getvalue())) > MAX_IMAGE_B64_BYTES
    media_type, data = prepare_image(buf.getvalue())
    assert media_type == "image/jpeg"
    assert len(base64.b64encode(data)) <= MAX_IMAGE_B64_BYTES


def test_transparent_png_gets_white_background():
    _, data = prepare_image(png_bytes(2500, 100, "RGBA"))
    assert Image.open(io.BytesIO(data)).getpixel((10, 10)) == pytest.approx((255, 255, 255), abs=3)


def test_garbage_is_rejected():
    with pytest.raises(UserError):
        prepare_image(b"not an image at all")


async def _blocks(*updates, files):
    bot, _ = make_bot(files)
    return await batch_content([u.message.as_(bot) for u in updates], bot)


async def test_pdf_is_checked_by_signature():
    doc = {"file_id": "f1", "file_name": "lecture.pdf", "mime_type": "application/pdf", "file_size": 20}
    with pytest.raises(UserError, match="не похож на PDF"):
        await _blocks(update(1, document=doc), files={"f1": b"MZ fake exe"})
    blocks = await _blocks(update(1, document=doc), files={"f1": b"%PDF-1.7 ..."})
    assert blocks[0]["type"] == "document" and blocks[0]["title"] == "lecture.pdf"


async def test_big_text_file_is_refused_before_download():
    doc = {"file_id": "f2", "file_name": "server.log", "mime_type": "text/plain",
           "file_size": content.MAX_TEXT_FILE_BYTES + 1}
    with pytest.raises(UserError, match="КБ"):
        await _blocks(update(1, document=doc), files={})  # файла нет — значит, скачивать и не пытались


async def test_text_file_in_cp1251_is_decoded():
    doc = {"file_id": "f3", "file_name": "notes.txt", "mime_type": "text/plain", "file_size": 6}
    blocks = await _blocks(update(1, document=doc), files={"f3": "Привет".encode("cp1251")})
    assert blocks == [{"type": "text", "text": "Файл notes.txt:\n\nПривет"}]


async def test_image_sent_as_file_is_processed():
    doc = {"file_id": "f4", "file_name": "shot.PNG", "mime_type": "image/png", "file_size": 100}
    blocks = await _blocks(update(1, document=doc), files={"f4": png_bytes(3000, 3000)})
    assert blocks[0]["source"]["media_type"] == "image/jpeg"


async def test_unsupported_file_type():
    doc = {"file_id": "f5", "file_name": "song.mp3", "mime_type": "audio/mpeg", "file_size": 100}
    with pytest.raises(UserError, match="не поддерживается"):
        await _blocks(update(1, document=doc), files={})


async def test_album_puts_media_first_and_keeps_text_order():
    blocks = await _blocks(
        update(1, photo="p1", caption="Что общего?", media_group_id="g"),
        update(2, photo="p2", media_group_id="g"),
        files={"p1": jpeg_bytes(), "p2": jpeg_bytes()},
    )
    assert [b["type"] for b in blocks] == ["image", "image", "text"]
    assert blocks[2]["text"] == "Что общего?"
