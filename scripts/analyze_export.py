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
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.classifier import TopicClassifier  # noqa: E402
from app import history_analysis as core  # noqa: E402
from app.history_analysis import (  # noqa: E402,F401  (реэкспорт для тестов и обратной совместимости)
    CHUNK_CHARS, Message, Thread, anonymize, chunks, estimate, transcript,
)

# ---------------------------------------------------------------- разбор архива
def fix_text(s: str) -> str:
    """Instagram пишет UTF-8 как latin-1 escape: 'Ð¿Ñ€Ð¸' вместо 'при'."""
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


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


async def analyze(threads: list[Thread], owner: str, out: Path, client: Any, classifier: TopicClassifier) -> dict[str, Any]:
    """Анализ и запись черновиков в папку out."""
    out.mkdir(parents=True, exist_ok=True)
    result = await core.analyze_threads(threads, owner, client, classifier, progress=lambda m: print(f"  {m}", flush=True))
    (out / "personal_contacts.txt").write_text(
        "# Предполагаемые ники собеседников, чьи диалоги не про тату. Проверьте список вручную,\n"
        "# затем отправьте нужные в Telegram-группу командой /personal @ник\n"
        + "\n".join(f"@{u}" for u in result["personal_contacts"]) + "\n",
        encoding="utf-8",
    )
    for name, key in (("knowledge_base_draft.md", "knowledge_base"), ("style_guide.md", "style_guide"),
                      ("test_cases.md", "test_cases"), ("batch_notes.md", "notes")):
        (out / name).write_text(result.get(key, ""), encoding="utf-8")
    return result


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
