"""Обучение на ответах человека: бот смотрит, как отвечает мастер или менеджер, и предлагает правила.

1. Сбор примеров (только в диалогах про тату, где переписка и так хранится):
   - edited:   черновик бота исправили кнопкой «Исправить»;
   - replaced: был черновик, но человек ответил клиенту сам из Instagram;
   - manual:   человек ведёт диалог сам (после «Беру диалог», эскалации или вместо бота).
2. Раз в неделю или по команде /learn модель (ANALYSIS_MODEL) читает обезличенные примеры и текущую базу
   знаний и предлагает правила. Каждое приходит в Telegram с кнопками «Принять» / «Отклонить».
3. Принятые правила дописываются к системному промпту. Так бот начинает их применять, а человек
   решает, что из разовых ответов стоит превращать в правило.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from .agent import FALLBACK_BETA
from .history_analysis import _cost, _text, anonymize, call_model
from .telegram import esc, kb

if TYPE_CHECKING:
    from .service import Service

log = logging.getLogger(__name__)

MIN_EXAMPLES = 5  # меньше примеров за неделю: ждём следующей
MAX_EXAMPLES = 80
KINDS = ("fact", "correction", "faq", "style")
KIND_LABEL = {"fact": "Факт", "correction": "Исправление", "faq": "Ответ на частый вопрос", "style": "Стиль"}

PROMPT = """You help a tattoo studio's Instagram assistant learn from how the artist or manager actually replies.
You get the assistant's current knowledge base and rules, and anonymized examples: the recent conversation,
what the assistant drafted (if anything), and what the human actually sent.

Propose at most 8 rules the assistant should follow from now on. Only propose a rule when it is:
- general: it would apply to many future clients, not a one-off (never specific free dates, a discount for one person,
  a promise made to one client, or anything about a particular client);
- new or a correction: not already in the knowledge base, or the human clearly contradicts it;
- safe: no medical advice, no confirming bookings, no payment or card details, no personal data.
A price may be proposed only when the human stated it as a general price; then say so in "why" so the studio can confirm.

