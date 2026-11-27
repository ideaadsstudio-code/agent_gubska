"""Ядро анализа переписки мастера: обезличивание, отбор тату-диалогов, черновики базы знаний.

Используется и скриптом scripts/analyze_export.py (ZIP-выгрузка Instagram), и сервером
(переписка через API, страница /admin/analysis). Результат возвращается, а не пишется на диск:
сырая переписка нигде не сохраняется.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .agent import FALLBACK_BETA
from .classifier import TopicClassifier
from .pricing import cost_usd

ANALYSIS_MODEL = "claude-opus-5-5"
CHUNK_CHARS = 300_000  # примерно 100k токенов на пачку диалогов

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
URL = re.compile(r"(https?://\S+|www\.\S+)")
PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
HANDLE = re.compile(r"(?<!\w)@[\w.]{2,}")


@dataclass
class Message:
    sender: str
    text: str
    ts: float
    photo: bool = False


@dataclass
class Thread:
    path: str
    participants: list[str]
    messages: list[Message] = field(default_factory=list)

    @property
    def username_guess(self) -> str:
        # Папка диалога обычно называется <ник>_<число>.
        name = self.path.rstrip("/").split("/")[-1]
        return name.rsplit("_", 1)[0] if "_" in name else name


# ---------------------------------------------------------------- обезличивание
# Обращение по имени после приветствия или благодарности: «Привет, Катя!», «Спасибо, Оля», «Hi Kate».
GREETING_NAME = re.compile(
    r"(?i:\b(привет(ик)?|здравствуй(те)?|добр(ый|ое|ого)\s+(день|вечер|утро|утра|дня|вечера)|"
    r"привіт|вітаю|добрий\s+(день|вечір)|спасибо|благодарю|дякую|hi|hello|hey|dear|thanks|thank\s+you|hola|gracias))"
    r"(\s*,\s*|\s+)([A-ZА-ЯЁІЇЄҐ][a-zа-яёіїєґ'’-]{1,20})"
)


def anonymize(text: str, names: list[str]) -> str:
    text = EMAIL.sub("[email]", text)
    text = URL.sub("[link]", text)
    text = PHONE.sub("[phone]", text)
    text = HANDLE.sub("[username]", text)
    for name in names:
        # Полное имя, отдельные слова и части ника: «kate.ink_92» → kate, ink.
        for part in {name, *name.split(), *re.split(r"[\s._\d-]+", name)}:
            if len(part) >= 3:
                text = re.sub(rf"(?<!\w){re.escape(part)}(?!\w)", "[name]", text, flags=re.IGNORECASE)
    return GREETING_NAME.sub(lambda m: m.group(0)[: m.start(m.lastindex) - m.start(0)] + "[name]", text)


def transcript(thread: Thread, owner: str, limit: int | None = None) -> str:
    others = [p for p in thread.participants if p != owner]
    lines = []
    for m in thread.messages[:limit]:
        who = "Artist" if m.sender == owner else "Client"
        body = anonymize(m.text, others + [owner]) if m.text else ""
        if m.photo:
            body = f"{body} [photo]".strip()
        if body:
            day = datetime.fromtimestamp(m.ts, tz=timezone.utc).strftime("%Y-%m-%d")
            lines.append(f"[{day}] {who}: {body}")
    return "\n".join(lines)


# ---------------------------------------------------------------- анализ
CHUNK_PROMPT = """Below are anonymized Instagram Direct conversations between a tattoo artist ("Artist") and clients \
("Client"). Names, usernames, phones, emails and links were removed. Study them and write notes in Russian, \
in these sections:

1. Цены: every price or price range the artist actually quoted, with what it was for (size, style, placement) and the date.
2. Условия: deposit, booking, rescheduling, cancellation, payment methods, working hours, location hints, \
session length, anything the artist states as a rule.
3. Частые вопросы: the questions clients ask most, with how the artist answered (paraphrase).
4. Стиль мастера: tone, length of messages, greetings, sign-offs, emoji, how formal, which language with whom, \
5–10 characteristic phrases (quote them, they contain no personal data).
5. Что работает: patterns in conversations that ended in a booking vs. ones where the client went silent.
6. Неясное: contradictions (for example two different prices for similar work) and gaps.

Report only what the conversations show. Do not invent prices or rules. Do not include any personal details."""

SYNTHESIS_PROMPT = """You get notes from several batches of a tattoo artist's Instagram conversations. Merge them \
into three documents in Russian for the studio owner to review. Where batches disagree, keep the most recent \
information and mark the conflict with "ПРОВЕРИТЬ:". Do not invent anything; leave "TODO" where information is missing.

- knowledge_base: a draft of the studio knowledge base in this structure: Studio, Styles, Prices (ranges only), \
Booking and deposit, Before the session, Aftercare (general, non-medical), FAQ, Examples of the artist's real replies \
(5–10 short anonymized examples). The assistant bot will quote prices only from here, so ranges must reflect what \
the artist really quoted.
- style_guide: how the bot should write to sound like the artist: tone, length, greetings, emoji, phrases to use \
and to avoid.
- test_cases: 20–40 realistic client messages (paraphrased, no personal data) covering the common situations, each \
with a one-line note on what a good answer must contain. These become the bot's test set."""

