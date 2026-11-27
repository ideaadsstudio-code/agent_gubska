"""Анализ переписки на сервере: загрузка диалогов через API, черновики, защита и лимит расходов."""

from __future__ import annotations

import json

import httpx2 as httpx
from fastapi.testclient import TestClient

from app import history_analysis
from app.admin_analysis import STATE_KEY, threads_from_api
from app.classifier import TopicClassifier
from app.instagram import InstagramClient
from app.main import create_app
from conftest import FakeClaude, msg, text, topic

OWNER_ID = "17841462392047857"


def graph_handler(seen: list):
    full = "messages.limit(20){id,created_time,from,message}"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        fields = request.url.params.get("fields", "")
        if path.endswith("/me") and "instagram_business_account" in fields:
            return httpx.Response(200, json={"instagram_business_account": {"id": OWNER_ID, "username": "tattoo.by.gubska"}})
        if path.endswith("/me/conversations"):
            if request.url.params.get("after") == "p2":
                return httpx.Response(200, json={"data": [{"id": "c3"}]})
            return httpx.Response(200, json={"data": [{"id": "c1"}, {"id": "c2"}],
                                             "paging": {"next": "https://graph.facebook.com/v24.0/me/conversations?after=p2"}})
        if path.endswith("/c1") and fields == full:
            return httpx.Response(200, json={"messages": {"data": [
                {"id": "m12", "created_time": "2026-09-01T10:05:00+0000", "from": {"id": OWNER_ID, "username": "tattoo.by.gubska"},
                 "message": "Привет, Катя! Fine line 10 см от 3000 грн"},
                {"id": "m11", "created_time": "2026-09-01T10:00:00+0000", "from": {"id": "901", "username": "kate.ink"},
                 "message": "Привет! Сколько стоит тату 10 см? Мой номер +380 67 123 45 67"},
            ]}})
        if path.endswith("/c2") and fields == full:
            # Meta не успевает: HTTP 400 Timeout, code -2.
            return httpx.Response(400, json={"error": {"message": "Timeout", "code": -2}})
        if path.endswith("/c2"):
            return httpx.Response(200, json={"messages": {"data": [{"id": "m21"}]}})
        if path.endswith("/m21"):
            return httpx.Response(200, json={"id": "m21", "created_time": "2026-09-02T10:00:00+0000",
                                             "from": {"id": "902", "username": "bestie"}, "message": "Ань, как мама?"})
        if path.endswith("/c3"):
            return httpx.Response(200, json={"messages": {"data": [
                {"id": "m31", "created_time": "2026-10-01T10:00:00+0000", "from": {"id": "903", "username": "brand"},
                 "message": "Collab offer for your page"},
            ]}})
        return httpx.Response(404, json={"error": {"message": "not found"}})
    return handler


