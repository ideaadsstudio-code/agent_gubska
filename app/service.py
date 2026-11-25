"""Оркестратор: связывает webhook Instagram, агента, Telegram и базу."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from . import language
from .agent import Agent, AgentError, AgentResult
from .config import Settings
from .db import Conversation, Database
from .instagram import IncomingEvent, InstagramClient, parse_webhook, split_text
from .telegram import TelegramClient, conversation_buttons, draft_buttons, esc

log = logging.getLogger(__name__)

URGENCY_LABEL = {"high": "🔴 СРОЧНО", "normal": "🟡 обычная", "low": "⚪ не срочно"}
FAILURE_PLACEHOLDER = "(No reply was generated because of a technical error. The conversation was handed to the manager.)"
DELETED_PLACEHOLDER = "[message deleted by the client]"


def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


class Service:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        agent: Agent,
        ig: InstagramClient,
        tg: TelegramClient,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.db = db
        self.agent = agent
        self.ig = ig
        self.tg = tg
        self.clock = clock
        self._locks: dict[int, asyncio.Lock] = {}
        self._generation: dict[int, int] = {}
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ фон
    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Дождаться всех фоновых задач (для тестов и корректной остановки)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _lock(self, conversation_id: int) -> asyncio.Lock:
        return self._locks.setdefault(conversation_id, asyncio.Lock())

    # ------------------------------------------------------------------ Instagram
    async def handle_instagram(self, payload: dict[str, Any]) -> int:
        handled = 0
        for event in parse_webhook(payload):
            if not self.db.mark_event(f"{event.kind}:{event.mid}"):
                continue  # дубль: Meta повторяет доставку
            handled += 1
            if event.kind == "message":
                await self._on_client_message(event)
            elif event.kind == "echo":
                await self._on_echo(event)
            elif event.kind == "deleted":
                await self._on_deleted(event)
        return handled

    async def _on_client_message(self, ev: IncomingEvent) -> None:
        conv = self.db.get_or_create_conversation(ev.client_id)
        ts = ev.timestamp or self.clock()
        self.db.add_message(conv.id, "client", ev.text, mid=ev.mid, attachments=ev.attachments, created_at=ts)
        fields: dict[str, Any] = {"last_client_msg_at": ts, "last_activity_at": ts}
        pause = self.settings.disclosure_pause_hours * 3600
        if conv.llm_history and conv.last_activity_at and ts - conv.last_activity_at > pause:
            fields["needs_disclosure"] = True
        self.db.update_conversation(conv.id, **fields)
        if not conv.username:
            self._spawn(self._fetch_username(conv.id, ev.client_id))
        self._schedule(conv.id)

    async def _fetch_username(self, conversation_id: int, ig_user_id: str) -> None:
        username = await self.ig.get_username(ig_user_id)
        if username:
            self.db.update_conversation(conversation_id, username=username)

    def _schedule(self, conversation_id: int) -> None:
        """Клиенты часто пишут несколькими сообщениями подряд: ждём паузу и отвечаем на всё сразу."""
        gen = self._generation.get(conversation_id, 0) + 1
        self._generation[conversation_id] = gen
        self._spawn(self._debounced(conversation_id, gen))

    async def _debounced(self, conversation_id: int, gen: int) -> None:
        if self.settings.debounce_seconds > 0:
            await asyncio.sleep(self.settings.debounce_seconds)
        if self._generation.get(conversation_id) != gen:
            return
        try:
            await self.process(conversation_id)
        except Exception as e:
            log.exception("processing failed")
            await self.alert(f"Необработанная ошибка в диалоге #{conversation_id}: {esc(e)}")

    async def _on_echo(self, ev: IncomingEvent) -> None:
        conv = self.db.get_or_create_conversation(ev.client_id)
        if self.db.one("SELECT id FROM messages WHERE mid = ?", (ev.mid,)):
            return  # наше собственное сообщение, mid уже известен
        # Эхо может прийти раньше, чем API вернул mid: сверяем по тексту недавних исходящих.
        own = self.db.one(
            "SELECT id FROM messages WHERE conversation_id = ? AND author = 'bot' AND mid IS NULL AND text = ? "
            "AND created_at >= ? ORDER BY id LIMIT 1",
            (conv.id, ev.text, self.clock() - 300),
        )
        if own:
            self.db.execute("UPDATE messages SET mid = ? WHERE id = ?", (ev.mid, own["id"]))
            return

        # Сообщение отправлено не нашим приложением: менеджер из Instagram или Business Suite.
        ts = ev.timestamp or self.clock()
        self.db.add_message(conv.id, "manager", ev.text, mid=ev.mid, attachments=ev.attachments, created_at=ts)
        self.db.update_conversation(conv.id, last_activity_at=ts)
        self.db.add_note(conv.id, f"The studio manager wrote to the client: «{ev.text or '[attachment]'}»")
        await self._supersede_drafts(conv.id, "the manager replied to the client directly")
        if conv.status != "manager":
            self.db.update_conversation(conv.id, status="manager")
            await self._tg_send(
                f"✋ Менеджер ответил клиенту {self._who(conv)} в Instagram. Бот на паузе в этом диалоге.",
                conversation_buttons(conv.id),
            )

    async def _on_deleted(self, ev: IncomingEvent) -> None:
        row = self.db.one("SELECT * FROM messages WHERE mid = ?", (ev.mid,))
        if not row:
            return
        self.db.execute("DELETE FROM messages WHERE id = ?", (row["id"],))
        text = row["text"]
        if not text:
            return
        conv = self.db.get_conversation(row["conversation_id"])
        if not conv:
            return
        # Единственное место, где история редактируется. Блоки thinking после правки отбросит API (drop_block).
        history = conv.llm_history
        for message in history:
            if message["role"] != "user" or not isinstance(message["content"], list):
                continue
            for block in message["content"]:
                if block.get("type") == "text" and text in block.get("text", ""):
                    block["text"] = block["text"].replace(text, DELETED_PLACEHOLDER)
        notes = [n.replace(text, DELETED_PLACEHOLDER) for n in conv.pending_notes]
        self.db.update_conversation(conv.id, llm_history=history, pending_notes=notes)

    # ------------------------------------------------------------------ основной ход
    async def process(self, conversation_id: int) -> None:
        async with self._lock(conversation_id):
            conv = self.db.get_conversation(conversation_id)
            if conv is None:
                return
            rows = self.db.unprocessed_client_messages(conversation_id)
            if not rows:
                return
            ids = [r["id"] for r in rows]
            texts = [r["text"] for r in rows if r["text"]]
            lang = language.detect_language(" ".join(texts))

            if conv.status == "manager":
                # Бот на паузе. Модель узнает об этих сообщениях, если диалог вернут боту.
                for r in rows:
                    self.db.add_note(conversation_id, f"While the manager handled the chat, the client wrote: «{self._row_text(r)}»")
                self.db.mark_processed(ids)
                return

            await self._supersede_drafts(conversation_id, "the client wrote again before it was approved")
            conv = self.db.get_conversation(conversation_id) or conv  # заметки могли добавиться

            if any(language.wants_human(t) for t in texts):
                await self._handoff_on_request(conv, rows, lang)
                return

            user_content = self._build_user_turn(conv, rows)
            escalated: dict[str, bool] = {"done": False}

            async def execute_tool(name: str, args: dict[str, Any]) -> str:
                if name == "escalate_to_manager":
                    await self.escalate(conversation_id, args.get("reason", ""), args.get("urgency", "normal"))
                    escalated["done"] = True
                    return ("Escalated. Tell the client briefly that a team member will reply here soon. "
                            "Do not promise a specific time.")
                if name == "create_booking_request":
                    await self.create_booking(conversation_id, args)
                    escalated["done"] = True
                    return ("Booking request sent to the manager. Tell the client the manager will contact them "
                            "to confirm the date and the deposit. Do not say the booking is confirmed.")
                raise ValueError(f"Unknown tool {name}")

            try:
                result = await self.agent.respond(conv.llm_history, user_content, execute_tool)
            except AgentError as e:
                await self._on_agent_failure(conv, user_content, ids, lang, e, escalated["done"])
                return

            self.db.append_history(conversation_id, result.new_messages)
            self.db.update_conversation(conversation_id, pending_notes=[])
            self.db.mark_processed(ids)
            await self._record_usage(conversation_id, result)

            reply = result.reply_text
            if not reply and escalated["done"]:
                reply = language.pick(language.HANDOFF_ACK, lang)
            if not reply:
                return
            await self.deliver(conversation_id, self._with_disclosure(conv, reply, lang))

    def _row_text(self, row) -> str:
        text = row["text"] or ""
        attachments = json.loads(row["attachments"] or "[]")
        if attachments:
            kinds = ", ".join(a.get("type", "file") for a in attachments)
            text = f"{text} [attachment: {kinds}]".strip()
        return text

    def _build_user_turn(self, conv: Conversation, rows) -> list[dict[str, Any]]:
        lines = []
        if conv.pending_notes:
            lines.append("[Notes from the system since your last reply]")
            lines.extend(f"- {n}" for n in conv.pending_notes)
            lines.append("")
        received = _utc(rows[-1]["created_at"]).strftime("%A %Y-%m-%d %H:%M UTC")
        lines.append(f"[Client messages, received {received}]")
        for r in rows:
            lines.append(self._row_text(r))
        return [{"type": "text", "text": "\n".join(lines)}]

    def _with_disclosure(self, conv: Conversation, reply: str, lang: str) -> str:
        fresh = self.db.get_conversation(conv.id) or conv
        if fresh.needs_disclosure:
            return f"{language.pick(language.DISCLOSURES, lang)}\n\n{reply}"
        return reply

    async def _handoff_on_request(self, conv: Conversation, rows, lang: str) -> None:
        for r in rows:
            self.db.add_note(conv.id, f"The client wrote: «{self._row_text(r)}» and asked for a human, so the chat was handed to the manager.")
        self.db.mark_processed([r["id"] for r in rows])
        await self.escalate(conv.id, "Клиент попросил живого человека", "normal")
        await self.send_to_client(conv.id, self._with_disclosure(conv, language.pick(language.HANDOFF_ACK, lang), lang))

    async def _on_agent_failure(self, conv, user_content, ids, lang, error: AgentError, already_escalated: bool) -> None:
        log.error("agent failure in conversation %s: %s", conv.id, error)
        # История остаётся корректной: ход клиента и пометка об ошибке, без незавершённых tool_use.
        self.db.append_history(
            conv.id,
            [
                {"role": "user", "content": user_content},
                {"role": "assistant", "content": [{"type": "text", "text": FAILURE_PLACEHOLDER}]},
            ],
        )
        self.db.update_conversation(conv.id, pending_notes=[])
        self.db.mark_processed(ids)
        if error.cost_usd:
            self.db.record_usage(conv.id, self.settings.claude_model, error.usage.get("input", 0),
                                 error.usage.get("output", 0), error.usage.get("cache_write", 0),
                                 error.usage.get("cache_read", 0), error.cost_usd)
        await self.alert(f"Claude API: {esc(error)} (диалог #{conv.id}). Клиенту отправлен шаблон.")
        if not already_escalated:
            await self.escalate(conv.id, f"Техническая ошибка бота: {error}", "high")
        # Шаблон уходит сразу, даже в режиме одобрения: клиент не должен ждать.
        await self.send_to_client(conv.id, self._with_disclosure(conv, language.pick(language.FAILURE_REPLY, lang), lang))

    # ------------------------------------------------------------------ эскалация и запись
    def _who(self, conv: Conversation) -> str:
        return f"@{esc(conv.username)}" if conv.username else f"IG {esc(conv.ig_user_id)}"

    def _transcript(self, conversation_id: int, limit: int = 8) -> str:
        label = {"client": "👤", "bot": "🤖", "manager": "🧑‍🎨"}
        lines = [f"{label.get(r['author'], '?')} {esc(self._row_text(r))[:400]}" for r in self.db.recent_messages(conversation_id, limit)]
        return "\n".join(lines)

    async def escalate(self, conversation_id: int, reason: str, urgency: str) -> None:
        conv = self.db.get_conversation(conversation_id)
        assert conv
        self.db.update_conversation(conversation_id, status="manager")
        day = _utc(self.clock()).strftime("%Y-%m-%d")
        self.db.kv_set(f"escalations:{day}", str(int(self.db.kv_get(f"escalations:{day}", "0") or 0) + 1))
        text = (
            f"🔔 <b>Нужен менеджер</b> ({URGENCY_LABEL.get(urgency, urgency)})\n"
            f"Клиент: {self._who(conv)}, диалог #{conv.id}\n"
            f"Причина: {esc(reason)}\n\n{self._transcript(conversation_id)}"
        )
        await self._tg_send(text, conversation_buttons(conversation_id))

    async def create_booking(self, conversation_id: int, data: dict[str, Any]) -> None:
        conv = self.db.get_conversation(conversation_id)
        assert conv
        refs = [a["url"] for r in self.db.all(
            "SELECT attachments FROM messages WHERE conversation_id = ? AND author = 'client'", (conversation_id,)
        ) for a in json.loads(r["attachments"] or "[]") if a.get("url")]
        self.db.execute(
            "INSERT INTO bookings(conversation_id, data, created_at) VALUES (?, ?, ?)",
            (conversation_id, json.dumps(data, ensure_ascii=False), self.clock()),
        )
        self.db.update_conversation(conversation_id, status="manager")
        fields = [
            ("Идея", "idea"), ("Размер", "size"), ("Место", "placement"), ("Стиль", "style"),
            ("Референсы", "references"), ("Даты", "preferred_dates"), ("Имя", "client_name"), ("Заметки", "notes"),
        ]
        body = "\n".join(f"<b>{label}:</b> {esc(data.get(key, 'unknown'))}" for label, key in fields)
        links = "\n".join(f'<a href="{esc(u)}">референс {i + 1}</a>' for i, u in enumerate(refs[-10:]))
        text = f"📝 <b>Заявка на запись</b>\nКлиент: {self._who(conv)}, диалог #{conv.id}\n\n{body}"
        if links:
            text += f"\n\n{links}"
        await self._tg_send(text, conversation_buttons(conversation_id, with_confirm=True))

    # ------------------------------------------------------------------ доставка
    async def deliver(self, conversation_id: int, text: str) -> None:
        if self.settings.approval_mode:
            conv = self.db.get_conversation(conversation_id)
            assert conv
            cur = self.db.execute(
                "INSERT INTO drafts(conversation_id, text, created_at) VALUES (?, ?, ?)",
                (conversation_id, text, self.clock()),
            )
            draft_id = int(cur.lastrowid)
            card = (
                f"📨 <b>Черновик ответа</b> для {self._who(conv)}, диалог #{conversation_id}\n\n"
                f"{self._transcript(conversation_id, 4)}\n\n<b>Бот предлагает:</b>\n{esc(text)}"
            )
            msg_id = await self._tg_send(card, draft_buttons(draft_id))
            self.db.execute("UPDATE drafts SET tg_message_id = ? WHERE id = ?", (msg_id, draft_id))
            self.db.update_conversation(conversation_id, needs_disclosure=False)
            return
        if await self.send_to_client(conversation_id, text):
            self.db.update_conversation(conversation_id, needs_disclosure=False)

    async def send_to_client(self, conversation_id: int, text: str) -> bool:
        conv = self.db.get_conversation(conversation_id)
        assert conv
        now = self.clock()
        window = self.settings.reply_window_hours * 3600
        if not conv.last_client_msg_at or now - conv.last_client_msg_at > window:
            await self.alert(f"Окно 24 ч для {self._who(conv)} закрыто, сообщение не отправлено. Бот не пишет первым.")
            return False
        try:
            for chunk in split_text(text):
                row_id = self.db.add_message(conversation_id, "bot", chunk, created_at=now, processed=True)
                mid = await self.ig.send_text(conv.ig_user_id, chunk)
                if mid:
                    self.db.execute("UPDATE messages SET mid = ? WHERE id = ? AND mid IS NULL", (mid, row_id))
        except Exception as e:
            log.exception("instagram send failed")
            await self.alert(f"Не удалось отправить сообщение {self._who(conv)} в Instagram: {esc(e)}")
            return False
        self.db.update_conversation(conversation_id, last_activity_at=now, needs_disclosure=False)
        return True

    async def _supersede_drafts(self, conversation_id: int, why: str) -> None:
        drafts = self.db.all("SELECT * FROM drafts WHERE conversation_id = ? AND status = 'pending'", (conversation_id,))
        for d in drafts:
            self.db.execute("UPDATE drafts SET status = 'superseded' WHERE id = ?", (d["id"],))
            self.db.add_note(conversation_id, f"Your previous reply was NOT sent to the client ({why}).")
            if d["tg_message_id"]:
                await self._safe(self.tg.edit_markup(d["tg_message_id"], None))

    # ------------------------------------------------------------------ Telegram
    async def handle_telegram(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self._on_callback(update["callback_query"])
        elif "message" in update:
            await self._on_tg_message(update["message"])

    def _from_our_chat(self, chat: dict[str, Any] | None) -> bool:
        return bool(chat) and str(chat.get("id")) == str(self.settings.telegram_chat_id)

    async def _on_callback(self, cq: dict[str, Any]) -> None:
        message = cq.get("message") or {}
        if not self._from_our_chat(message.get("chat")):
            return
        user = cq.get("from") or {}
        who = esc(user.get("username") or user.get("first_name") or "менеджер")
        action, _, raw_id = (cq.get("data") or "").partition(":")
        if not raw_id.isdigit():
            return
        obj_id = int(raw_id)
        answer = await self._run_action(action, obj_id, who)
        await self._safe(self.tg.answer_callback(cq.get("id", ""), answer))

    async def _run_action(self, action: str, obj_id: int, who: str) -> str:
        if action in {"take", "return", "confirm"}:
            conv = self.db.get_conversation(obj_id)
            if not conv:
                return "Диалог не найден"
            if action == "take":
                self.db.update_conversation(conv.id, status="manager")
                await self._tg_send(f"🙋 {who} взял диалог с {self._who(conv)}")
                return "Диалог ваш, бот молчит"
            if action == "return":
                self.db.update_conversation(conv.id, status="bot", needs_disclosure=True)
                self.db.add_note(conv.id, "The manager returned the conversation to you. Continue helping the client.")
                await self._tg_send(f"🤖 {who} вернул боту диалог с {self._who(conv)}")
                return "Бот снова отвечает (на следующее сообщение клиента)"
            booking = self.db.one(
                "SELECT id FROM bookings WHERE conversation_id = ? ORDER BY id DESC LIMIT 1", (conv.id,)
            )
            if not booking:
                return "Заявки нет"
            self.db.execute("UPDATE bookings SET status = 'confirmed' WHERE id = ?", (booking["id"],))
            await self._tg_send(f"✅ {who} подтвердил запись {self._who(conv)}. Календарь подключим на этапе 7.")
            return "Запись подтверждена"

        draft = self.db.one("SELECT * FROM drafts WHERE id = ?", (obj_id,))
        if not draft:
            return "Черновик не найден"
        if draft["status"] != "pending":
            return f"Черновик уже обработан ({draft['status']})"
        cid = draft["conversation_id"]
        if action == "send":
            ok = await self.send_to_client(cid, draft["text"])
            self.db.execute("UPDATE drafts SET status = ? WHERE id = ?", ("sent" if ok else "expired", obj_id))
            if not ok:
                self.db.add_note(cid, "Your previous reply could not be delivered to the client.")
            await self._safe(self.tg.edit_markup(draft["tg_message_id"], None))
            return "Отправлено" if ok else "Не отправлено, см. алерт"
        if action == "edit":
            prompt_id = await self._tg_send(
                f"✏️ Ответьте на это сообщение исправленным текстом для черновика #{obj_id}", force_reply=True
            )
            self.db.execute(
                "INSERT OR REPLACE INTO tg_pending_edits(tg_prompt_message_id, draft_id) VALUES (?, ?)", (prompt_id, obj_id)
            )
            return "Жду исправленный текст"
        if action == "grab":
            self.db.execute("UPDATE drafts SET status = 'discarded' WHERE id = ?", (obj_id,))
            self.db.update_conversation(cid, status="manager")
            self.db.add_note(cid, "Your previous reply was NOT sent: the manager took over the conversation.")
            await self._safe(self.tg.edit_markup(draft["tg_message_id"], None))
            return "Диалог ваш, черновик не отправлен"
        return "Неизвестное действие"

    async def _on_tg_message(self, message: dict[str, Any]) -> None:
        if not self._from_our_chat(message.get("chat")):
            return
        reply_to = message.get("reply_to_message") or {}
        pending = self.db.one(
            "SELECT draft_id FROM tg_pending_edits WHERE tg_prompt_message_id = ?", (reply_to.get("message_id"),)
        )
        text = (message.get("text") or "").strip()
        if not pending or not text:
            return
        draft = self.db.one("SELECT * FROM drafts WHERE id = ?", (pending["draft_id"],))
        self.db.execute("DELETE FROM tg_pending_edits WHERE tg_prompt_message_id = ?", (reply_to.get("message_id"),))
        if not draft or draft["status"] != "pending":
            await self._tg_send("Этот черновик уже неактуален, исправление не отправлено.")
            return
        cid = draft["conversation_id"]
        ok = await self.send_to_client(cid, text)
        self.db.execute("UPDATE drafts SET status = ? WHERE id = ?", ("edited" if ok else "expired", draft["id"]))
        if ok:
            self.db.add_note(cid, f"The manager edited your previous reply before sending. The client actually received: «{text}»")
        await self._safe(self.tg.edit_markup(draft["tg_message_id"], None))
        await self._tg_send("✅ Исправленный ответ отправлен" if ok else "❌ Не отправлено, см. алерт")

    # ------------------------------------------------------------------ расход и алерты
    async def _record_usage(self, conversation_id: int, result: AgentResult) -> None:
        u = result.usage
        self.db.record_usage(conversation_id, result.model, u.get("input", 0), u.get("output", 0),
                             u.get("cache_write", 0), u.get("cache_read", 0), result.cost_usd)
        await self.check_budget()

    def _month_start(self) -> float:
        now = _utc(self.clock())
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()

    async def check_budget(self) -> None:
        budget = self.settings.monthly_budget_usd
        if budget <= 0:
            return
        spend = self.db.spend_since(self._month_start())
        month = _utc(self.clock()).strftime("%Y-%m")
        for threshold in (80, 50):
            if spend >= budget * threshold / 100:
                key = f"budget_alert:{month}:{threshold}"
                if not self.db.kv_get(key):
                    self.db.kv_set(key, "1")
                    await self.alert(f"Расход Claude API за месяц ${spend:.2f}, это {threshold}%+ бюджета ${budget:.0f}.")
                break

    async def alert(self, text: str) -> None:
        await self._tg_send(f"⚠️ {text}")

    async def _tg_send(self, text: str, markup: dict | None = None, force_reply: bool = False) -> int:
        try:
            return await self.tg.send(text, markup, force_reply=force_reply)
        except Exception:
            log.exception("telegram send failed")
            return 0

    async def _safe(self, coro) -> None:
        try:
            await coro
        except Exception:
            log.exception("telegram call failed")

    # ------------------------------------------------------------------ по расписанию
    async def daily_summary(self) -> None:
        now = self.clock()
        day_start = _utc(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        day = _utc(now).strftime("%Y-%m-%d")
        counts = {r["author"]: r["n"] for r in self.db.all(
            "SELECT author, COUNT(*) AS n FROM messages WHERE created_at >= ? GROUP BY author", (day_start,)
        )}
        dialogs = self.db.one(
            "SELECT COUNT(DISTINCT conversation_id) AS n FROM messages WHERE created_at >= ? AND author = 'client'",
            (day_start,),
        )["n"]
        bookings = self.db.one("SELECT COUNT(*) AS n FROM bookings WHERE created_at >= ?", (day_start,))["n"]
        spend_day = self.db.spend_since(day_start)
        spend_month = self.db.spend_since(self._month_start())
        budget = self.settings.monthly_budget_usd
        pct = f" ({spend_month / budget * 100:.0f}% бюджета)" if budget > 0 else ""
        await self._tg_send(
            f"📊 <b>Сводка за {day}</b>\n"
            f"Диалогов: {dialogs}\n"
            f"Сообщений: клиентов {counts.get('client', 0)}, бота {counts.get('bot', 0)}, менеджера {counts.get('manager', 0)}\n"
            f"Эскалаций: {self.db.kv_get(f'escalations:{day}', '0')}, заявок на запись: {bookings}\n"
            f"Расход сегодня: ${spend_day:.2f}\n"
            f"Расход за месяц: ${spend_month:.2f}{pct}"
        )

    async def check_token(self) -> None:
        """Продлевает долгий токен Instagram и напоминает, если срок подходит к концу."""
        now = self.clock()
        expires_at = float(self.db.kv_get("ig_token_expires_at", "0") or 0)
        last_try = float(self.db.kv_get("ig_token_last_refresh_try", "0") or 0)
        due = not expires_at or expires_at - now < self.settings.token_refresh_days * 86400
        if due and now - last_try > 86400:
            self.db.kv_set("ig_token_last_refresh_try", str(now))
            try:
                token, expires_in = await self.ig.refresh_token()
                self.db.kv_set("ig_token", token)
                self.db.kv_set("ig_token_expires_at", str(now + expires_in))
                expires_at = now + expires_in
                log.info("instagram token refreshed")
            except Exception as e:
                log.exception("token refresh failed")
                await self.alert(f"Не удалось обновить токен Instagram: {esc(e)}")
        if expires_at and expires_at - now < self.settings.token_reminder_days * 86400:
            day = _utc(now).strftime("%Y-%m-%d")
            if self.db.kv_get("ig_token_reminder_day") != day:
                self.db.kv_set("ig_token_reminder_day", day)
                left = max(0, int((expires_at - now) // 86400))
                await self.alert(f"Токен Instagram истекает через {left} дн. Обновите IG_ACCESS_TOKEN.")

    def purge(self) -> None:
        """Хранение 90 дней: старые диалоги удаляются целиком вместе с историей для модели."""
        cutoff = self.clock() - self.settings.retention_days * 86400
        self.db.execute("DELETE FROM conversations WHERE COALESCE(last_activity_at, created_at) < ?", (cutoff,))
        self.db.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
        self.db.execute("DELETE FROM processed_events WHERE created_at < ?", (self.clock() - 7 * 86400,))

    async def tick(self) -> None:
        """Вызывается раз в минуту планировщиком."""
        now = _utc(self.clock())
        day = now.strftime("%Y-%m-%d")
        if now.hour >= self.settings.summary_hour_utc and self.db.kv_get("summary_day") != day:
            self.db.kv_set("summary_day", day)
            await self.daily_summary()
        if self.db.kv_get("maintenance_day") != day:
            self.db.kv_set("maintenance_day", day)
            self.purge()
            await self.check_token()
