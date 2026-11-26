"""Сценарии поведения бота. Claude, Instagram и Telegram заменены фейками."""

from __future__ import annotations

import json

from app import language
from app.instagram import split_text
from app.language import DISCLOSURES, FAILURE_REPLY, HANDOFF_ACK
from app.service import DELETED_PLACEHOLDER
from conftest import CLIENT_ID, api_error, msg, text, thinking, timeout_error, tool

EN_DISCLOSURE = DISCLOSURES["en"]


# ---------------------------------------------------------------- базовый ответ и раскрытие
async def test_first_reply_has_disclosure(env):
    env.claude.queue(msg(thinking(), text("We do fine line and blackwork!")))
    await env.client_says("Hi, what styles do you do?")
    assert env.sent_texts() == [f"{EN_DISCLOSURE}\n\nWe do fine line and blackwork!"]
    assert env.ig.sent[0][0] == CLIENT_ID


async def test_second_reply_within_day_has_no_disclosure(env):
    env.claude.queue(msg(text("Hello!")), msg(text("From 100 to 200 EUR.")))
    await env.client_says("Hi")
    env.clock.advance(hours=2)
    await env.client_says("How much is a small one?")
    assert env.sent_texts()[1] == "From 100 to 200 EUR."


async def test_disclosure_again_after_24h_pause(env):
    env.claude.queue(msg(text("Hello!")), msg(text("Welcome back!")))
    await env.client_says("Hi")
    env.clock.advance(hours=25)
    await env.client_says("Hi again")
    assert env.sent_texts()[1].startswith(EN_DISCLOSURE)


async def test_disclosure_in_client_language(env):
    env.claude.queue(msg(text("Да, делаем!")))
    await env.client_says("Привет, делаете fine line?")
    assert env.sent_texts()[0].startswith(DISCLOSURES["ru"])


async def test_disclosure_after_return_from_manager(env):
    env.claude.queue(msg(text("Hello!")), msg(text("Sure, what size?")))
    await env.client_says("Hi")
    cid = env.conv().id
    await env.press(f"take:{cid}")
    await env.press(f"return:{cid}")
    await env.client_says("I want a rose")
    assert env.sent_texts()[-1].startswith(EN_DISCLOSURE)


async def test_language_detection():
    assert language.detect_language("Привіт, скільки коштує?") == "uk"
    assert language.detect_language("Привет, сколько стоит?") == "ru"
    assert language.detect_language("Hola, cuánto cuesta?") == "es"
    assert language.detect_language("Hi there") == "en"


# ---------------------------------------------------------------- дубли и неинтересные события
async def test_duplicate_webhook_answered_once(env):
    env.claude.queue(msg(text("Hello!")))
    payload = env.payload("Hi", mid="dup_1")
    await env.service.handle_instagram(payload)
    await env.service.handle_instagram(payload)
    await env.service.drain()
    assert len(env.claude.calls) == 1
    assert len(env.ig.sent) == 1


async def test_read_and_reaction_events_ignored(env):
    payload = {"object": "instagram", "entry": [{"id": "9000", "messaging": [
        {"sender": {"id": CLIENT_ID}, "recipient": {"id": "9000"}, "timestamp": 1, "read": {"mid": "x"}},
        {"sender": {"id": CLIENT_ID}, "recipient": {"id": "9000"}, "timestamp": 1, "reaction": {"mid": "x", "action": "react"}},
    ]}]}
    assert await env.service.handle_instagram(payload) == 0
    assert env.claude.calls == []


async def test_debounce_batches_messages_into_one_call(env, settings):
    settings.debounce_seconds = 0.05
    env.claude.queue(msg(text("A rose on the forearm, nice!")))
    await env.service.handle_instagram(env.payload("Hi"))
    await env.service.handle_instagram(env.payload("I want a rose"))
    await env.service.handle_instagram(env.payload("on my forearm"))
    await env.service.drain()
    assert len(env.claude.calls) == 1
    user_text = env.claude.calls[0]["messages"][-1]["content"][0]["text"]
    assert "Hi" in user_text and "I want a rose" in user_text and "on my forearm" in user_text


# ---------------------------------------------------------------- просьба позвать человека
async def test_human_keyword_escalates_without_claude(env):
    await env.client_says("human")
    assert env.claude.calls == []
    assert env.conv().status == "manager"
    assert "Нужен менеджер" in env.tg.texts()[0]
    assert env.sent_texts()[0].endswith(HANDOFF_ACK["en"])


