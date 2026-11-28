"""Бот ведёт только диалоги про тату. Личная переписка мастера не хранится и никуда не пересылается."""

from __future__ import annotations

from conftest import CLIENT_ID, image, msg, text, topic


def stored_texts(env) -> list[str]:
    return [r["text"] for r in env.db.all("SELECT text FROM messages")]


async def test_tattoo_question_answered(fenv):
    fenv.haiku.queue(topic("tattoo"))
    fenv.claude.queue(msg(text("Small fine line pieces start from 100 EUR.")))
    await fenv.client_says("Hi! How much is a small fine line tattoo?")
    assert fenv.conv().scope == "tattoo"
    assert fenv.sent_texts()[0].endswith("start from 100 EUR.")


async def test_personal_chat_ignored_and_forgotten(fenv):
    fenv.haiku.queue(topic("not_tattoo"))
    await fenv.client_says("Аня, привет! Как там мама, выздоровела?")
    conv = fenv.conv()
    assert conv.scope == "ignored"
    assert fenv.claude.calls == [] and fenv.ig.sent == []
    # В Telegram только короткое уведомление без текста сообщения.
    assert len(fenv.tg.sent) == 1 and "мама" not in fenv.tg.texts()[0] and "assign:" in str(fenv.tg.sent[0]["markup"])
    assert stored_texts(fenv) == [] and conv.llm_history == [] and conv.username is None


async def test_ignored_chat_stays_ignored_without_more_model_calls(fenv):
    fenv.haiku.queue(topic("not_tattoo"))
    await fenv.client_says("Wanna grab a coffee tomorrow?")
    await fenv.client_says("Also how much is a tattoo lol")
    assert len(fenv.haiku.calls) == 1 and fenv.claude.calls == []
    assert stored_texts(fenv) == []


async def test_unclear_waits_for_more(fenv):
    fenv.haiku.queue(topic("unclear"), topic("tattoo"))
    fenv.claude.queue(msg(text("Sure, what size?")))
    await fenv.client_says("Привет!")
    assert fenv.ig.sent == [] and fenv.conv().scope == "unknown"
    await fenv.client_says("Хочу татуировку на запястье")
    assert fenv.conv().scope == "tattoo"
    classifier_input = fenv.haiku.calls[1]["messages"][0]["content"]
    assert "Привет!" in classifier_input and "запястье" in classifier_input
    bot_input = fenv.claude.calls[0]["messages"][-1]["content"][0]["text"]
    assert "Привет!" in bot_input and "запястье" in bot_input


async def test_unclear_too_long_left_to_artist(fenv):
    fenv.haiku.queue(topic("unclear"), topic("unclear"), topic("unclear"))
    for t in ("Hey", "🙂", "?"):
        await fenv.client_says(t)
    assert fenv.conv().scope == "ignored"
    assert fenv.ig.sent == [] and stored_texts(fenv) == []


async def test_old_contact_tattoo_question_always_drafted(fenv, settings):
    settings.approval_mode = False  # даже после тестового периода
    fenv.ig.existing_contact = True
    fenv.haiku.queue(topic("tattoo"))
    fenv.claude.queue(msg(text("Sure! What size?")))
    await fenv.client_says("Hi again, I want another tattoo")
    conv = fenv.conv()
    assert conv.scope == "tattoo" and conv.review_required
    assert fenv.ig.sent == []  # ничего не ушло клиенту без проверки
    assert "Черновик ответа" in fenv.tg.texts()[-1]


async def test_old_contact_personal_message_left_with_notice(fenv):
    fenv.ig.existing_contact = True
    fenv.haiku.queue(topic("not_tattoo"))
    await fenv.client_says("Аня, привет! Как дела?")
    assert fenv.conv().scope == "ignored"
    assert fenv.claude.calls == [] and fenv.ig.sent == []
    assert len(fenv.tg.sent) == 1 and "Как дела" not in fenv.tg.texts()[0]


async def test_existing_contact_check_can_be_disabled(fenv, settings):
    settings.skip_existing_contacts = False
    fenv.ig.existing_contact = True
    fenv.haiku.queue(topic("tattoo"))
    fenv.claude.queue(msg(text("From 100 EUR")))
    await fenv.client_says("Hi, how much is a tattoo?")
    assert fenv.ig.history_checks == 0 and len(fenv.ig.sent) == 1


async def test_history_check_failure_falls_back_to_classifier(fenv):
    fenv.ig.existing_contact = None  # API не ответил
    fenv.haiku.queue(topic("not_tattoo"))
    await fenv.client_says("Hi Anna, it's me from school!")
    assert fenv.conv().scope == "ignored"


async def test_ad_click_skips_classifier(fenv):
    fenv.claude.queue(msg(text("Hi! Tell me about your idea")))
    await fenv.client_says("Hi", referral={"source": "ADS", "ad_id": "123", "type": "OPEN_THREAD"})
    assert fenv.conv().scope == "tattoo"
    assert fenv.haiku.calls == [] and fenv.ig.history_checks == 0
    assert len(fenv.ig.sent) == 1