SYNTH_SCHEMA = {
    "type": "object",
    "properties": {
        "knowledge_base": {"type": "string"},
        "style_guide": {"type": "string"},
        "test_cases": {"type": "string"},
    },
    "required": ["knowledge_base", "style_guide", "test_cases"],
    "additionalProperties": False,
}


async def call_model(client: Any, **params: Any) -> Any:
    """Стриминг: длинный ответ не упирается в таймаут HTTP."""
    async with client.beta.messages.stream(**params) as stream:
        return await stream.get_final_message()


def _cost(resp: Any) -> float:
    u = resp.usage
    return cost_usd(resp.model, u.input_tokens or 0, u.output_tokens or 0,
                    getattr(u, "cache_creation_input_tokens", 0) or 0, getattr(u, "cache_read_input_tokens", 0) or 0)


def _text(resp: Any) -> str:
    if resp.stop_reason == "refusal":
        raise RuntimeError("Модель отказалась обрабатывать пачку диалогов")
    return "\n".join(b.text for b in resp.content if b.type == "text").strip()


def chunks(texts: list[str], limit: int = CHUNK_CHARS) -> list[str]:
    """Пачки диалогов до limit символов. Очень длинный диалог делится на части, а не обрезается."""
    pieces = []
    for t in texts:
        lines, part = t.splitlines(), []
        for line in lines:
            if part and sum(len(x) + 1 for x in part) + len(line) > limit - 100:
                pieces.append("\n".join(part))
                part = ["(continued)"]
            part.append(line[: limit - 200])
        pieces.append("\n".join(part))
    out, current = [], ""
    for p in pieces:
        piece = f"<conversation>\n{p}\n</conversation>\n"
        if current and len(current) + len(piece) > limit:
            out.append(current)
            current = ""
        current += piece
    if current:
        out.append(current)
    return out


async def analyze_threads(
    threads: list[Thread],
    owner: str,
    client: Any,
    classifier: TopicClassifier,
    progress: Callable[[str], None] = lambda _msg: None,
) -> dict[str, Any]:
    """Возвращает черновики (knowledge_base, style_guide, test_cases), список личных контактов и расход."""
    spent = 0.0
    tattoo, personal = [], []
    for i, t in enumerate(threads, 1):
        if not any(m.sender != owner for m in t.messages):
            continue
        verdict = await classifier.classify(transcript(t, owner, limit=12).splitlines())
        spent += verdict.cost_usd
        (tattoo if verdict.topic == "tattoo" else personal).append(t)
        progress(f"Классификация диалогов: {i}/{len(threads)}")

    result: dict[str, Any] = {
        "tattoo": len(tattoo),
        "personal": len(personal),
        "personal_contacts": sorted({t.username_guess for t in personal}),
        "batches": 0,
        "cost_usd": spent,
    }
    transcripts = [transcript(t, owner) for t in tattoo]
    transcripts = [t for t in transcripts if t]
    if not transcripts:
        result.update(knowledge_base="", style_guide="", test_cases="", notes="Тату-диалогов не найдено.")
        return result

    notes = []
    batches = chunks(transcripts)
    for i, batch in enumerate(batches, 1):
        progress(f"Анализ пачки диалогов {i}/{len(batches)}")
        resp = await call_model(
            client, model=ANALYSIS_MODEL, max_tokens=32000,
            system=CHUNK_PROMPT, messages=[{"role": "user", "content": batch}],
            output_config={"effort": "high"},
            betas=[FALLBACK_BETA], fallbacks="default",
        )
        spent += _cost(resp)
        notes.append(_text(resp))

    progress("Сводка и черновики")
    resp = await call_model(
        client, model=ANALYSIS_MODEL, max_tokens=64000,
        system=SYNTHESIS_PROMPT,
        messages=[{"role": "user", "content": "\n\n".join(f"<batch_notes>\n{n}\n</batch_notes>" for n in notes)}],
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": SYNTH_SCHEMA}},
        betas=[FALLBACK_BETA], fallbacks="default",
    )
    spent += _cost(resp)
    docs = json.loads(_text(resp))
    result.update(docs, batches=len(batches), cost_usd=spent, notes="\n\n---\n\n".join(notes))
    return result


def estimate(threads: list[Thread], owner: str) -> tuple[int, float]:
    chars = sum(len(transcript(t, owner)) for t in threads)
    tokens = chars // 3
    # Худший случай: все диалоги про тату; классификация + чтение + заметки и сводка.
    usd = len(threads) * 0.0005 + tokens * 4 / 1e6 + (chars // CHUNK_CHARS + 2) * 12000 * 20 / 1e6
    return tokens, usd