async def test_human_keyword_in_russian(env):
    await env.client_says("Позовите менеджера пожалуйста")
    assert language.wants_human("менеджер")
    assert env.conv().status == "manager"
    assert env.sent_texts()[0].endswith(HANDOFF_ACK["ru"])


async def test_long_message_mentioning_manager_goes_to_claude(env):
    env.claude.queue(msg(text("Sure!")))
    await env.client_says("My friend said your manager is nice, how much is a small tattoo on the wrist?")
    assert len(env.claude.calls) == 1


async def test_manager_status_bot_stays_silent(env):
    env.claude.queue(msg(text("Hello!")))
    await env.client_says("Hi")
    await env.press(f"take:{env.conv().id}")
    await env.client_says("Are you there?")
    assert len(env.claude.calls) == 1
    assert len(env.ig.sent) == 1
    assert any("Are you there?" in n for n in env.conv().pending_notes)


# ---------------------------------------------------------------- инструменты
async def test_escalate_tool(env):
    env.claude.queue(
        msg(thinking(), tool("escalate_to_manager", {"reason": "Wants a cover-up", "urgency": "normal"}), stop="tool_use"),
        msg(thinking("s2"), text("I'll pass this to the team, they'll reply here soon.")),
    )
    await env.client_says("Can you cover up an old tattoo?")
    assert env.conv().status == "manager"
    card = env.tg.sent[0]
    assert "Wants a cover-up" in card["text"]
    assert f"take:{env.conv().id}" in env.tg.buttons(0)
    assert env.sent_texts()[0].endswith("they'll reply here soon.")
    tool_result = env.claude.calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "tu_1"


async def test_booking_tool_creates_card_with_references(env):
    booking = {"idea": "rose", "size": "10 cm", "placement": "forearm", "style": "fine line",
               "references": "one photo", "preferred_dates": "next weekend", "client_name": "Kate", "notes": "first tattoo"}
    env.claude.queue(msg(text("Send me a reference")))
    await env.client_says("I want a rose")
    env.claude.queue(
        msg(tool("create_booking_request", booking), stop="tool_use"),
        msg(text("Thanks! The manager will contact you to confirm the date and deposit.")),
    )
    await env.client_says(attachments=[{"type": "image", "payload": {"url": "https://cdn.example/ref.jpg"}}])
    card = env.tg.sent[-1]["text"]
    assert "Заявка на запись" in card and "forearm" in card and "next weekend" in card
    assert "https://cdn.example/ref.jpg" in card
    assert f"confirm:{env.conv().id}" in env.tg.buttons(-1)
    assert env.conv().status == "manager"
    row = env.db.one("SELECT data FROM bookings")
    assert json.loads(row["data"])["idea"] == "rose"


async def test_confirm_booking_button(env):
    booking = dict.fromkeys(["idea", "size", "placement", "style", "references", "preferred_dates", "client_name", "notes"], "x")
    env.claude.queue(msg(tool("create_booking_request", booking), stop="tool_use"), msg(text("Done!")))
    await env.client_says("Book me")
    await env.press(f"confirm:{env.conv().id}")
    assert env.db.one("SELECT status FROM bookings")["status"] == "confirmed"
    assert len(env.ig.sent) == 1  # подтверждение не пишет клиенту от имени бота


async def test_empty_text_after_escalation_sends_ack(env):
    env.claude.queue(
        msg(tool("escalate_to_manager", {"reason": "r", "urgency": "low"}), stop="tool_use"),
        msg(thinking()),
    )
    await env.client_says("Something odd")
    assert env.sent_texts()[0].endswith(HANDOFF_ACK["en"])


async def test_tool_loop_cap_falls_back(env, settings):
    settings.max_tool_iterations = 2
    env.claude.queue(
        msg(tool("escalate_to_manager", {"reason": "a", "urgency": "low"}), stop="tool_use"),
        msg(tool("escalate_to_manager", {"reason": "b", "urgency": "low"}, tid="tu_2"), stop="tool_use"),
    )
    await env.client_says("Loop")
    assert env.sent_texts()[0].endswith(FAILURE_REPLY["en"])


