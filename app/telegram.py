"""Telegram Bot API: карточки для менеджера, кнопки, алерты."""

from __future__ import annotations

import html
import logging
from typing import Any

import httpx2 as httpx

log = logging.getLogger(__name__)


def esc(text: Any) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


def kb(*rows: list[tuple[str, str]]) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def conversation_buttons(conversation_id: int, with_confirm: bool = False) -> dict[str, Any]:
    row = [("Беру диалог", f"take:{conversation_id}"), ("Вернуть боту", f"return:{conversation_id}")]
    rows = [row]
    if with_confirm:
        rows.append([("Подтвердить запись", f"confirm:{conversation_id}")])
    return kb(*rows)


def draft_buttons(draft_id: int) -> dict[str, Any]:
    return kb([("Отправить", f"send:{draft_id}"), ("Исправить", f"edit:{draft_id}"), ("Забрать диалог", f"grab:{draft_id}")])


class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str, http: httpx.AsyncClient | None = None):
        self.base = f"https://api.telegram.org/bot{bot_token}"
        self.chat_id = chat_id
        self.http = http or httpx.AsyncClient(timeout=20.0)

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = await self.http.post(f"{self.base}/{method}", json=payload)
        data = resp.json()
        if not data.get("ok"):
            log.error("telegram %s failed: %s", method, data)
            raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
        return data.get("result") or {}

    async def send(self, text: str, reply_markup: dict | None = None, force_reply: bool = False) -> int:
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if force_reply:
            payload["reply_markup"] = {"force_reply": True, "selective": True}
        result = await self._call("sendMessage", payload)
        return int(result.get("message_id", 0))

    async def send_to(self, chat_id: int | str, text: str) -> None:
        """Ответ в произвольный чат: нужен только для /chatid, пока TELEGRAM_CHAT_ID ещё не задан."""
        await self._call("sendMessage", {"chat_id": chat_id, "text": text[:4096]})

    async def set_webhook(self, url: str, secret: str) -> None:
        await self._call("setWebhook", {
            "url": url,
            "secret_token": secret,
            "allowed_updates": ["message", "callback_query"],
            "drop_pending_updates": False,
        })

    async def edit_markup(self, message_id: int, reply_markup: dict | None) -> None:
        await self._call(
            "editMessageReplyMarkup",
            {"chat_id": self.chat_id, "message_id": message_id, "reply_markup": reply_markup or {"inline_keyboard": []}},
        )

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})
