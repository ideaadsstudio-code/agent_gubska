"""Instagram API with Instagram Login: подпись webhook, разбор событий, отправка сообщений."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
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
    kind: str  # message | echo | deleted | referral
    client_id: str  # IGSID клиента
    mid: str
    text: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    timestamp: float = 0.0  # секунды
    from_ad: bool = False  # клиент пришёл из рекламы «Написать в Direct»


def _is_ad(referral: dict[str, Any] | None) -> bool:
    return bool(referral) and (referral.get("source") == "ADS" or bool(referral.get("ad_id")))


def parse_webhook(payload: dict[str, Any]) -> list[IncomingEvent]:
    """Возвращает только интересующие нас события. Прочтения, реакции и т. п. пропускаются."""
    events: list[IncomingEvent] = []
    if payload.get("object") != "instagram":
        return events
    for entry in payload.get("entry", []):
        for item in entry.get("messaging", []):
            message = item.get("message")
            ts = float(item.get("timestamp", 0)) / 1000.0
            sender = str(item.get("sender", {}).get("id", ""))
            recipient = str(item.get("recipient", {}).get("id", ""))
            if not message and _is_ad(item.get("referral")):
                # Клиент открыл Direct из рекламы, но ещё ничего не написал.
                events.append(IncomingEvent("referral", client_id=sender, mid=f"ref:{sender}:{int(ts)}",
                                            timestamp=ts, from_ad=True))
                continue
            if not message or not message.get("mid"):
                continue
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
                              attachments=attachments, timestamp=ts,
                              from_ad=_is_ad(message.get("referral")) or _is_ad(item.get("referral")))
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


class ReduceDataError(RuntimeError):
    """Meta: «Please reduce the amount of data you're asking for» — запрос надо сделать меньше."""


def _with_limit(url: str, params: dict | None, limit: int) -> tuple[str, dict | None]:
    """Тот же запрос (или ссылка next) с другим размером страницы."""
    if params is not None:
        return url, {**params, "limit": str(limit)}
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query["limit"] = str(limit)
    return urlunsplit(parts._replace(query=urlencode(query))), None