def make_ig(handler) -> InstagramClient:
    ig = InstagramClient(lambda: "PAGE", mode="facebook", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    ig.retry_delay = 0
    return ig


async def test_fetch_history_pages_and_fills_missing_fields():
    seen: list = []
    ig = make_ig(graph_handler(seen))
    owner = await ig.own_account()
    assert owner == (OWNER_ID, "tattoo.by.gubska")
    convs = await ig.fetch_history(max_conversations=10)
    assert len(convs) == 3
    assert convs[1]["messages"][0]["text"] == "Ань, как мама?"
    assert all("access_token" not in str(r.url) for r in seen)  # токен только в заголовке

    threads = threads_from_api(convs, OWNER_ID, "tattoo.by.gubska")
    kate = threads[0]
    assert kate.username_guess == "kate.ink"
    assert [m.sender for m in kate.messages] == ["kate.ink", "tattoo.by.gubska"]  # по времени


def make_app(env, settings, monkeypatch, haiku_topics: list[str]):
    settings.playground_token = settings.admin_token = "admin123"
    seen: list = []
    env.service.ig = make_ig(graph_handler(seen))
    haiku = FakeClaude()
    haiku.queue(*[topic(t) for t in haiku_topics])
    env.service.classifier = TopicClassifier(haiku)
    sent: list[dict] = []

    async def fake_call(client, **params):
        sent.append(params)
        if "format" in params.get("output_config", {}):
            return msg(text(json.dumps({"knowledge_base": "# KB: fine line от 3000 грн",
                                        "style_guide": "# Style", "test_cases": "# Tests"})))
        return msg(text("Цены: fine line 10 см от 3000 грн"))

    monkeypatch.setattr(history_analysis, "call_model", fake_call)
    return create_app(settings, env.service, run_scheduler=False), sent


def test_analysis_requires_admin_token(env, settings, monkeypatch):
    app, _ = make_app(env, settings, monkeypatch, [])
    with TestClient(app) as c:
        assert c.get("/admin/analysis").status_code == 403
        assert c.post("/admin/analysis/start", data={"token": "nope"}).status_code == 403


def test_analysis_end_to_end(env, settings, monkeypatch):
    app, sent = make_app(env, settings, monkeypatch, ["tattoo", "not_tattoo", "not_tattoo"])
    with TestClient(app) as c:
        r = c.post("/admin/analysis/start", data={"token": "admin123", "max_conversations": "50", "max_usd": "15"},
                   follow_redirects=False)
        assert r.status_code == 303
    # Выход из TestClient дожидается фоновой задачи.
    state = json.loads(env.db.kv_get(STATE_KEY))
    assert state["status"] == "done" and state["tattoo"] == 1 and state["personal"] == 2
    assert state["knowledge_base"].startswith("# KB")
    assert set(state["personal_contacts"]) == {"bestie", "brand"}
    # В модель ушла только обезличенная тату-переписка.
    batch = sent[0]["messages"][0]["content"]
    assert "3000 грн" in batch and "Катя" not in batch and "+380" not in batch and "мама" not in batch
    # Сырая переписка нигде не сохранена.
    stored = json.dumps([dict(r) for r in env.db.all("SELECT * FROM kv")], ensure_ascii=False)
    assert "+380" not in stored and "мама" not in stored
    assert any("Анализ переписки @tattoo.by.gubska готов" in t for t in env.tg.texts())
    with TestClient(app) as c:
        page = c.get("/admin/analysis", params={"token": "admin123"}).text
    assert "Черновик базы знаний" in page and "fine line от 3000" in page and "@bestie" in page


def test_analysis_stops_when_too_expensive(env, settings, monkeypatch):
    import app.admin_analysis as aa

    app, sent = make_app(env, settings, monkeypatch, [])
    monkeypatch.setattr(aa, "estimate", lambda threads, owner, model=None: (10_000_000, 42.0))
    with TestClient(app) as c:
        c.post("/admin/analysis/start", data={"token": "admin123", "max_usd": "15"})
    state = json.loads(env.db.kv_get(STATE_KEY))
    assert state["status"] == "too_expensive" and state["estimate_usd"] == 42.0
    assert sent == [] and env.db.spend_since(0) == 0  # до модели дело не дошло


def test_analysis_error_reported(env, settings, monkeypatch):
    app, _ = make_app(env, settings, monkeypatch, [])

    async def boom(*a, **k):
        raise RuntimeError("К странице не привязан Instagram")

    env.service.ig.own_account = boom
    with TestClient(app) as c:
        c.post("/admin/analysis/start", data={"token": "admin123"})
    state = json.loads(env.db.kv_get(STATE_KEY))
    assert state["status"] == "error" and "не привязан" in state["error"]
    assert any("Анализ переписки не удался" in t for t in env.tg.texts())


def test_analysis_uses_sonnet_by_default(env, settings, monkeypatch):
    app, sent = make_app(env, settings, monkeypatch, ["tattoo", "not_tattoo", "not_tattoo"])
    with TestClient(app) as c:
        c.post("/admin/analysis/start", data={"token": "admin123"})
    assert sent and all(p["model"] == "claude-sonnet-5-5" for p in sent)
    assert sent[0]["fallbacks"] == "default"


def test_estimate_per_100_dialogs():
    from app.history_analysis import Message, Thread, estimate

    # 100 типичных диалогов: 20 сообщений по ~80 символов.
    threads = [Thread(f"u{i}", ["artist", f"u{i}"], [
        Message("artist" if j % 2 else f"u{i}", "Сообщение про тату, размер и цену, примерно такой длины текст." , j)
        for j in range(20)]) for i in range(100)]
    _, sonnet = estimate(threads, "artist", "claude-sonnet-5-5")
    _, opus = estimate(threads, "artist", "claude-opus-5-5")
    assert sonnet < opus and sonnet < 1.0


async def test_fetch_history_retries_timeouts():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"data": []})

    ig = make_ig(handler)
    assert await ig.fetch_history(10) == []
    assert calls["n"] == 2


def test_error_without_message_shows_type(env, settings, monkeypatch):
    app, _ = make_app(env, settings, monkeypatch, [])

    async def boom(*a, **k):
        raise TimeoutError()

    env.service.ig.own_account = boom
    with TestClient(app) as c:
        c.post("/admin/analysis/start", data={"token": "admin123"})
    state = json.loads(env.db.kv_get(STATE_KEY))
    assert state["error"] == "TimeoutError"


async def test_list_shrinks_page_when_meta_asks_for_less():
    seen_limits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/me/conversations"):
            limit = request.url.params.get("limit")
            seen_limits.append(limit)
            if int(limit) > 5:
                return httpx.Response(500, json={"error": {
                    "message": "Please reduce the amount of data you're asking for, then retry your request", "code": 1}})
            if request.url.params.get("after") == "p2":
                return httpx.Response(200, json={"data": [{"id": "c2"}]})
            return httpx.Response(200, json={"data": [{"id": "c1"}], "paging": {
                "next": f"https://graph.facebook.com/v24.0/me/conversations?limit={limit}&after=p2"}})
        return httpx.Response(200, json={"messages": {"data": [
            {"id": "m", "created_time": "2026-09-01T10:00:00+0000", "from": {"id": "9", "username": "u"}, "message": "hi"}]}})

    convs = await make_ig(handler).fetch_history(10)
    assert len(convs) == 2
    assert seen_limits[:3] == ["25", "10", "5"] and seen_limits[-1] == "5"
