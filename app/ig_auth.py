"""Подключение Instagram мастера по ссылке (Instagram Business Login).

1. Администратор открывает /auth/instagram/start?token=<ADMIN_TOKEN> и копирует ссылку.
2. Мастер открывает ссылку на своём телефоне и нажимает «Разрешить» в окне Instagram.
3. Instagram возвращает её на /auth/instagram/callback, сервер получает долгий токен,
   сохраняет его в базе и подписывает аккаунт на webhook.

Пароль мастера никуда не попадает. Ссылка одноразовая и действует 24 часа.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from .config import Settings
from .service import Service
from .telegram import esc

log = logging.getLogger(__name__)

SCOPES = "instagram_business_basic,instagram_business_manage_messages"
LINK_TTL = 86400


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(title)}</title>
<style>
:root{{--bg:#f6f5f2;--card:#fff;--text:#1d1d1f;--muted:#6e6e73;--accent:#0a66ff;--border:#ddd}}
@media (prefers-color-scheme: dark){{:root{{--bg:#121214;--card:#1c1c1f;--text:#f2f2f3;--muted:#9a9aa1;--accent:#3a82ff;--border:#333}}}}
body{{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,-apple-system,sans-serif}}
main{{max-width:640px;margin:0 auto;padding:32px 16px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:20px}}
p.muted{{color:var(--muted)}} textarea{{width:100%;box-sizing:border-box;font:13px/1.4 ui-monospace,monospace;
padding:10px;border-radius:10px;border:1px solid var(--border);background:var(--bg);color:var(--text)}}
button{{font:inherit;background:var(--accent);color:#fff;border:0;border-radius:10px;padding:10px 16px;margin-top:10px;cursor:pointer}}
</style></head><body><main><div class="card"><h1>{esc(title)}</h1>{body}</div></main></body></html>"""


def build_router(settings: Settings, service: Service) -> APIRouter:
    router = APIRouter()
    db = service.db

    def redirect_uri() -> str:
        return f"{settings.public_url}/auth/instagram/callback"

    @router.get("/auth/instagram/start", response_class=HTMLResponse)
    async def start(token: str = ""):
        if not settings.admin_token or not hmac.compare_digest(token, settings.admin_token):
            raise HTTPException(status_code=403)
        if settings.ig_api_mode == "facebook":
            return _page("Ссылка не нужна", "<p>Бот подключён через вход Facebook (IG_API_MODE=facebook): "
                         "токен страницы выдаёт администратор страницы в панели Meta, мастеру ничего делать не нужно.</p>")
        missing = [n for n, v in (("IG_APP_ID", settings.ig_app_id), ("IG_APP_SECRET", settings.ig_app_secret),
                                  ("PUBLIC_URL / домен Railway", settings.public_url)) if not v]
        if missing:
            return _page("Не хватает настроек", f"<p>Добавьте в Railway → Variables: {esc(', '.join(missing))}.</p>")
        state = secrets.token_urlsafe(24)
        db.kv_set(f"ig_auth_state:{state}", str(time.time() + LINK_TTL))
        link = "https://www.instagram.com/oauth/authorize?" + urlencode({
            "client_id": settings.ig_app_id,
            "redirect_uri": redirect_uri(),
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
            "force_reauth": "true",
        })
        return _page("Ссылка для мастера", f"""
<p>Отправьте эту ссылку мастеру. Она открывает её на своём телефоне, входит в Instagram <b>tattoo.by.gubska</b>
(если ещё не вошла) и нажимает «Разрешить».</p>
<textarea id="l" rows="5" readonly>{esc(link)}</textarea>
<button id="b" type="button">Скопировать ссылку</button>
<p class="muted">Ссылка одноразовая и действует 24 часа. Когда мастер разрешит доступ,
в Telegram-группу придёт «Instagram подключён». Если нужна новая ссылка, обновите эту страницу.</p>
<p class="muted">В настройках Meta (Instagram business login → OAuth redirect URIs) должен быть адрес:<br>
<code>{esc(redirect_uri())}</code></p>
<script>document.getElementById("b").onclick=async()=>{{const t=document.getElementById("l");
try{{await navigator.clipboard.writeText(t.value);document.getElementById("b").textContent="Скопировано";}}
catch(e){{t.select();}}}};</script>""")

    @router.get("/auth/instagram/callback", response_class=HTMLResponse)
    async def callback(request: Request):
        q = request.query_params
        state = q.get("state", "")
        expires = db.kv_get(f"ig_auth_state:{state}") if state else None
        if not expires or float(expires) < time.time():
            return _page("Ссылка устарела", "<p>Попросите прислать новую ссылку.</p>")
        db.execute("DELETE FROM kv WHERE key = ?", (f"ig_auth_state:{state}",))  # одноразовая
        if q.get("error"):
            await service.alert("Мастер не дал доступ к Instagram (нажата «Отмена» в окне Instagram).")
            return _page("Доступ не выдан", "<p>Вы отменили подключение. Если это ошибка, попросите новую ссылку.</p>")
        code = q.get("code", "").removesuffix("#_")
        try:
            short = await service.ig.exchange_code(settings.ig_app_id, settings.ig_app_secret, redirect_uri(), code)
            token, expires_in = await service.ig.long_lived_token(settings.ig_app_secret, short)
            username = await service.ig.whoami(token)
        except Exception as e:
            log.exception("instagram auth failed")
            await service.alert(f"Не удалось подключить Instagram: {esc(e)}")
            return _page("Не получилось", "<p>Что-то пошло не так. Мы уже получили уведомление и пришлём новую ссылку.</p>")
        db.kv_set("ig_token", token)
        db.kv_set("ig_token_expires_at", str(time.time() + expires_in))
        try:
            await service.ig.subscribe_app()
            subscribed = "аккаунт подписан на сообщения"
        except Exception as e:
            log.exception("subscribe failed")
            subscribed = f"но подписка на сообщения не удалась: {esc(e)}"
        await service.alert(f"✅ Instagram подключён: @{esc(username)}, {subscribed}. Токен действует 60 дней и продлевается сам.")
        return _page("Готово!", "<p>Доступ выдан, эту страницу можно закрыть. Спасибо!</p>")

    return router
