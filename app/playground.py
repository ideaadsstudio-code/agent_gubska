"""Тестовый чат /playground: переписка с настоящим ботом без Instagram.

Тот же агент, промпт, база знаний и фильтр тем, но своя база в памяти, а Instagram и Telegram
заменены заглушками: ответы и карточки менеджера показываются прямо на странице.
Расход Claude учитывается в общей сводке и бюджете.
"""

from __future__ import annotations

import dataclasses
import hmac
import itertools
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .agent import Agent
from .classifier import TopicClassifier
from .config import Settings
from .db import Database
from .service import Service

OUR_ID = "playground-studio"


class _IG:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self._ids = itertools.count(1)

    async def send_text(self, recipient_id: str, text: str) -> str:
        self.sent.append((recipient_id, text))
        return f"pg_out_{next(self._ids)}"

    async def get_username(self, user_id: str) -> str | None:
        return f"test_{user_id[-6:]}"

    async def had_messages_before(self, user_id: str, before_ts: float) -> bool | None:
        return False

    async def subscribe_app(self, fields: str = "") -> None:
        return None

    async def refresh_token(self):
        raise RuntimeError("not available in playground")


class _TG:
    def __init__(self):
        self.sent: list[str] = []
        self._ids = itertools.count(1)

    async def send(self, text: str, reply_markup: dict | None = None, force_reply: bool = False) -> int:
        self.sent.append(text)
        return next(self._ids)

    async def send_to(self, chat_id, text: str) -> None:
        self.sent.append(text)

    async def edit_markup(self, message_id: int, reply_markup: dict | None) -> None:
        return None

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        return None

    async def set_webhook(self, url: str, secret: str) -> None:
        return None


class MessageIn(BaseModel):
    token: str
    session: str
    text: str


class ResetIn(BaseModel):
    token: str
    session: str


def build_router(settings: Settings, agent: Agent, classifier: TopicClassifier | None, main_db: Database) -> APIRouter:
    pg_settings = dataclasses.replace(
        settings,
        approval_mode=False,
        debounce_seconds=0,
        skip_existing_contacts=False,
        db_path=":memory:",
        bot_launch_at=time.time(),
    )
    db = Database(":memory:")
    ig, tg = _IG(), _TG()
    service = Service(pg_settings, db, agent, ig, tg, images=None, classifier=classifier)  # type: ignore[arg-type]
    mids = itertools.count(1)
    router = APIRouter()

    def check(token: str) -> None:
        if not settings.playground_token or not hmac.compare_digest(token, settings.playground_token):
            raise HTTPException(status_code=403, detail="wrong token")

    def client_id(session: str) -> str:
        return "pg_" + "".join(ch for ch in session if ch.isalnum())[:32]

    @router.get("/playground", response_class=HTMLResponse)
    async def page():
        if not settings.playground_token:
            raise HTTPException(status_code=404)
        return PAGE

    @router.post("/playground/api/message")
    async def message(body: MessageIn) -> dict[str, Any]:
        check(body.token)
        text = body.text.strip()[:2000]
        if not text:
            raise HTTPException(status_code=400, detail="empty")
        cid = client_id(body.session)
        sent_before, tg_before, spend_before = len(ig.sent), len(tg.sent), db.spend_since(0)
        now_ms = int(time.time() * 1000)
        await service.handle_instagram({"object": "instagram", "entry": [{"id": OUR_ID, "time": now_ms, "messaging": [{
            "sender": {"id": cid}, "recipient": {"id": OUR_ID}, "timestamp": now_ms,
            "message": {"mid": f"pg_{next(mids)}", "text": text},
        }]}]})
        await service.drain()
        spent = db.spend_since(0) - spend_before
        if spent > 0:
            main_db.record_usage(None, f"{settings.claude_model} (playground)", 0, 0, 0, 0, spent)
        conv = db.find_conversation(cid)
        return {
            "replies": [t for r, t in ig.sent[sent_before:] if r == cid],
            "manager": tg.sent[tg_before:],
            "scope": conv.scope if conv else "unknown",
            "status": conv.status if conv else "bot",
            "cost_usd": round(spent, 4),
        }

    @router.post("/playground/api/reset")
    async def reset(body: ResetIn) -> dict[str, bool]:
        check(body.token)
        conv = db.find_conversation(client_id(body.session))
        if conv:
            db.execute("DELETE FROM conversations WHERE id = ?", (conv.id,))
        return {"ok": True}

    return router


PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Тестовый чат</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--text:#1d1d1f;--muted:#6e6e73;--me:#0a66ff;--me-text:#fff;--bot:#ececec;--note:#fff6dc;--note-text:#5c4500;--border:#ddd}
@media (prefers-color-scheme: dark){:root{--bg:#121214;--card:#1c1c1f;--text:#f2f2f3;--muted:#9a9aa1;--me:#3a82ff;--bot:#2a2a2e;--note:#3a3220;--note-text:#f3dfa8;--border:#333}}
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--text);font:16px/1.45 system-ui,-apple-system,sans-serif;display:flex;flex-direction:column}
header{padding:12px 16px;border-bottom:1px solid var(--border);display:flex;gap:8px;align-items:center;justify-content:space-between}
header b{font-size:17px}header small{color:var(--muted);display:block;font-size:13px}
button{font:inherit;border:1px solid var(--border);background:var(--card);color:var(--text);border-radius:10px;padding:8px 12px;cursor:pointer}
#log{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:8px}
.m{max-width:85%;padding:9px 13px;border-radius:16px;white-space:pre-wrap;word-wrap:break-word}
.me{align-self:flex-end;background:var(--me);color:var(--me-text);border-bottom-right-radius:4px}
.bot{align-self:flex-start;background:var(--bot);border-bottom-left-radius:4px}
.note{align-self:stretch;background:var(--note);color:var(--note-text);font-size:14px;border-radius:10px;max-width:none}
.sys{align-self:center;color:var(--muted);font-size:13px;text-align:center}
form{display:flex;gap:8px;padding:12px 16px;border-top:1px solid var(--border);background:var(--card)}
textarea{flex:1;font:inherit;resize:none;border:1px solid var(--border);border-radius:12px;padding:9px 12px;background:var(--bg);color:var(--text);min-height:42px;max-height:140px}
#send{background:var(--me);color:#fff;border:none}
</style></head><body>
<header><div><b>Тестовый чат с ботом</b><small>Вы пишете как клиент. В Instagram ничего не уходит.</small></div>
<button id="reset" type="button">Новый диалог</button></header>
<div id="log" aria-live="polite"></div>
<form id="f"><textarea id="t" rows="1" placeholder="Сообщение…" required></textarea><button id="send">Отправить</button></form>
<script>
const token = new URLSearchParams(location.search).get("token") || "";
const store = (k, v) => { try { v === undefined ? null : localStorage.setItem(k, v); return localStorage.getItem(k); } catch (e) { return null; } };
let session = store("pg_session") || "";
if (!session) { session = Math.random().toString(36).slice(2); store("pg_session", session); }
const log = document.getElementById("log");
function add(cls, text) { const d = document.createElement("div"); d.className = "m " + cls; d.textContent = text; log.appendChild(d); log.scrollTop = log.scrollHeight; return d; }
function plain(html) { const d = document.createElement("div"); d.innerHTML = html; return d.textContent; }
const scopeNote = {ignored: "Фильтр решил, что диалог не про тату: бот молчит, в Instagram его вёл бы мастер.",
  personal: "Диалог отнесён к личным: бот молчит.", unknown: "Пока неясно, про тату ли диалог: бот ждёт следующего сообщения."};
async function post(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({token, session, ...body})});
  if (r.status === 403) throw new Error("Неверная ссылка: в адресе нужен ?token=…");
  if (!r.ok) throw new Error("Ошибка сервера " + r.status);
  return r.json();
}
document.getElementById("f").addEventListener("submit", async (e) => {
  e.preventDefault();
  const t = document.getElementById("t"); const text = t.value.trim(); if (!text) return;
  t.value = ""; add("me", text); const wait = add("sys", "бот думает…");
  try {
    const res = await post("/playground/api/message", {text});
    wait.remove();
    res.replies.forEach(r => add("bot", r));
    res.manager.forEach(n => add("note", "Менеджеру в Telegram:\\n" + plain(n)));
    if (!res.replies.length && scopeNote[res.scope]) add("sys", scopeNote[res.scope]);
    if (!res.replies.length && res.status === "manager" && !res.manager.length) add("sys", "Диалог у менеджера: бот молчит.");
    if (res.cost_usd) add("sys", "$" + res.cost_usd.toFixed(4));
  } catch (err) { wait.textContent = err.message; }
});
document.getElementById("t").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); document.getElementById("send").click(); } });
document.getElementById("reset").addEventListener("click", async () => {
  try { await post("/playground/api/reset", {}); } catch (e) {}
  session = Math.random().toString(36).slice(2); store("pg_session", session);
  log.innerHTML = ""; add("sys", "Новый диалог. Бот видит вас как нового клиента.");
});
add("sys", "Напишите боту, как написал бы клиент. Картинки здесь не поддерживаются.");
</script></body></html>"""