Each rule: "kind" is fact (a studio fact or policy), correction (the knowledge base or the assistant was wrong),
faq (how to answer a recurring question), or style (how to phrase replies). "rule" is one or two sentences in English,
written as an instruction or fact for the assistant. "why" is one short sentence in Russian for the studio manager,
explaining what in the examples led to it. Return an empty list if nothing qualifies."""

SCHEMA = {
    "type": "object",
    "properties": {
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "rule": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["kind", "rule", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["rules"],
    "additionalProperties": False,
}


class Learner:
    def __init__(self, service: "Service", base_prompt: str):
        self.service = service
        self.db = service.db
        self.base_prompt = base_prompt
        self.running = False

    # ------------------------------------------------------------------ сбор примеров
    def record(self, conversation_id: int, kind: str, human_reply: str, bot_draft: str | None = None) -> None:
        if not human_reply.strip():
            return
        context = [
            f"{'Client' if r['author'] == 'client' else 'Studio'}: {r['text']}"
            for r in self.db.recent_messages(conversation_id, 8)
            if r["text"] and r["text"] != human_reply
        ][-6:]
        self.db.execute(
            "INSERT INTO learning_examples(conversation_id, kind, context, bot_draft, human_reply, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (conversation_id, kind, json.dumps(context, ensure_ascii=False), bot_draft, human_reply, time.time()),
        )
        log.info("dialog #%s: learning example recorded (%s)", conversation_id, kind)

    # ------------------------------------------------------------------ системный промпт
    def accepted_rules(self) -> list:
        return self.db.all("SELECT * FROM learned_rules WHERE status = 'accepted' ORDER BY id")

    def system_prompt(self) -> str:
        rules = self.accepted_rules()
        if not rules:
            return self.base_prompt
        lines = "\n".join(f"- {r['rule']}" for r in rules)
        return (f"{self.base_prompt}\n\n## Learned from the studio's own replies (approved by the studio)\n"
                f"These rules come from how the artist actually answers. If one contradicts the knowledge base "
                f"above, follow the rule.\n{lines}")

    def apply(self) -> None:
        """Пересобирает системный промпт. Это сбрасывает кэш один раз; устаревшие блоки thinking
        API отбрасывает сам (drop_block), поэтому правила меняем редко и пачкой."""
        self.service.agent.set_system(self.system_prompt())

    # ------------------------------------------------------------------ анализ
    async def run(self, force: bool = False) -> int:
        """Возвращает число новых предложений."""
        if self.running:
            return 0
        self.running = True
        try:
            rows = self.db.all(
                "SELECT * FROM learning_examples WHERE used = 0 ORDER BY id DESC LIMIT ?", (MAX_EXAMPLES,)
            )
            if len(rows) < (1 if force else MIN_EXAMPLES):
                if force:
                    await self.service._tg_send("🧠 Пока нет новых ответов менеджера, на которых можно учиться.")
                return 0
            examples = []
            for r in reversed(rows):
                scrub = lambda t: anonymize(t or "", [])  # noqa: E731
                examples.append({
                    "conversation": [scrub(line) for line in json.loads(r["context"] or "[]")],
                    "assistant_draft": scrub(r["bot_draft"]) or None,
                    "human_sent": scrub(r["human_reply"]),
                    "situation": r["kind"],
                })
            current = self.system_prompt()
            kb_start = current.find("# Knowledge base")
            knowledge = current[kb_start:] if kb_start >= 0 else current
            model = self.service.settings.analysis_model
            resp = await call_model(
                self.service.agent.client, model=model, max_tokens=16000,
                system=PROMPT,
                messages=[{"role": "user", "content":
                           f"<knowledge_base>\n{knowledge}\n</knowledge_base>\n\n<examples>\n"
                           f"{json.dumps(examples, ensure_ascii=False, indent=1)}\n</examples>"}],
                output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
                betas=[FALLBACK_BETA], fallbacks="default",
            )
            cost = _cost(resp)
            self.db.record_usage(None, f"{model} (learning)", 0, 0, 0, 0, cost)
            proposals = json.loads(_text(resp)).get("rules", [])[:8]
            ids = [r["id"] for r in rows]
            self.db.execute(f"UPDATE learning_examples SET used = 1 WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))
            if not proposals:
                await self.service._tg_send(f"🧠 Разобрал {len(rows)} ответов менеджера: новых правил нет.")
                return 0
            await self.service._tg_send(
                f"🧠 Разобрал {len(rows)} ответов менеджера и предлагаю {len(proposals)} правил. "
                "Принятые бот начнёт применять сразу."
            )
            for p in proposals:
                cur = self.db.execute(
                    "INSERT INTO learned_rules(kind, rule, why, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
                    (p["kind"], p["rule"].strip(), p["why"].strip(), time.time()),
                )
                rid = int(cur.lastrowid)
                await self.service._tg_send(
                    f"<b>{KIND_LABEL.get(p['kind'], p['kind'])}</b>\n{esc(p['rule'])}\n\n<i>{esc(p['why'])}</i>",
                    kb([("Принять", f"learn_ok:{rid}"), ("Отклонить", f"learn_no:{rid}")]),
                )
            return len(proposals)
        except Exception as e:
            log.exception("learning failed")
            await self.service.alert(f"Обучение на ответах менеджера не удалось: {esc(e)}")
            return 0
        finally:
            self.running = False

    # ------------------------------------------------------------------ решения менеджера
    def decide(self, rule_id: int, accept: bool) -> str:
        row = self.db.one("SELECT * FROM learned_rules WHERE id = ?", (rule_id,))
        if not row:
            return "Правило не найдено"
        if row["status"] != "pending":
            return f"Уже решено: {row['status']}"
        self.db.execute("UPDATE learned_rules SET status = ? WHERE id = ?", ("accepted" if accept else "rejected", rule_id))
        if accept:
            self.apply()
            return "Принято, бот уже применяет"
        return "Отклонено"

    def remove(self, rule_id: int) -> bool:
        row = self.db.one("SELECT status FROM learned_rules WHERE id = ?", (rule_id,))
        if not row or row["status"] != "accepted":
            return False
        self.db.execute("UPDATE learned_rules SET status = 'removed' WHERE id = ?", (rule_id,))
        self.apply()
        return True

    def rules_listing(self) -> str:
        rules = self.accepted_rules()
        if not rules:
            return "🧠 Выученных правил пока нет."
        lines = [f"#{r['id']} · {esc(r['rule'])}" for r in rules]
        return "🧠 <b>Выученные правила</b> (удалить: /unrule номер)\n\n" + "\n\n".join(lines)

    async def maybe_weekly(self, now: float) -> None:
        week = time.strftime("%G-W%V", time.gmtime(now))
        if self.db.kv_get("learning_week") == week:
            return
        self.db.kv_set("learning_week", week)
        await self.run()

