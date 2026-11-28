"""Обучение на ответах человека: сбор примеров, предложения правил, принятие и применение."""

from __future__ import annotations

import json

import pytest

import app.learning as learning
from app.learning import Learner
from conftest import msg, text


@pytest.fixture
def lenv(env, settings):
    settings.approval_mode = True
    env.learner = Learner(env.service, "BASE PROMPT\n\n# Knowledge base\nPrices: £150+")
    env.service.learner = env.learner
    env.learner.apply()
    return env


def examples(env):
    return env.db.all("SELECT * FROM learning_examples ORDER BY id")


async def draft(env, client_text="Hi", bot_text="Hello!"):
    env.claude.queue(msg(text(bot_text)))
    await env.client_says(client_text)
    return env.tg.sent[-1]


async def test_edited_draft_recorded(lenv):
    await draft(lenv, "How much for 15 cm?", "From £200 to £500.")
    draft_id = lenv.tg.buttons(-1)[1].split(":")[1]
    await lenv.press(f"edit:{draft_id}")
    prompt = lenv.tg.sent[-1]
    await lenv.service.handle_telegram({"message": {
        "chat": {"id": -100}, "text": "Hi! Usually £250-400 for that size :)", "reply_to_message": {"message_id": prompt["id"]}}})
    ex = examples(lenv)
    assert len(ex) == 1 and ex[0]["kind"] == "edited"
    assert "From £200" in ex[0]["bot_draft"] and ex[0]["human_reply"].startswith("Hi! Usually")
    assert "How much for 15 cm?" in ex[0]["context"]


async def test_manager_reply_instead_of_draft_recorded(lenv):
    await draft(lenv, "Do you do cover-ups?", "Yes, send a photo.")
    await lenv.manager_echo("Depends on the tattoo, send me a photo please :)")
    ex = examples(lenv)
    assert ex[0]["kind"] == "replaced" and ex[0]["bot_draft"]


async def test_manual_reply_recorded(lenv, settings):
    settings.approval_mode = False
    lenv.claude.queue(msg(text("Hello!")))
    await lenv.client_says("Hi")
    await lenv.manager_echo("Hi! Anastasiia here")
    ex = examples(lenv)
    assert ex[0]["kind"] == "manual" and ex[0]["bot_draft"] is None


async def test_personal_chats_not_recorded(lenv, settings):
    settings.topic_filter = True
    await lenv.manager_echo("Привет, дорогая!")  # мастер начала: личное
    await lenv.client_says("Привет!")
    await lenv.manager_echo("Как дела?")
    assert examples(lenv) == []


async def run_learning(lenv, monkeypatch, rules):
    sent = []

    async def fake_call(client, **params):
        sent.append(params)
        return msg(text(json.dumps({"rules": rules})))

    monkeypatch.setattr(learning, "call_model", fake_call)
    return sent


async def test_learn_proposes_accept_and_apply(lenv, monkeypatch):
    await draft(lenv, "Do you do cover-ups?", "Yes")
    await lenv.manager_echo("Depends on the tattoo, send me a photo +44 7700 900123 please")
    sent = await run_learning(lenv, monkeypatch, [
        {"kind": "faq", "rule": "For cover-ups, ask for a photo of the current tattoo.", "why": "Мастер просит фото"}])
    await lenv.service.handle_telegram({"message": {"chat": {"id": -100}, "text": "/learn"}})
    await lenv.service.drain()
    body = sent[0]["messages"][0]["content"]
    assert "+44 7700" not in body and "[phone]" in body  # обезличено
    assert sent[0]["model"] == "claude-sonnet-5-5"
    card = lenv.tg.sent[-1]
    assert "ask for a photo" in card["text"]
    ok_btn = card["markup"]["inline_keyboard"][0][0]["callback_data"]
    await lenv.press(ok_btn)
    system = lenv.service.agent.system[0]["text"]
    assert "For cover-ups, ask for a photo" in system and system.startswith("BASE PROMPT")
    assert examples(lenv)[0]["used"] == 1


async def test_reject_and_unrule(lenv, monkeypatch):
    await draft(lenv)
    await lenv.manager_echo("Hi there :)")
    await run_learning(lenv, monkeypatch, [
        {"kind": "style", "rule": "Use ':)' smiley.", "why": "так пишет мастер"},
        {"kind": "fact", "rule": "Studio is closed on Sundays.", "why": "упоминала"}])
    await lenv.service.handle_telegram({"message": {"chat": {"id": -100}, "text": "/learn"}})
    await lenv.service.drain()
    cards = [m for m in lenv.tg.sent if m["markup"] and "learn_ok" in str(m["markup"])]
    first_ok = cards[0]["markup"]["inline_keyboard"][0][0]["callback_data"]
    second_no = cards[1]["markup"]["inline_keyboard"][0][1]["callback_data"]
    await lenv.press(first_ok)
    await lenv.press(second_no)
    system = lenv.service.agent.system[0]["text"]
    assert "Use ':)'" in system and "closed on Sundays" not in system
    rid = first_ok.split(":")[1]
    await lenv.service.handle_telegram({"message": {"chat": {"id": -100}, "text": f"/unrule {rid}"}})
    assert "Use ':)'" not in lenv.service.agent.system[0]["text"]


async def test_weekly_needs_enough_examples(lenv, monkeypatch):
    sent = await run_learning(lenv, monkeypatch, [])
    await lenv.learner.maybe_weekly(lenv.clock())
    assert sent == []  # меньше 5 примеров: модель не вызывается


async def test_accepted_rules_survive_restart(lenv, monkeypatch):
    lenv.db.execute("INSERT INTO learned_rules(kind, rule, why, status, created_at) VALUES ('fact', 'Rule A', '', 'accepted', 0)")
    fresh = Learner(lenv.service, "BASE PROMPT")
    fresh.apply()
    assert "Rule A" in lenv.service.agent.system[0]["text"]