class InstagramClient:
    """Два режима подключения:
    - "facebook": Instagram API через вход Facebook. Токен страницы Facebook, к которой привязан Instagram,
      запросы на graph.facebook.com, "/me" означает эту страницу. Токен страницы не истекает.
    - "instagram": Instagram API через вход Instagram (graph.instagram.com). Meta не даёт его новым приложениям.
    Формат webhook и тела запросов в обоих режимах одинаковый."""

    def __init__(self, access_token_getter, api_version: str = "v24.0", http: httpx.AsyncClient | None = None,
                 mode: str = "instagram"):
        self._token = access_token_getter
        self.mode = mode
        host = "graph.facebook.com" if mode == "facebook" else "graph.instagram.com"
        self.base = f"https://{host}/{api_version}"
        self.http = http or httpx.AsyncClient(timeout=20.0)
        self.retry_delay = 2.0  # секунды между повторами запроса к Meta (в тестах 0)

    async def send_text(self, recipient_id: str, text: str) -> str:
        """Отправляет текст и возвращает message_id. Тег HUMAN_AGENT боту запрещён и не используется."""
        resp = await self.http.post(
            f"{self.base}/me/messages",
            headers={"Authorization": f"Bearer {self._token()}"},
            json={"recipient": {"id": recipient_id}, "message": {"text": text}},
        )
        self._raise_clean(resp, "Отправка сообщения")
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

    async def had_messages_before(self, user_id: str, before_ts: float) -> bool | None:
        """Была ли переписка с этим человеком до запуска бота. Читаем только даты сообщений, без текста.
        None, если API не ответил."""
        headers = {"Authorization": f"Bearer {self._token()}"}
        try:
            resp = await self.http.get(
                f"{self.base}/me/conversations",
                params={"platform": "instagram", "user_id": user_id},
                headers=headers,
            )
            resp.raise_for_status()
            conversations = resp.json().get("data", [])
            if not conversations:
                return False
            resp = await self.http.get(
                f"{self.base}/{conversations[0]['id']}",
                params={"fields": "messages{created_time}"},
                headers=headers,
            )
            resp.raise_for_status()
            for m in (resp.json().get("messages") or {}).get("data", []):
                created = datetime.strptime(m["created_time"], "%Y-%m-%dT%H:%M:%S%z").timestamp()
                if created < before_ts:
                    return True
            return False
        except Exception:
            log.warning("could not check conversation history for %s", user_id)
            return None

    async def subscribe_app(self, fields: str | None = None) -> None:
        """Подписывает аккаунт мастера (в режиме facebook: его страницу) на webhook нашего приложения."""
        if fields is None:
            # Подписка заменяет весь список полей, поэтому перечисляем все нужные.
            # Названия различаются: API страницы Facebook ждёт messaging_referrals, Instagram Login — messaging_referral.
            if self.mode == "facebook":
                fields = "messages,messaging_postbacks,messaging_referrals"
            else:
                fields = "messages,messaging_postbacks,messaging_referral"
        resp = await self.http.post(
            f"{self.base}/me/subscribed_apps",
            params={"subscribed_fields": fields},
            headers={"Authorization": f"Bearer {self._token()}"},
        )
        self._raise_clean(resp, "Подписка на webhook")

    # ---- история переписки для анализа (/admin/analysis) ----
    async def own_account(self) -> tuple[str, str]:
        """(id, username) Instagram-аккаунта, от имени которого работает бот."""
        headers = {"Authorization": f"Bearer {self._token()}"}
        if self.mode == "facebook":
            resp = await self.http.get(f"{self.base}/me", params={"fields": "instagram_business_account{id,username}"},
                                       headers=headers)
            self._raise_clean(resp, "Аккаунт Instagram страницы")
            acc = resp.json().get("instagram_business_account") or {}
            if not acc.get("id"):
                raise RuntimeError("К странице не привязан Instagram (нет instagram_business_account)")
            return str(acc["id"]), acc.get("username", "")
        resp = await self.http.get(f"{self.base}/me", params={"fields": "user_id,username"}, headers=headers)
        self._raise_clean(resp, "Аккаунт Instagram")
        data = resp.json()
        return str(data.get("user_id") or data.get("id")), data.get("username", "")

    async def fetch_history(self, max_conversations: int = 300,
                            progress=lambda _msg: None) -> list[dict]:
        """Последние диалоги аккаунта: [{"messages": [{"from_id","from_username","text","ts"}]}].
        Meta отдаёт подробности только по ~20 последним сообщениям каждого диалога.

        Сначала лёгкий список диалогов, потом сообщения каждого диалога отдельно: запрос
        «диалоги вместе с сообщениями» Meta не успевает выполнить (Timeout, code -2)."""
        headers = {"Authorization": f"Bearer {self._token()}"}
        url: str | None = f"{self.base}/me/conversations"
        page_size = 25
        params: dict | None = {"platform": "instagram", "limit": str(page_size), "fields": "id"}
        ids: list[str] = []
        while url and len(ids) < max_conversations:
            try:
                body = await self._graph_get(url, params, headers, "Список диалогов")
            except ReduceDataError:
                # Meta просит запрашивать меньше: уменьшаем страницу, пока не начнёт отвечать.
                if page_size == 1:
                    raise
                page_size = {25: 10, 10: 5, 5: 2}.get(page_size, 1)
                url, params = _with_limit(url, params, page_size)
                progress(f"Meta просит меньше данных за раз, загружаю по {page_size}")
                continue
            ids.extend(c["id"] for c in body.get("data", []) if c.get("id"))
            progress(f"Найдено диалогов: {min(len(ids), max_conversations)}")
            url = (body.get("paging") or {}).get("next")
            params = None  # в ссылке next параметры уже есть (без токена: он в заголовке)
        ids = ids[:max_conversations]

        sem = asyncio.Semaphore(4)
        done = {"n": 0, "skipped": 0}

        async def one(conv_id: str) -> dict | None:
            async with sem:
                try:
                    msgs = await self._conversation_messages(conv_id, headers)
                except Exception:
                    log.warning("skip conversation %s", conv_id)
                    done["skipped"] += 1
                    msgs = None
                done["n"] += 1
                if done["n"] % 10 == 0 or done["n"] == len(ids):
                    progress(f"Загружено диалогов: {done['n']}/{len(ids)}")
                return {"messages": msgs} if msgs is not None else None

        results = await asyncio.gather(*(one(i) for i in ids))
        if done["skipped"]:
            progress(f"Загружено диалогов: {len(ids) - done['skipped']}, пропущено из-за ошибок Meta: {done['skipped']}")
        return [r for r in results if r is not None]

    async def _conversation_messages(self, conv_id: str, headers: dict) -> list[dict]:
        msg_fields = "id,created_time,from,message"
        raw = None
        for n in (20, 10):
            try:
                body = await self._graph_get(f"{self.base}/{conv_id}",
                                             {"fields": f"messages.limit({n}){{{msg_fields}}}"}, headers, "Диалог")
                raw = (body.get("messages") or {}).get("data", [])
                break
            except RuntimeError:
                continue
        if raw is None:
            # Тяжело даже так: берём только id сообщений и дочитываем каждое отдельно.
            body = await self._graph_get(f"{self.base}/{conv_id}", {"fields": "messages.limit(20){id}"}, headers, "Диалог")
            raw = (body.get("messages") or {}).get("data", [])
        out = []
        for m in raw[:20]:
            if "message" not in m and m.get("id"):
                try:
                    m = await self._graph_get(f"{self.base}/{m['id']}", {"fields": msg_fields}, headers, "Сообщение")
                except RuntimeError:
                    continue
            try:
                ts = datetime.strptime(m["created_time"], "%Y-%m-%dT%H:%M:%S%z").timestamp()
            except (KeyError, ValueError):
                ts = 0.0
            sender = m.get("from") or {}
            out.append({"from_id": str(sender.get("id", "")), "from_username": sender.get("username", ""),
                        "text": m.get("message", "") or "", "ts": ts})
        return out

    async def _graph_get(self, url: str, params: dict | None, headers: dict, what: str, attempts: int = 3) -> dict:
        """GET с повтором при таймауте: сетевом, 5xx или таймауте самой Meta (HTTP 400, code -2)."""
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                resp = await self.http.get(url, params=params, headers=headers, timeout=90.0)
            except httpx.TimeoutException:
                if last:
                    raise RuntimeError(f"{what}: Instagram не ответил за 90 секунд")
                await asyncio.sleep(self.retry_delay * (attempt + 1))
                continue
            if not resp.is_error:
                return resp.json()
            retryable = resp.status_code >= 500
            try:
                err = resp.json().get("error") or {}
                if "reduce the amount of data" in (err.get("message") or "").lower():
                    raise ReduceDataError(f"{what}: Meta просит запрашивать меньше данных за раз")
                if err.get("code") == -2 and last:
                    # Таймаут на стороне Meta даже после повторов: запрос тоже надо уменьшить.
                    raise ReduceDataError(f"{what}: Meta не успевает ответить (Timeout)")
                retryable = retryable or err.get("code") in (-2, 1, 2, 4) or err.get("is_transient") is True
            except ValueError:
                pass
            if retryable and not last:
                await asyncio.sleep(self.retry_delay * (attempt + 1))
                continue
            self._raise_clean(resp, what)
        raise AssertionError("unreachable")

    # ---- Instagram Business Login: мастер разрешает доступ по ссылке ----
    @staticmethod
    def _raise_clean(resp: httpx.Response, what: str) -> None:
        """Ошибка без адреса запроса: в нём бывают токены и секреты."""
        if resp.is_error:
            try:
                body = resp.json()
                err = body.get("error") if isinstance(body.get("error"), dict) else {}
                detail = body.get("error_message") or err.get("message") or ""
                if err.get("code"):
                    detail = f"{detail} (code {err['code']})"
            except ValueError:
                detail = ""
            raise RuntimeError(f"{what}: HTTP {resp.status_code} {detail}".strip())

    async def exchange_code(self, app_id: str, app_secret: str, redirect_uri: str, code: str) -> str:
        """Код из ссылки → короткий токен (1 час)."""
        resp = await self.http.post(
            "https://api.instagram.com/oauth/access_token",
            data={"client_id": app_id, "client_secret": app_secret, "grant_type": "authorization_code",
                  "redirect_uri": redirect_uri, "code": code},
        )
        self._raise_clean(resp, "Обмен кода на токен")
        body = resp.json()
        data = body["data"][0] if isinstance(body.get("data"), list) and body["data"] else body
        return data["access_token"]

    async def long_lived_token(self, app_secret: str, short_token: str) -> tuple[str, int]:
        """Короткий токен → долгий (60 дней)."""
        resp = await self.http.get(
            "https://graph.instagram.com/access_token",
            params={"grant_type": "ig_exchange_token", "client_secret": app_secret, "access_token": short_token},
        )
        self._raise_clean(resp, "Получение долгого токена")
        data = resp.json()
        return data["access_token"], int(data.get("expires_in", 0))

    async def whoami(self, token: str) -> str:
        resp = await self.http.get(f"{self.base}/me", params={"fields": "user_id,username"},
                                   headers={"Authorization": f"Bearer {token}"})
        self._raise_clean(resp, "Профиль")
        return resp.json().get("username", "")

    async def refresh_token(self) -> tuple[str, int]:
        """Обновляет долгий токен. Возвращает (новый токен, срок жизни в секундах)."""
        resp = await self.http.get(
            "https://graph.instagram.com/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": self._token()},
        )
        if resp.is_error:
            # Токен стоит в адресе запроса, поэтому стандартная ошибка httpx его бы раскрыла.
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except ValueError:
                detail = ""
            raise RuntimeError(f"Instagram вернул HTTP {resp.status_code}: {detail or 'без описания'}")
        data = resp.json()
        return data["access_token"], int(data.get("expires_in", 0))
