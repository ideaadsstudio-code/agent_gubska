"""Подключение Instagram мастера по ссылке."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import httpx2 as httpx
from fastapi.testclient import TestClient

from app.instagram import InstagramClient
from app.main import create_app


def setup(env, settings, fail_exchange: bool = False):
    settings.playground_token = settings.admin_token = "admin123"
    settings.ig_api_mode = "instagram"
    settings.ig_app_id = "app-id"
    settings.public_url = "https://bot.example"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/oauth/access_token":
            if fail_exchange:
                return httpx.Response(400, json={"error_message": "Invalid code"})
            return httpx.Response(200, json={"data": [{"access_token": "short", "user_id": "1"}]})
        if path == "/access_token":
            return httpx.Response(200, json={"access_token": "long-token", "expires_in": 5_184_000})
        if path.endswith("/me"):
            return httpx.Response(200, json={"username": "tattoo.by.gubska"})
        if path.endswith("/subscribed_apps"):
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404)

    env.service.ig = InstagramClient(lambda: env.db.kv_get("ig_token") or "",
                                     http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return seen


def get_link(c) -> str:
    page = c.get("/auth/instagram/start", params={"token": "admin123"}).text
    start = page.index("https://www.instagram.com/oauth/authorize?")
    return page[start: page.index("</textarea>")].replace("&amp;", "&")


def test_start_requires_admin_token(env, settings):
    setup(env, settings)
    with TestClient(create_app(settings, env.service, run_scheduler=False)) as c:
        assert c.get("/auth/instagram/start").status_code == 403
        assert c.get("/auth/instagram/start", params={"token": "nope"}).status_code == 403


def test_link_and_callback_store_token(env, settings):
    seen = setup(env, settings)
    with TestClient(create_app(settings, env.service, run_scheduler=False)) as c:
        q = parse_qs(urlparse(get_link(c)).query)
        assert q["client_id"] == ["app-id"]
        assert q["redirect_uri"] == ["https://bot.example/auth/instagram/callback"]
        assert "instagram_business_manage_messages" in q["scope"][0]
        r = c.get("/auth/instagram/callback", params={"code": "abc#_", "state": q["state"][0]})
        assert "Готово" in r.text
        # Ссылка одноразовая.
        again = c.get("/auth/instagram/callback", params={"code": "abc", "state": q["state"][0]})
        assert "устарела" in again.text
    assert env.db.kv_get("ig_token") == "long-token"
    assert float(env.db.kv_get("ig_token_expires_at")) > time.time() + 50 * 86400
    assert b"code=abc&" in seen[0].content or seen[0].content.endswith(b"code=abc")  # «#_» отрезан
    assert any(r.url.path.endswith("/subscribed_apps") for r in seen)
    assert any("Instagram подключён: @tattoo.by.gubska" in t for t in env.tg.texts())


def test_callback_rejects_unknown_state(env, settings):
    setup(env, settings)
    with TestClient(create_app(settings, env.service, run_scheduler=False)) as c:
        r = c.get("/auth/instagram/callback", params={"code": "abc", "state": "forged"})
    assert "устарела" in r.text and env.db.kv_get("ig_token") is None


def test_callback_denied(env, settings):
    setup(env, settings)
    with TestClient(create_app(settings, env.service, run_scheduler=False)) as c:
        state = parse_qs(urlparse(get_link(c)).query)["state"][0]
        r = c.get("/auth/instagram/callback", params={"error": "access_denied", "state": state})
    assert "Доступ не выдан" in r.text
    assert any("не дал доступ" in t for t in env.tg.texts())


def test_exchange_error_reported_without_secrets(env, settings):
    settings.ig_app_secret = "SUPER-SECRET"
    setup(env, settings, fail_exchange=True)
    with TestClient(create_app(settings, env.service, run_scheduler=False)) as c:
        state = parse_qs(urlparse(get_link(c)).query)["state"][0]
        r = c.get("/auth/instagram/callback", params={"code": "bad", "state": state})
    assert "Не получилось" in r.text
    alert = next(t for t in env.tg.texts() if "Не удалось подключить" in t)
    assert "Invalid code" in alert and "SUPER-SECRET" not in alert


def test_start_lists_missing_settings(env, settings):
    setup(env, settings)
    settings.ig_app_id = ""
    with TestClient(create_app(settings, env.service, run_scheduler=False)) as c:
        r = c.get("/auth/instagram/start", params={"token": "admin123"})
    assert "IG_APP_ID" in r.text
