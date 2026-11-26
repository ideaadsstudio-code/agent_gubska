"""HTTP-слой и проверка, что настоящий SDK принимает наш запрос."""

from __future__ import annotations

import json

import anthropic
import httpx2 as httpx
from fastapi.testclient import TestClient

from app.agent import Agent
from app.main import create_app
from conftest import dumps, sign


def make_client(env) -> TestClient:
    return TestClient(create_app(env.settings, env.service, run_scheduler=False))


def test_webhook_verification(env):
    with make_client(env) as c:
        ok = c.get("/webhooks/instagram", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
        assert ok.status_code == 200 and ok.text == "42"
        bad = c.get("/webhooks/instagram", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"})
        assert bad.status_code == 403


def test_bad_signature_rejected(env):
    body = dumps(env.payload("Hi"))
    with make_client(env) as c:
        r = c.post("/webhooks/instagram", content=body, headers={"X-Hub-Signature-256": sign(body, "wrong")})
        assert r.status_code == 403
        r = c.post("/webhooks/instagram", content=body)
        assert r.status_code == 403
    assert env.claude.calls == []


def test_signed_webhook_end_to_end(env):
    from conftest import msg, text

    env.claude.queue(msg(text("We do fine line!")))
    body = dumps(env.payload("Do you do fine line?"))
    with make_client(env) as c:
        r = c.post("/webhooks/instagram", content=body, headers={"X-Hub-Signature-256": sign(body)})
        assert r.status_code == 200
    assert env.sent_texts()[0].endswith("We do fine line!")


def test_telegram_webhook_requires_secret(env):
    with make_client(env) as c:
        assert c.post("/webhooks/telegram", json={}).status_code == 403
        r = c.post("/webhooks/telegram", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"})
        assert r.status_code == 200


def test_privacy_page(env):
    with make_client(env) as c:
        r = c.get("/privacy")
        assert r.status_code == 200
        assert "Privacy Policy" in r.text and "90 days" in r.text and "train" in r.text


async def test_real_sdk_accepts_request(settings):
    """Запрос проходит через настоящий anthropic SDK до HTTP; проверяем тело и заголовки."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta", "")
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": "Hi!"}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 3, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        })

    client = anthropic.AsyncAnthropic(
        api_key="test", http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx.MockTransport(handler))
    )
    agent = Agent(settings, client, "SYSTEM")
    result = await agent.respond([], [{"type": "text", "text": "Hello"}], execute_tool=None)  # type: ignore[arg-type]
    assert result.reply_text == "Hi!"
    assert seen["body"]["fallbacks"] == "default"
    assert seen["body"]["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    assert "server-side-fallback-2026-07-01" in seen["beta"]
    assert "thinking-binding-controls-2026-08-01" in seen["beta"]
    assert result.new_messages[-1]["content"][0] == {"type": "thinking", "thinking": "", "signature": "s"}


async def test_had_messages_before_reads_only_dates():
    from app.instagram import InstagramClient

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/me/conversations"):
            return httpx.Response(200, json={"data": [{"id": "conv_1"}]})
        return httpx.Response(200, json={"messages": {"data": [
            {"id": "m2", "created_time": "2026-12-02T10:00:00+0000"},
            {"id": "m1", "created_time": "2025-05-01T10:00:00+0000"},
        ]}})

    ig = InstagramClient(lambda: "tok", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    launch = 1_795_000_000.0  # ноябрь 2026
    assert await ig.had_messages_before("1789", launch) is True
    assert requests[1].url.params["fields"] == "messages{created_time}"  # текст сообщений не запрашивается
    assert await ig.had_messages_before("1789", 1_700_000_000.0) is False


def test_playground_off_without_token(env):
    with make_client(env) as c:
        assert c.get("/playground").status_code == 404
        assert c.post("/playground/api/message", json={"token": "", "session": "s", "text": "hi"}).status_code == 403


def test_playground_chat(env, settings):
    from conftest import msg, text

    settings.playground_token = "pg-secret"
    env.claude.queue(msg(text("We do fine line!")))
    with make_client(env) as c:
        assert "Тестовый чат" in c.get("/playground").text
        bad = c.post("/playground/api/message", json={"token": "nope", "session": "s1", "text": "hi"})
        assert bad.status_code == 403
        r = c.post("/playground/api/message", json={"token": "pg-secret", "session": "s1", "text": "Fine line?"})
        data = r.json()
    assert data["replies"][0].endswith("We do fine line!")
    assert env.ig.sent == [] and env.tg.sent == []  # настоящие Instagram и Telegram не трогаются
    assert env.db.spend_since(0) > 0  # расход попадает в общий бюджет


def test_data_deletion_page(env):
    with make_client(env) as c:
        r = c.get("/data-deletion")
    assert r.status_code == 200 and "delete my data" in r.text


async def test_forget_command_deletes_everything(ienv):
    from conftest import image, msg, text

    ienv.claude.queue(msg(text("Nice!")))
    await ienv.client_says("Like this", attachments=image())
    ienv.db.update_conversation(ienv.conv().id, username="ink_lover")
    await ienv.service.handle_telegram({"message": {"chat": {"id": -100}, "text": "/forget @ink_lover"}})
    assert ienv.conv() is None
    assert ienv.db.one("SELECT COUNT(*) AS n FROM messages")["n"] == 0
    assert ienv.images.deleted == ["file_1"]


async def test_chatid_command_answers_any_chat(env):
    calls = []

    async def send_to(chat_id, text):
        calls.append((chat_id, text))

    env.tg.send_to = send_to
    await env.service.handle_telegram({"message": {"chat": {"id": -555}, "text": "/chatid"}})
    assert calls == [(-555, "ID этого чата: -555")]


async def test_register_webhooks(env, settings):
    calls = []

    async def set_webhook(url, secret):
        calls.append(("tg", url, secret))

    async def subscribe_app(fields="messages,messaging_referral"):
        calls.append(("ig", fields))

    settings.public_url = "https://bot.example"
    settings.telegram_bot_token = "t"
    settings.ig_access_token = "ig"
    env.tg.set_webhook = set_webhook
    env.ig.subscribe_app = subscribe_app
    await env.service.register_webhooks()
    assert ("tg", "https://bot.example/webhooks/telegram", "tg-secret") in calls
    assert ("ig", "messages,messaging_referral") in calls


async def test_refresh_error_does_not_leak_token():
    from app.instagram import InstagramClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "Invalid OAuth access token"}})

    ig = InstagramClient(lambda: "SECRET-TOKEN-123", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        await ig.refresh_token()
        raise AssertionError("expected error")
    except RuntimeError as e:
        assert "SECRET-TOKEN-123" not in str(e) and "Invalid OAuth" in str(e)
