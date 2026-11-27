"""Скрипт анализа выгрузки: разбор, кодировка, обезличивание, пайплайн с фейковым Claude."""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import analyze_export as ax  # noqa: E402
from app.classifier import TopicClassifier  # noqa: E402
from conftest import FakeClaude, msg, text, topic  # noqa: E402

OWNER = "Anna Gubska"


def mojibake(s: str) -> str:
    return s.encode("utf-8").decode("latin-1")


def thread_json(other: str, messages: list[tuple[str, str]]) -> bytes:
    return json.dumps({
        "participants": [{"name": mojibake(other)}, {"name": OWNER}],
        # Instagram кладёт новые сообщения первыми.
        "messages": list(reversed([
            {"sender_name": mojibake(sender), "timestamp_ms": 1_700_000_000_000 + i * 60_000, "content": mojibake(body)}
            for i, (sender, body) in enumerate(messages)
        ])),
    }).encode()


def make_export(tmp_path: Path) -> Path:
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("your_instagram_activity/messages/inbox/kate.ink_123/message_1.json", thread_json("Катя Смирнова", [
            ("Катя Смирнова", "Привет! Сколько стоит тату 10 см? Мой номер +380 67 123 45 67"),
            (OWNER, "Привет, Катя! Fine line 10 см от 3000 грн, пиши на anna@example.com"),
        ]))
        z.writestr("your_instagram_activity/messages/inbox/bestie_99/message_1.json", thread_json("Оля", [
            ("Оля", "Ань, как мама?"), (OWNER, "Лучше, спасибо!"),
        ]))
        z.writestr("your_instagram_activity/messages/message_requests/brand_7/message_1.json", thread_json("Brand", [
            ("Brand", "Collab offer: https://brand.example/deal @brand"),
        ]))
        z.writestr("your_instagram_activity/media/photo.jpg", b"x")
    return path


def test_read_and_fix_encoding(tmp_path):
    threads = ax.read_export(make_export(tmp_path))
    assert len(threads) == 3
    assert ax.find_owner(threads) == OWNER
    kate = next(t for t in threads if "kate" in t.path)
    assert kate.username_guess == "kate.ink"
    assert kate.messages[0].text.startswith("Привет! Сколько стоит")


def test_anonymize_removes_personal_data(tmp_path):
    threads = ax.read_export(make_export(tmp_path))
    kate = next(t for t in threads if "kate" in t.path)
    t = ax.transcript(kate, OWNER)
    for secret in ("Катя", "Смирнова", "+380", "anna@example.com", "Anna"):
        assert secret not in t
    assert "Client: Привет! Сколько стоит тату 10 см?" in t
    assert "Artist: Привет, [name]! Fine line 10 см от 3000 грн" in t
    brand = next(t for t in threads if "brand" in t.path)
    bt = ax.transcript(brand, OWNER)
    assert "[link]" in bt and "[username]" in bt and "brand.example" not in bt


def test_chunks_split_long_thread_without_loss():
    long = "\n".join(f"Client: line {i} " + "x" * 50 for i in range(200))
    parts = ax.chunks([long, "short"], limit=3000)
    assert all(len(p) <= 3100 for p in parts)
    joined = "".join(parts)
    assert all(f"line {i} " in joined for i in range(200)) and "short" in joined


async def test_pipeline(tmp_path, monkeypatch):
    threads = ax.read_export(make_export(tmp_path))
    haiku = FakeClaude()
    # Порядок диалогов в архиве не важен: классификатор решает по содержимому.
    order = [("kate" in t.path) for t in threads]
    haiku.queue(*[topic("tattoo" if is_tattoo else "not_tattoo") for is_tattoo in order])
    sent: list[dict] = []

    async def fake_call(client, **params):
        sent.append(params)
        if "format" in params.get("output_config", {}):
            return msg(text(json.dumps({"knowledge_base": "# KB", "style_guide": "# Style", "test_cases": "# Tests"})))
        return msg(text("Цены: fine line 10 см от 3000 грн"))

    monkeypatch.setattr(ax.core, "call_model", fake_call)
    out = tmp_path / "out"
    result = await ax.analyze(threads, OWNER, out, client=None, classifier=TopicClassifier(haiku))
    assert result["tattoo"] == 1 and result["personal"] == 2
    assert (out / "knowledge_base_draft.md").read_text() == "# KB"
    personal = (out / "personal_contacts.txt").read_text()
    assert "@bestie" in personal and "@brand" in personal and "kate" not in personal
    batch_input = sent[0]["messages"][0]["content"]
    assert "3000 грн" in batch_input and "Катя" not in batch_input and "мама" not in batch_input
    assert sent[0]["fallbacks"] == "default"