# ---------------------------------------------------------------- отказоустойчивость
async def test_api_error_sends_template_and_escalates(env):
    env.claude.queue(api_error(500))
    await env.client_says("Hi")
    assert env.sent_texts()[0] == f"{EN_DISCLOSURE}\n\n{FAILURE_REPLY['en']}"
    texts = env.tg.texts()
    assert any("Claude API" in t for t in texts)
    assert any("СРОЧНО" in t for t in texts)
    assert env.conv().status == "manager"


async def test_timeout_sends_template(env):
    env.claude.queue(timeout_error())
    await env.client_says("Hi")
    assert env.sent_texts()[0].endswith(FAILURE_REPLY["en"])


async def test_rate_limit_or_no_credits_sends_template(env):
    env.claude.queue(api_error(429))
    await env.client_says("Hi")
    assert env.sent_texts()[0].endswith(FAILURE_REPLY["en"])


async def test_refusal_sends_template(env):
    env.claude.queue(msg(stop="refusal"))
    await env.client_says("Hi")
    assert env.sent_texts()[0].endswith(FAILURE_REPLY["en"])


async def test_history_valid_after_failure(env):
    env.claude.queue(api_error(500))
    await env.client_says("Hi")
    await env.press(f"return:{env.conv().id}")
    env.claude.queue(msg(text("Hello again")))
    await env.client_says("Hello?")
    roles = [m["role"] for m in env.claude.calls[-1]["messages"]]
    assert roles == ["user", "assistant", "user"]


async def test_instagram_send_failure_alerts(env):
    env.ig.fail = True
    env.claude.queue(msg(text("Hello")))
    await env.client_says("Hi")
    assert any("Не удалось отправить" in t for t in env.tg.texts())


# ---------------------------------------------------------------- эхо менеджера
async def test_manager_echo_pauses_bot(env):
    env.claude.queue(msg(text("Hello!")))
    await env.client_says("Hi")
    await env.manager_echo("Hi! Anna here, I'll help you.")
    conv = env.conv()
    assert conv.status == "manager"
    assert any("Anna here" in n for n in conv.pending_notes)
    assert any("Бот на паузе" in t for t in env.tg.texts())
    await env.client_says("Thanks Anna")
    assert len(env.claude.calls) == 1


async def test_own_echo_ignored(env):
    env.claude.queue(msg(text("Hello!")))
    await env.client_says("Hi")
    await env.manager_echo(env.sent_texts()[0], mid="out_1")
    assert env.conv().status == "bot"


async def test_own_echo_before_mid_known_matched_by_text(env):
    env.claude.queue(msg(text("Hello!")))
    await env.client_says("Hi")
    env.db.execute("UPDATE messages SET mid = NULL WHERE author = 'bot'")
    await env.manager_echo(env.sent_texts()[0], mid="early_echo")
    assert env.conv().status == "bot"
    assert env.db.one("SELECT mid FROM messages WHERE author = 'bot'")["mid"] == "early_echo"


async def test_manager_messages_reach_model_after_return(env):
    env.claude.queue(msg(text("Hello!")))
    await env.client_says("Hi")
    await env.manager_echo("The artist is on vacation until May")
    await env.press(f"return:{env.conv().id}")
    env.claude.queue(msg(text("Got it")))
    await env.client_says("ok so when can I come?")
    user_text = env.claude.calls[-1]["messages"][-1]["content"][0]["text"]
    assert "vacation until May" in user_text
    assert "returned the conversation" in user_text
    assert env.conv().pending_notes == []


# ---------------------------------------------------------------- режим одобрения
async def test_approval_mode_creates_draft(env, settings):
    settings.approval_mode = True
    env.claude.queue(msg(text("Hello!")))
    await env.client_says("Hi")
    assert env.ig.sent == []
    assert "Черновик ответа" in env.tg.sent[-1]["text"]
    assert env.tg.buttons(-1)[0].startswith("send:")


async def test_approval_send(env, settings):
    settings.approval_mode = True
    env.claude.queue(msg(text("Hello!")))
    await env.client_says("Hi")
    await env.press(env.tg.buttons(-1)[0])
    assert env.sent_texts() == [f"{EN_DISCLOSURE}\n\nHello!"]
    assert env.db.one("SELECT status FROM drafts")["status"] == "sent"


