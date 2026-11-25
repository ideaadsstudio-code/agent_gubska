from __future__ import annotations

import copy
import hashlib
import hmac
import itertools
import json
from pathlib import Path
from typing import Any

import anthropic
import httpx2 as httpx
import pytest
from anthropic.types.beta import BetaMessage

from app.agent import Agent
from app.config import Settings
from app.db import Database
from app.service import Service

ROOT = Path(__file__).resolve().parent.parent
CLIENT_ID = "1789"
OUR_ID = "9000"
T0 = 1_780_000_000.0  # фиксированное «сейчас»


# ---------------------------------------------------------------- фейки
def msg(*blocks: dict, stop: str = "end_turn", model: str = "claude-opus-5-5", usage: dict | None = None) -> BetaMessage:
    return BetaMessage.model_validate({
        "id": "msg_x", "type": "message", "role": "assistant", "model": model,
        "content": list(blocks), "stop_reason": stop, "stop_sequence": None,
        "usage": usage or {"input_tokens": 1000, "output_tokens": 100,
                           "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    })


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def thinking(sig: str = "sig") -> dict:
    return {"type": "thinking", "thinking": "", "signature": sig}


def tool(name: str, args: dict, tid: str = "tu_1") -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": args}


class FakeClaude:
    def __init__(self):
        self.script: list[Any] = []
        self.calls: list[dict] = []
        self.beta = self
        self.messages = self

    def with_options(self, **_: Any) -> "FakeClaude":
        return self

    def queue(self, *items: Any) -> None:
        self.script.extend(items)

    async def create(self, **params: Any) -> BetaMessage:
        self.calls.append(copy.deepcopy(params))
        if not self.script:
            raise AssertionError("Claude called but nothing scripted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeIG:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.fail = False
        self.refresh_result: tuple[str, int] | Exception = ("new-token", 60 * 86400)
        self._ids = itertools.count(1)

    async def send_text(self, recipient_id: str, text: str) -> str:
        if self.fail:
            raise RuntimeError("instagram down")
        self.sent.append((recipient_id, text))
        return f"out_{next(self._ids)}"

    async def get_username(self, user_id: str) -> str | None:
        return "ink_lover"

    async def refresh_token(self) -> tuple[str, int]:
        if isinstance(self.refresh_result, Exception):
            raise self.refresh_result
        return self.refresh_result


class FakeTG:
    def __init__(self):
        self.sent: list[dict] = []
        self.edits: list[int] = []
        self.answers: list[str] = []
        self._ids = itertools.count(100)

    async def send(self, text: str, reply_markup: dict | None = None, force_reply: bool = False) -> int:
        mid = next(self._ids)
        self.sent.append({"id": mid, "text": text, "markup": reply_markup, "force_reply": force_reply})
        return mid

    async def edit_markup(self, message_id: int, reply_markup: dict | None) -> None:
        self.edits.append(message_id)

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.answers.append(text)

    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent]

    def buttons(self, index: int = -1) -> list[str]:
        markup = self.sent[index]["markup"] or {}
        return [b["callback_data"] for row in markup.get("inline_keyboard", []) for b in row]


class Clock:
    def __init__(self, now: float = T0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, hours: float = 0, seconds: float = 0) -> None:
        self.now += hours * 3600 + seconds


# ---------------------------------------------------------------- фикстуры
@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        ig_app_secret="app-secret",
        ig_verify_token="verify-me",
        telegram_chat_id="-100",
        telegram_webhook_secret="tg-secret",
        approval_mode=False,
        debounce_seconds=0,
        db_path=str(tmp_path / "test.db"),
        system_prompt_path=str(ROOT / "prompts" / "system.md"),
        knowledge_base_path=str(ROOT / "knowledge" / "knowledge_base.md"),
    )


class Env:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.clock = Clock()
        self.claude = FakeClaude()
        self.ig = FakeIG()
        self.tg = FakeTG()
        self.db = Database(settings.db_path)
        self.agent = Agent(settings, self.claude, "SYSTEM PROMPT")
        self.service = Service(settings, self.db, self.agent, self.ig, self.tg, clock=self.clock)
        self._mid = itertools.count(1)

    def payload(self, text: str = "", kind: str = "message", mid: str | None = None,
                attachments: list[dict] | None = None, deleted: bool = False) -> dict:
        mid = mid or f"m_{next(self._mid)}"
        message: dict[str, Any] = {"mid": mid}
        if text:
            message["text"] = text
        if attachments:
            message["attachments"] = attachments
        if deleted:
            message["is_deleted"] = True
        if kind == "echo":
            message["is_echo"] = True
            sender, recipient = OUR_ID, CLIENT_ID
        else:
            sender, recipient = CLIENT_ID, OUR_ID
        return {"object": "instagram", "entry": [{"id": OUR_ID, "time": int(self.clock() * 1000), "messaging": [
            {"sender": {"id": sender}, "recipient": {"id": recipient},
             "timestamp": int(self.clock() * 1000), "message": message}
        ]}]}

    async def client_says(self, text: str = "", **kw: Any) -> None:
        await self.service.handle_instagram(self.payload(text, **kw))
        await self.service.drain()

    async def manager_echo(self, text: str, mid: str | None = None) -> None:
        await self.service.handle_instagram(self.payload(text, kind="echo", mid=mid))
        await self.service.drain()

    async def press(self, data: str, chat_id: str = "-100") -> None:
        await self.service.handle_telegram({"callback_query": {
            "id": "cb", "data": data, "from": {"username": "manager_anna"},
            "message": {"message_id": 1, "chat": {"id": int(chat_id)}},
        }})
        await self.service.drain()

    def conv(self):
        return self.db.find_conversation(CLIENT_ID)

    def sent_texts(self) -> list[str]:
        return [t for _, t in self.ig.sent]


@pytest.fixture
def env(settings) -> Env:
    return Env(settings)


def sign(body: bytes, secret: str = "app-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def api_error(status: int = 500) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request, json={"error": {"type": "api_error", "message": "boom"}})
    cls = {429: anthropic.RateLimitError, 400: anthropic.BadRequestError}.get(status, anthropic.InternalServerError)
    return cls("boom", response=response, body=None)


def timeout_error() -> anthropic.APITimeoutError:
    return anthropic.APITimeoutError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))


def dumps(obj: Any) -> bytes:
    return json.dumps(obj).encode()


class FakeImages:
    """Заменяет ImageStore: «скачивает» и «загружает» без сети."""

    def __init__(self):
        self.uploaded: list[str] = []
        self.deleted: list[str] = []
        self.fail_urls: set[str] = set()
        self._ids = itertools.count(1)

    async def ingest(self, url: str, name: str) -> str | None:
        if url in self.fail_urls:
            raise RuntimeError("cdn 404")
        file_id = f"file_{next(self._ids)}"
        self.uploaded.append(file_id)
        return file_id

    async def delete(self, file_id: str) -> None:
        self.deleted.append(file_id)


@pytest.fixture
def ienv(settings) -> Env:
    e = Env(settings)
    e.images = FakeImages()
    e.service.images = e.images
    return e


def image(url: str = "https://cdn.example/ref.jpg") -> list[dict]:
    return [{"type": "image", "payload": {"url": url}}]
