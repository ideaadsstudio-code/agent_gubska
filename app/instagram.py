"""Instagram API with Instagram Login: подпись webhook, разбор событий, отправка сообщений."""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx

log = logging.getLogger(__name__)

IG_TEXT_LIMIT = 1000  # лимит длины текстового сообщения Instagram


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


@dataclass
class IncomingEvent:
    kind: str  # message | echo | deleted
    client_id: str  # IGSID клиента
    mid: str
    text: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    timestamp: float = 0.0  # секунды


def parse_webhook(payload: dict[str, Any]) -> list[IncomingEvent]:
    """Возвращает только интересующие нас события. Прочтения, реакции и т. п. пропускаются."""
    events: list[IncomingEvent] = []
    if payload.get("object") != "instagram":
        return events
    for entry in payload.get("entry", []):
        for item in entry.get("messaging", []):
            message = item.get("message")
            if not message or not message.get("mid"):
                continue
            ts = float(item.get("timestamp", 0)) / 1000.0
            sender = str(item.get("sender", {}).get("id", ""))
            recipient = str(item.get("recipient", {}).get("id", ""))
            if message.get("is_deleted"):
                events.append(IncomingEvent("deleted", client_id=sender, mid=message["mid"], timestamp=ts))
                continue
            attachments = [
                {"type": a.get("type", "file"), "url": (a.get("payload") or {}).get("url", "")}
                for a in message.get("attachments", []) or []
            ]
            if message.get("is_echo"):
                # Эхо: отправитель наш аккаунт, клиент в recipient.
                events.append(
                    IncomingEvent("echo", client_id=recipient, mid=message["mid"], text=message.get("text", ""),
                                  attachments=attachments, timestamp=ts)
                )
                continue
            if message.get("is_unsupported"):
                attachments.append({"type": "unsupported", "url": ""})
            events.append(
                IncomingEvent("message", client_id=sender, mid=message["mid"], text=message.get("text", ""),
                              attachments=attachments, timestamp=ts)
            )
    return events


def split_text(text: str, limit: int = IG_TEXT_LIMIT) -> list[str]:
    """Делит длинный ответ по абзацам, затем по предложениям, затем жёстко."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    current = ""
    for piece in _pieces(text, limit):
        candidate = f"{current}\n\n{piece}" if current else piece
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                parts.append(current)
            current = piece
    if current:
        parts.append(current)
    return parts


def _pieces(text: str, limit: int) -> list[str]:
    out: list[str] = []
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) <= limit:
            out.append(para)
            continue
        sentence_buf = ""
        for sentence in para.replace(". ", ".\n").split("\n"):
            while len(sentence) > limit:
                out.append(sentence[:limit])
                sentence = sentence[limit:]
            candidate = f"{sentence_buf} {sentence}".strip()
            if len(candidate) <= limit:
                sentence_buf = candidate
            else:
                out.append(sentence_buf)
                sentence_buf = sentence
        if sentence_buf:
            out.append(sentence_buf)
    return out


class InstagramClient:
    def __init__(self, access_token_getter, api_version: str = "v24.0", http: httpx.AsyncClient | None = None):
        self._token = access_token_getter
        self.base = f"https://graph.instagram.com/{api_version}"
        self.http = http or httpx.AsyncClient(timeout=20.0)

    async def send_text(self, recipient_id: str, text: str) -> str:
        """Отправляет текст и возвращает message_id. Тег HUMAN_AGENT боту запрещён и не используется."""
        resp = await self.http.post(
            f"{self.base}/me/messages",
            headers={"Authorization": f"Bearer {self._token()}"},
            json={"recipient": {"id": recipient_id}, "message": {"text": text}},
        )
        resp.raise_for_status()
        return str(resp.json().get("message_id", ""))

    async def get_username(self, user_id: str) -> str | None:
        try:
            resp = await self.http.get(
                f"{self.base}/{user_id}",
                params={"fields": "username,name"},
                headers={"Authorization": f"Bearer {self._token()}"},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("username") or data.get("name")
        except Exception:  # профиль не критичен
            log.info("could not fetch profile for %s", user_id)
            return None

    async def refresh_token(self) -> tuple[str, int]:
        """Обновляет долгий токен. Возвращает (новый токен, срок жизни в секундах)."""
        resp = await self.http.get(
            "https://graph.instagram.com/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": self._token()},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["access_token"], int(data.get("expires_in", 0))