async def test_approval_edit(env, settings):
    settings.approval_mode = True
    env.claude.queue(msg(text("Hello!")))
    await env.client_says("Hi")
    draft_id = env.tg.buttons(-1)[1].split(":")[1]
    await env.press(f"edit:{draft_id}")
    prompt = env.tg.sent[-1]
    assert prompt["force_reply"]
    await env.service.handle_telegram({"message": {
        "chat": {"id": -100}, "text": "Hi! Anna here.", "reply_to_message": {"message_id": prompt["id"]},
    }})
    assert env.sent_texts() == ["Hi! Anna here."]
    assert env.db.one("SELECT status FROM drafts")["status"] == "edited"
    assert any("actually received" in n for n in env.conv().pending_notes)


async def test_approval_grab(env, settings):
    settings.approval_mode = True
    env.claude.queue(msg(text("Hello!")))
    await env.client_says("Hi")
    await env.press(env.tg.buttons(-1)[2])
    assert env.ig.sent == []
    assert env.conv().status == "manager"
    assert any("NOT sent" in n for n in env.conv().pending_notes)


async def test_approval_send_after_window_closed(env, settings):
    settings.approval_mode = True
    env.claude.queue(msg(text("Hello!")))
    await env.client_says("Hi")
    env.clock.advance(hours=25)
    await env.press(env.tg.buttons(-1)[0])
    assert env.ig.sent == []
    assert any("Окно 24 ч" in t for t in env.tg.texts())


async def test_new_client_message_supersedes_draft(env, settings):
    settings.approval_mode = True
    env.claude.queue(msg(text("Hello!")), msg(text("A rose, nice")))
    await env.client_says("Hi")
    first_send = env.tg.buttons(-1)[0]
    await env.client_says("I want a rose")
    assert "Your previous reply was NOT sent" in env.claude.calls[-1]["messages"][-1]["content"][0]["text"]
    await env.press(first_send)
    assert env.ig.sent == []


# ---------------------------------------------------------------- правила Meta и данные
async def test_bot_never_writes_first_on_return(env):
    env.claude.queue(msg(text("Hello!")))
    await env.client_says("Hi")
    await env.press(f"take:{env.conv().id}")
    await env.press(f"return:{env.conv().id}")
    assert len(env.ig.sent) == 1
    assert len(env.claude.calls) == 1


async def test_deleted_message_removed_and_redacted(env):
    env.claude.queue(msg(text("Noted")))
    await env.client_says("my phone is 555-1234", mid="secret_mid")
    await env.client_says(mid="secret_mid", deleted=True)
    assert env.db.one("SELECT id FROM messages WHERE mid = 'secret_mid'") is None
    history = json.dumps(env.conv().llm_history)
    assert "555-1234" not in history and DELETED_PLACEHOLDER in history


async def test_purge_after_retention(env):
    env.claude.queue(msg(text("Hello")))
    await env.client_says("Hi")
    env.clock.advance(hours=24 * 91)
    await env.service.purge()
    assert env.conv() is None
    assert env.db.one("SELECT COUNT(*) AS n FROM messages")["n"] == 0


async def test_callback_from_other_chat_ignored(env):
    env.claude.queue(msg(text("Hello")))
    await env.client_says("Hi")
    await env.press(f"take:{env.conv().id}", chat_id="-999")
    assert env.conv().status == "bot"


def test_split_long_text():
    long = ("This is a sentence about aftercare. " * 60).strip()
    parts = split_text(long)
    assert len(parts) > 1 and all(len(p) <= 1000 for p in parts)
    assert " ".join(parts).replace("  ", " ") == long


