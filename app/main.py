"""HTTP-слой: webhook Instagram, webhook Telegram, политика конфиденциальности."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from .agent import Agent, load_system_prompt
from .classifier import TopicClassifier
from .config import Settings
from .db import Database
from .images import ImageStore
from .instagram import InstagramClient, verify_signature
from .admin_analysis import build_router as build_admin_analysis
from .ig_auth import build_router as build_ig_auth
from .playground import build_router as build_playground
from .service import Service
from .telegram import TelegramClient, esc

log = logging.getLogger(__name__)


def build_service(settings: Settings) -> Service:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # HTTP-библиотеки пишут в лог полный адрес запроса, а в адресах Telegram и Meta бывают токены.
    for noisy in ("httpx2", "httpx", "httpcore", "httpcore2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    db = Database(settings.db_path)
    # ANTHROPIC_API_KEY читается SDK из окружения.
    client = anthropic.AsyncAnthropic(max_retries=2)
    agent = Agent(settings, client, load_system_prompt(settings))
    ig = InstagramClient(lambda: db.kv_get("ig_token") or settings.ig_access_token, settings.graph_api_version,
                         mode=settings.ig_api_mode)
    tg = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)
    images = ImageStore(client, settings.retention_days) if settings.attach_images else None
    classifier = TopicClassifier(client, settings.classifier_model) if settings.topic_filter else None
    return Service(settings, db, agent, ig, tg, images=images, classifier=classifier)


async def _scheduler(service: Service) -> None:
    while True:
        try:
            await service.tick()
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(60)


def create_app(settings: Settings | None = None, service: Service | None = None, run_scheduler: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    service = service or build_service(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_scheduler:
            await service.register_webhooks()
        task = asyncio.create_task(_scheduler(service)) if run_scheduler else None
        yield
        if task:
            task.cancel()
        await service.drain()

    app = FastAPI(title="Tattoo Instagram agent", lifespan=lifespan)
    app.state.service = service
    app.include_router(build_playground(settings, service.agent, service.classifier, service.db))
    app.include_router(build_ig_auth(settings, service))
    app.include_router(build_admin_analysis(settings, service))

    if settings.playground_token and not settings.playground_token.isalnum():
        log.warning("PLAYGROUND_TOKEN лучше задавать только из букв и цифр: спецсимволы ломаются в адресе страницы.")

    @app.get("/health")
    async def health():
        return {"ok": True, "model": settings.claude_model, "approval_mode": settings.approval_mode}

    @app.get("/webhooks/instagram")
    async def verify(request: Request):
        q = request.query_params
        token = q.get("hub.verify_token", "")
        if (
            q.get("hub.mode") == "subscribe"
            and settings.ig_verify_token
            and hmac.compare_digest(token, settings.ig_verify_token)
        ):
            return PlainTextResponse(q.get("hub.challenge", ""))
        raise HTTPException(status_code=403, detail="verification failed")

    @app.post("/webhooks/instagram")
    async def instagram_webhook(request: Request):
        body = await request.body()
        if not verify_signature(settings.ig_app_secret, body, request.headers.get("X-Hub-Signature-256")):
            raise HTTPException(status_code=403, detail="bad signature")
        try:
            payload = json.loads(body)
        except ValueError:
            raise HTTPException(status_code=400, detail="bad json")
        # Быстро отвечаем 200: ответ клиенту готовится в фоне.
        await service.handle_instagram(payload)
        return {"ok": True}

    @app.post("/webhooks/telegram")
    async def telegram_webhook(request: Request):
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not settings.telegram_webhook_secret or not hmac.compare_digest(secret, settings.telegram_webhook_secret):
            raise HTTPException(status_code=403, detail="bad secret")
        await service.handle_telegram(await request.json())
        return {"ok": True}

    @app.get("/data-deletion", response_class=HTMLResponse)
    async def data_deletion():
        contact = f" or email {esc(settings.privacy_contact)}" if settings.privacy_contact else ""
        return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Data Deletion</title>
<style>body{{font-family:system-ui,sans-serif;max-width:720px;margin:2rem auto;padding:0 16px;line-height:1.55}}</style>
</head><body>
<h1>How to delete your data</h1>
<p>{esc(settings.studio_name)} keeps your Instagram conversation with us for up to {settings.retention_days} days, then deletes it automatically.</p>
<p>To delete it sooner, send us a Direct message on Instagram saying "delete my data"{contact}.
A person from the studio will delete your conversation, the images you sent and any booking request within 30 days
and confirm it to you.</p>
<p>If you delete a single message in Instagram, we delete our copy of it automatically.</p>
<p>See also our <a href="/privacy">Privacy Policy</a>.</p>
</body></html>"""

    @app.get("/privacy", response_class=HTMLResponse)
    async def privacy():
        contact = f"<p>Contact: {esc(settings.privacy_contact)}</p>" if settings.privacy_contact else ""
        return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Privacy Policy</title>
<style>body{{font-family:system-ui,sans-serif;max-width:720px;margin:2rem auto;padding:0 16px;line-height:1.55}}</style>
</head><body>
<h1>Privacy Policy</h1>
<p>This policy describes how {esc(settings.studio_name)} handles messages sent to our Instagram account.</p>
<h2>What we process</h2>
<p>When you message us on Instagram, we receive your Instagram-scoped ID, username, and the content of your messages,
including images you send. An AI assistant helps answer your questions; a person from the studio can take over at any time.
Write "human" to talk to a person.</p>
<h2>Why</h2>
<p>To answer your questions, prepare booking requests, and pass your request to the studio manager.</p>
<h2>Service providers</h2>
<p>Messages are processed by Meta (Instagram), Anthropic (AI model provider) and Telegram (internal notifications to our manager).
We do not use your messages to train AI models.</p>
<h2>Retention and deletion</h2>
<p>Conversations are kept for up to {settings.retention_days} days and then deleted. If you delete a message in Instagram,
we delete it from our records. You can ask us to delete your data at any time by messaging us.</p>
<h2>Personal conversations</h2>
<p>The assistant answers only conversations about tattoo services. Other conversations with the artist are not answered
by the assistant and their content is not stored. The first messages of a new conversation are checked automatically to decide this.</p>
<h2>Payments</h2>
<p>We never ask for card numbers or identity documents in chat.</p>
{contact}
</body></html>"""

    return app