async def test_ad_referral_event_without_message(fenv):
    payload = {"object": "instagram", "entry": [{"id": "9000", "messaging": [{
        "sender": {"id": CLIENT_ID}, "recipient": {"id": "9000"}, "timestamp": 1_780_000_000_000,
        "referral": {"source": "ADS", "ad_id": "123", "type": "OPEN_THREAD"},
    }]}]}
    await fenv.service.handle_instagram(payload)
    await fenv.service.drain()
    assert fenv.conv().scope == "tattoo" and fenv.ig.sent == []  # бот не пишет первым
    fenv.claude.queue(msg(text("Hello!")))
    await fenv.client_says("Hello")
    assert fenv.haiku.calls == [] and len(fenv.ig.sent) == 1


async def test_artist_writes_first_is_personal(fenv):
    await fenv.manager_echo("Привет, дорогая! Как ты?")
    assert fenv.conv().scope == "personal"
    await fenv.client_says("Хорошо! Кстати, сколько стоит тату?")
    assert fenv.haiku.calls == [] and fenv.claude.calls == [] and fenv.ig.sent == []
    assert stored_texts(fenv) == [] and fenv.tg.sent == []


async def test_artist_answers_before_bot_decides(fenv):
    fenv.haiku.queue(topic("unclear"))
    await fenv.client_says("Hi")
    await fenv.manager_echo("Hi! Who's this?")
    assert fenv.conv().scope == "ignored"
    assert all("Who's this" not in t for t in fenv.tg.texts()) and stored_texts(fenv) == []


async def test_personal_command_blocks_contact(fenv):
    await fenv.service.handle_telegram({"message": {"chat": {"id": -100}, "text": "/personal @Best_Friend"}})
    fenv.ig.username = "best_friend"
    await fenv.client_says("Hey, how much would you charge me for a tattoo? :)")
    assert fenv.conv().scope == "personal"
    assert fenv.haiku.calls == [] and fenv.ig.sent == []


async def test_bot_command_hands_conversation_over(fenv):
    fenv.haiku.queue(topic("not_tattoo"))
    await fenv.client_says("Collab offer for your page")
    fenv.db.update_conversation(fenv.conv().id, username="ink_lover")
    await fenv.service.handle_telegram({"message": {"chat": {"id": -100}, "text": "/bot @ink_lover"}})
    assert fenv.conv().scope == "tattoo"
    fenv.claude.queue(msg(text("Sure, tell me your idea")))
    await fenv.client_says("Actually I want a tattoo")
    assert len(fenv.ig.sent) == 1


async def test_images_not_uploaded_for_personal_chats(fenv):
    fenv.haiku.queue(topic("not_tattoo"))
    await fenv.client_says("Look at my cat!", attachments=image())
    assert fenv.images.uploaded == []


async def test_images_uploaded_once_tattoo(fenv):
    fenv.haiku.queue(topic("tattoo"))
    fenv.claude.queue(msg(text("Lovely reference")))
    await fenv.client_says("Can you do this?", attachments=image())
    assert fenv.images.uploaded == ["file_1"]
    assert fenv.claude.calls[0]["messages"][-1]["content"][1]["type"] == "image"


async def test_manager_echo_in_personal_chat_not_forwarded(fenv):
    fenv.haiku.queue(topic("not_tattoo"))
    await fenv.client_says("Are we still on for dinner?")
    await fenv.manager_echo("Yes, 8pm!")
    assert all("8pm" not in t and "dinner" not in t for t in fenv.tg.texts()) and stored_texts(fenv) == []


async def test_classifier_cost_counted(fenv):
    fenv.haiku.queue(topic("not_tattoo"))
    await fenv.client_says("Hi Anna")
    assert fenv.db.spend_since(0) > 0


async def test_classifier_request_shape(fenv):
    fenv.haiku.queue(topic("not_tattoo"))
    await fenv.client_says("Hi Anna")
    params = fenv.haiku.calls[0]
    assert params["model"] == "claude-haiku-5-5"
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert "fallbacks" not in params and "tool_choice" not in params


async def test_daily_summary_counts_skipped(fenv):
    fenv.haiku.queue(topic("not_tattoo"))
    await fenv.client_says("Hi Anna")
    await fenv.service.daily_summary()
    assert "оставлено мастеру: 1" in fenv.tg.texts()[-1]


async def test_silent_notice_sent_once_and_assign_button_answers(fenv):
    fenv.haiku.queue(topic("unclear"), topic("unclear"))
    await fenv.client_says("Привет!")
    await fenv.client_says("👋")
    notices = [m for m in fenv.tg.sent if "бот не отвечает" in m["text"]]
    assert len(notices) == 1
    fenv.claude.queue(msg(text("Здравствуйте! Чем помочь?")))
    await fenv.press(f"assign:{fenv.conv().id}")
    assert fenv.conv().scope == "tattoo"
    # Бот ответил на уже пришедшие сообщения: это ответ, а не первое сообщение.
    assert len(fenv.ig.sent) == 1 and fenv.sent_texts()[0].endswith("Чем помочь?")


async def test_master_first_chat_has_no_notice(fenv):
    await fenv.manager_echo("Привет, дорогая!")
    await fenv.client_says("Привет! Как ты?")
    assert fenv.tg.sent == []