# ---------------------------------------------------------------- запрос к Claude
async def test_request_shape(env):
    env.claude.queue(msg(text("Hello")))
    await env.client_says("Hi")
    params = env.claude.calls[0]
    assert params["model"] == "claude-opus-5-5"
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert params["cache_control"] == {"type": "ephemeral"}
    assert params["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in params["betas"]
    assert params["thinking"]["type"] == "adaptive"
    assert params["output_config"] == {"effort": "medium"}
    assert "tool_choice" not in params and "temperature" not in params
    assert all(t["strict"] for t in params["tools"])


async def test_haiku_has_no_fallbacks(env, settings):
    settings.claude_model = "claude-haiku-5-5"
    env.claude.queue(msg(text("Hello"), model="claude-haiku-5-5"))
    await env.client_says("Hi")
    assert "fallbacks" not in env.claude.calls[0]
    assert "server-side-fallback-2026-07-01" not in env.claude.calls[0]["betas"]


async def test_history_is_append_only_with_thinking(env):
    env.claude.queue(
        msg(thinking("sig_a"), tool("escalate_to_manager", {"reason": "r", "urgency": "low"}), stop="tool_use"),
        msg(thinking("sig_b"), text("Passing to the team")),
    )
    await env.client_says("Question 1")
    await env.press(f"return:{env.conv().id}")
    env.claude.queue(msg(text("Answer 2")))
    await env.client_says("Question 2")
    first_full = env.claude.calls[1]["messages"]
    second = env.claude.calls[2]["messages"]
    # Всё, что видела модель раньше, передаётся байт в байт, плюс её финальный ответ.
    assert second[: len(first_full)] == first_full
    assert second[len(first_full)]["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig_b"}
    assert env.claude.calls[1]["system"] == env.claude.calls[2]["system"]
    assert env.claude.calls[1]["tools"] == env.claude.calls[2]["tools"]


# ---------------------------------------------------------------- бюджет, сводка, токен
async def test_budget_alerts_once_at_50_and_80(env, settings):
    settings.monthly_budget_usd = 1.0
    big = {"input_tokens": 0, "output_tokens": 15_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    env.claude.queue(msg(text("a"), usage=big), msg(text("b"), usage=big), msg(text("c"), usage=big))
    for t in ("1", "2", "3"):  # $0.30 за ответ
        await env.client_says(t)
    alerts = [t for t in env.tg.texts() if "бюджета" in t]
    assert len(alerts) == 2
    assert "50%" in alerts[0] and "80%" in alerts[1]


async def test_daily_summary(env):
    env.claude.queue(msg(text("Hello")))
    await env.client_says("Hi")
    await env.service.daily_summary()
    summary = env.tg.texts()[-1]
    assert "Сводка" in summary and "Диалогов: 1" in summary and "$" in summary


async def test_page_token_not_refreshed_in_facebook_mode(env, settings):
    settings.ig_access_token = "page-token"
    settings.ig_api_mode = "facebook"
    await env.service.check_token()
    assert env.tg.sent == [] and env.db.kv_get("ig_token") is None


async def test_token_check_skipped_without_instagram(env):
    await env.service.check_token()
    assert env.tg.sent == [] and env.db.kv_get("ig_token") is None


async def test_token_refresh_stores_new_token(env, settings):
    settings.ig_access_token = "old-token"
    settings.ig_api_mode = "instagram"
    await env.service.check_token()
    assert env.db.kv_get("ig_token") == "new-token"
    assert float(env.db.kv_get("ig_token_expires_at")) > env.clock() + 50 * 86400


async def test_token_expiry_reminder(env, settings):
    settings.ig_access_token = "old-token"
    settings.ig_api_mode = "instagram"
    env.ig.refresh_result = RuntimeError("refresh denied")
    env.db.kv_set("ig_token_expires_at", str(env.clock() + 5 * 86400))
    await env.service.check_token()
    texts = env.tg.texts()
    assert any("Не удалось обновить токен" in t for t in texts)
    assert any("истекает через 5" in t for t in texts)


async def test_handoff_ack_keeps_conversation_language(env):
    env.claude.queue(msg(text("Расскажите про идею")))
    await env.client_says("Хочу записаться")
    await env.client_says("Human")
    assert env.sent_texts()[-1] == HANDOFF_ACK["ru"]


async def test_switch_to_english_is_respected(env):
    env.claude.queue(msg(text("Расскажите про идею")), msg(text("Sure!")))
    await env.client_says("Хочу записаться")
    await env.client_says("Can we continue in English please, I don't read Russian well")
    assert len(env.claude.calls) == 2  # не ушло в менеджера и не сломалось


async def test_model_told_not_to_greet_when_disclosure_added(env):
    env.claude.queue(msg(text("Hello!")), msg(text("Sure")))
    await env.client_says("Hi")
    first = env.claude.calls[0]["messages"][-1]["content"][0]["text"]
    assert "Do not greet the client" in first
    await env.client_says("I want a rose")
    second = env.claude.calls[1]["messages"][-1]["content"][0]["text"]
    assert "Do not greet the client" not in second
