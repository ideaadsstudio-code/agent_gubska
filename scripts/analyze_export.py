"""Анализ выгрузки переписки Instagram: черновик базы знаний, стиль мастера, тестовые вопросы.

Запуск:
    python scripts/analyze_export.py путь/к/выгрузке.zip --dry-run   # только разбор и статистика, без API
    python scripts/analyze_export.py путь/к/выгрузке.zip --yes       # полный анализ (тратит API)

Что происходит:
1. Из архива читаются диалоги (messages/inbox и message_requests), чинится кодировка Instagram.
2. Из текста удаляются имена, ники, телефоны, почты и ссылки. Мастер становится "Artist", собеседник "Client".
3. Haiku 5.5 делит диалоги на тату-запросы и остальное. Личные диалоги дальше не анализируются,
   из них в результат попадает только предполагаемый ник для списка /personal.
4. Opus 5.5 читает обезличенные тату-диалоги и пишет черновики.

Всё сохраняется в --out (по умолчанию analysis_out/, он в .gitignore). Сам архив не копируется.
Удалите архив и папку результата, когда перенесёте нужное в knowledge/knowledge_base.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import FALLBACK_BETA  # noqa: E402
from app.classifier import TopicClassifier  # noqa: E402
from app.pricing import cost_usd  # noqa: E402

ANALYSIS_MODEL = "claude-opus-5-5"
CHUNK_CHARS = 300_000  # примерно 100k токенов на пачку диалогов

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
URL = re.compile(r"(https?://\S+|www\.\S+)")
PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
HANDLE = re.compile(r"(?<!\w)@[\w.]{2,}")


# ---------------------------------------------------------------- разбор архива
def fix_text(s: str) -> str:
    """Instagram пишет UTF-8 как latin-1 escape: 'Ð¿Ñ€Ð¸' вместо 'при'."""
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


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


def read_export(source: Path) -> list[Thread]:
    files: dict[str, list[dict[str, Any]]] = {}

    def add(name: str, raw: bytes) -> None:
        if not re.search(r"messages/(inbox|message_requests)/[^/]+/message_\d+\.json$", name):
            return
        files.setdefault(name.rsplit("/", 1)[0], []).append(json.loads(raw))

    if source.is_dir():
        for p in source.rglob("message_*.json"):
            add(p.as_posix(), p.read_bytes())
    else:
        with zipfile.ZipFile(source) as z:
            for name in z.namelist():
                if name.endswith(".json"):
                    add(name, z.read(name))

    threads = []
    for path, parts in files.items():
        participants = sorted({fix_text(p["name"]) for part in parts for p in part.get("participants", [])})
        msgs = []
        for part in parts:
            for m in part.get("messages", []):
                text = fix_text(m.get("content", "") or "")
                if m.get("share", {}).get("link") and not text:
                    text = "[shared post]"
                msgs.append(Message(fix_text(m.get("sender_name", "")), text, m.get("timestamp_ms", 0) / 1000,
                                    photo=bool(m.get("photos"))))
        msgs.sort(key=lambda m: m.ts)
        threads.append(Thread(path, participants, msgs))
    return threads


def find_owner(threads: list[Thread]) -> str:
    counts = Counter(p for t in threads for p in t.participants)
    if not counts:
        raise SystemExit("В архиве не найдено диалогов. Проверьте, что выгружены «Сообщения» в формате JSON.")
    return counts.most_common(1)[0][0]


# ---------------------------------------------------------------- обезличивание
def anonymize(text: str, names: list[str]) -> str:
    text = EMAIL.sub("[email]", text)
    text = URL.sub("[link]", text)
    text = PHONE.sub("[phone]", text)
    text = HANDLE.sub("[username]", text)
    for name in names:
        for part in {name, *name.split()}:
            if len(part) >= 3:
                text = re.sub(rf"(?<!\w){re.escape(part)}(?!\w)", "[name]", text, flags=re.IGNORECASE)
    return text


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


async def analyze(threads: list[Thread], owner: str, out: Path, client: Any, classifier: TopicClassifier) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    spent = 0.0
    tattoo, personal = [], []
    for i, t in enumerate(threads, 1):
        if not any(m.sender != owner for m in t.messages):
            continue
        verdict = await classifier.classify(transcript(t, owner, limit=12).splitlines())
        spent += verdict.cost_usd
        (tattoo if verdict.topic == "tattoo" else personal).append(t)
        print(f"\r  классификация {i}/{len(threads)}", end="", flush=True)
    print()

    (out / "personal_contacts.txt").write_text(
        "# Предполагаемые ники собеседников, чьи диалоги не про тату. Проверьте список вручную,\n"
        "# затем отправьте нужные в Telegram-группу командой /personal @ник\n"
        + "\n".join(sorted(f"@{t.username_guess}" for t in personal)) + "\n",
        encoding="utf-8",
    )
    transcripts = [transcript(t, owner) for t in tattoo]
    (out / "tattoo_conversations_anonymized.txt").write_text("\n\n---\n\n".join(transcripts), encoding="utf-8")

    notes = []
    batches = chunks(transcripts)
    for i, batch in enumerate(batches, 1):
        print(f"  анализ пачки {i}/{len(batches)}…", flush=True)
        resp = await call_model(
            client, model=ANALYSIS_MODEL, max_tokens=32000,
            system=CHUNK_PROMPT, messages=[{"role": "user", "content": batch}],
            output_config={"effort": "high"},
            betas=[FALLBACK_BETA], fallbacks="default",
        )
        spent += _cost(resp)
        notes.append(_text(resp))
    (out / "batch_notes.md").write_text("\n\n---\n\n".join(notes), encoding="utf-8")

    print("  сводка…", flush=True)
    resp = await call_model(
        client, model=ANALYSIS_MODEL, max_tokens=64000,
        system=SYNTHESIS_PROMPT,
        messages=[{"role": "user", "content": "\n\n".join(f"<batch_notes>\n{n}\n</batch_notes>" for n in notes)}],
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": SYNTH_SCHEMA}},
        betas=[FALLBACK_BETA], fallbacks="default",
    )
    spent += _cost(resp)
    docs = json.loads(_text(resp))
    (out / "knowledge_base_draft.md").write_text(docs["knowledge_base"], encoding="utf-8")
    (out / "style_guide.md").write_text(docs["style_guide"], encoding="utf-8")
    (out / "test_cases.md").write_text(docs["test_cases"], encoding="utf-8")
    return {"tattoo": len(tattoo), "personal": len(personal), "batches": len(batches), "cost_usd": spent}


def estimate(threads: list[Thread], owner: str) -> tuple[int, float]:
    chars = sum(len(transcript(t, owner)) for t in threads)
    tokens = chars // 3
    # Худший случай: все диалоги про тату; классификация + чтение + заметки и сводка.
    usd = len(threads) * 0.0005 + tokens * 4 / 1e6 + (chars // CHUNK_CHARS + 2) * 12000 * 20 / 1e6
    return tokens, usd


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="ZIP-архив выгрузки или распакованная папка")
    parser.add_argument("--out", type=Path, default=Path("analysis_out"))
    parser.add_argument("--owner", help="Имя мастера в выгрузке, если определилось неверно")
    parser.add_argument("--dry-run", action="store_true", help="только разбор и статистика, без вызовов API")
    parser.add_argument("--yes", action="store_true", help="подтвердить расход API")
    args = parser.parse_args()

    threads = read_export(args.source)
    owner = args.owner or find_owner(threads)
    msgs = sum(len(t.messages) for t in threads)
    tokens, usd = estimate(threads, owner)
    print(f"Диалогов: {len(threads)}, сообщений: {msgs}. Мастер в выгрузке: «{owner}».")
    print(f"Объём ~{tokens:,} токенов. Оценка стоимости анализа: до ${usd:.2f}.")
    if args.dry_run:
        sample = next((t for t in threads if len(t.messages) > 3), None)
        if sample:
            print("\nПример обезличенного диалога (первые строки):\n" + "\n".join(transcript(sample, owner).splitlines()[:8]))
        return
    if not args.yes:
        print("Чтобы запустить анализ, добавьте --yes.")
        return

    import anthropic

    client = anthropic.AsyncAnthropic()
    result = asyncio.run(analyze(threads, owner, args.out, client, TopicClassifier(client)))
    print(f"\nГотово: тату-диалогов {result['tattoo']}, прочих {result['personal']}, "
          f"потрачено ${result['cost_usd']:.2f}. Результат в {args.out}/")


if __name__ == "__main__":
    main()
