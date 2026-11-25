"""Фото-референсы: модель видит картинку через Files API."""

from __future__ import annotations

import json

import httpx2 as httpx

from app.images import ImageStore
from app.service import IMAGE_REMOVED_PLACEHOLDER
from conftest import image, msg, text


def blocks_of_last_call(env) -> list[dict]:
    return env.claude.calls[-1]["messages"][-1]["content"]


async def test_image_reaches_model_as_file_block(ienv):
    ienv.claude.queue(msg(text("Nice fine line rose!")))
    await ienv.client_says("Like this one", attachments=image())
    blocks = blocks_of_last_call(ienv)
    assert blocks[0]["type"] == "text" and "Like this one" in blocks[0]["text"]
    assert blocks[1] == {"type": "image", "source": {"type": "file", "file_id": "file_1"}}
    stored = json.loads(ienv.db.one("SELECT attachments FROM messages WHERE author = 'client'")["attachments"])
    assert stored[0]["file_id"] == "file_1"


async def test_image_stays_identical_in_later_turns(ienv):
    ienv.claude.queue(msg(text("Nice!")), msg(text("Sure")))
    await ienv.client_says(attachments=image())
    await ienv.client_says("And on the forearm?")
    first, second = ienv.claude.calls
    assert second["messages"][: len(first["messages"])] == first["messages"]


async def test_images_and_text_batched_in_order(ienv, settings):
    settings.debounce_seconds = 0.05
    ienv.claude.queue(msg(text("Got both")))
    await ienv.service.handle_instagram(ienv.payload("Two refs:"))
    await ienv.service.handle_instagram(ienv.payload(attachments=image("https://cdn.example/a.jpg")))
    await ienv.service.handle_instagram(ienv.payload(attachments=image("https://cdn.example/b.jpg")))
    await ienv.service.drain()
    assert len(ienv.claude.calls) == 1
    kinds = [b["type"] for b in blocks_of_last_call(ienv)]
    assert kinds == ["text", "image", "text", "image"]


async def test_failed_download_falls_back_to_placeholder(ienv):
    ienv.images.fail_urls.add("https://cdn.example/gone.jpg")
    ienv.claude.queue(msg(text("Could you send it again?")))
    await ienv.client_says(attachments=image("https://cdn.example/gone.jpg"))
    blocks = blocks_of_last_call(ienv)
    assert [b["type"] for b in blocks] == ["text"]
    assert "[attachment: image]" in blocks[0]["text"]


async def test_images_off_without_store(env):
    env.claude.queue(msg(text("ok")))
    await env.client_says(attachments=image())
    assert [b["type"] for b in blocks_of_last_call(env)] == ["text"]


async def test_deleted_image_removed_everywhere(ienv):
    ienv.claude.queue(msg(text("Nice!")))
    await ienv.client_says(attachments=image(), mid="img_mid")
    await ienv.client_says(mid="img_mid", deleted=True)
    assert ienv.images.deleted == ["file_1"]
    history = json.dumps(ienv.conv().llm_history)
    assert "file_1" not in history and IMAGE_REMOVED_PLACEHOLDER in history


async def test_purge_deletes_files_and_redacts_active_conversation(ienv):
    ienv.claude.queue(msg(text("Nice!")), msg(text("Still here")))
    await ienv.client_says(attachments=image())
    ienv.clock.advance(hours=24 * 89)
    await ienv.client_says("Hi again")  # диалог активен, но картинке уже 89 дней
    ienv.clock.advance(hours=24 * 2)
    await ienv.service.purge()
    assert ienv.images.deleted == ["file_1"]
    assert ienv.conv() is not None
    assert "file_1" not in json.dumps(ienv.conv().llm_history)


async def test_booking_card_still_has_reference_links(ienv):
    booking = dict.fromkeys(["idea", "size", "placement", "style", "references", "preferred_dates", "client_name", "notes"], "x")
    from conftest import tool
    ienv.claude.queue(msg(tool("create_booking_request", booking), stop="tool_use"), msg(text("Done")))
    await ienv.client_says("Book this", attachments=image("https://cdn.example/ref9.jpg"))
    assert "https://cdn.example/ref9.jpg" in ienv.tg.sent[-1]["text"]


class FakeFiles:
    def __init__(self):
        self.calls: list[dict] = []

    async def upload(self, **kw):
        self.calls.append(kw)
        return type("Meta", (), {"id": "file_real"})()

    async def delete(self, file_id):
        pass


class FakeAnthropic:
    def __init__(self):
        self.files = FakeFiles()


async def test_image_store_download_and_upload():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".jpg"):
            return httpx.Response(200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})
        return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})

    client = FakeAnthropic()
    store = ImageStore(client, retention_days=90, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await store.ingest("https://cdn.example/x.jpg", "ref") == "file_real"
    call = client.files.calls[0]
    assert call["file"] == ("ref", b"\xff\xd8jpeg", "image/jpeg")
    assert call["expires_in_seconds"] == 90 * 86400
    assert await store.ingest("https://cdn.example/page", "ref") is None
