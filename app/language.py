"""Детерминированные правила, которые выполняет код, а не модель."""

from __future__ import annotations

import re

DISCLOSURES = {
    "en": "Hi! I'm the studio's AI assistant. If you'd like to talk to a person, just write \"human\".",
    "ru": "Здравствуйте! Я AI-ассистент студии. Если хотите поговорить с человеком, напишите \"human\" или \"менеджер\".",
    "uk": "Вітаю! Я AI-асистент студії. Якщо хочете поговорити з людиною, напишіть \"human\" або \"менеджер\".",
    "es": "¡Hola! Soy el asistente de IA del estudio. Si prefieres hablar con una persona, escribe \"human\".",
    "de": "Hallo! Ich bin der KI-Assistent des Studios. Wenn du mit einem Menschen sprechen möchtest, schreib einfach \"human\".",
}

HANDOFF_ACK = {
    "en": "Got it! I've passed your message to the team, a person will reply here soon.",
    "ru": "Хорошо! Передаю ваш вопрос команде, вам скоро ответит человек.",
    "uk": "Добре! Передаю ваше питання команді, вам скоро відповість людина.",
    "es": "¡Entendido! He pasado tu mensaje al equipo, una persona te responderá pronto.",
    "de": "Alles klar! Ich habe deine Nachricht ans Team weitergegeben, ein Mensch antwortet dir bald.",
}

FAILURE_REPLY = {
    "en": "Thanks for your message! I'm passing it to the team, a person will get back to you here shortly.",
    "ru": "Спасибо за сообщение! Передаю его команде, вам скоро ответит человек.",
    "uk": "Дякую за повідомлення! Передаю його команді, вам скоро відповість людина.",
    "es": "¡Gracias por tu mensaje! Lo paso al equipo, una persona te responderá pronto.",
    "de": "Danke für deine Nachricht! Ich gebe sie ans Team weiter, ein Mensch meldet sich bald.",
}

# Основы слов: ловят падежи («менеджера», «человеком», «людину»).
HUMAN_STEMS = ("human", "manager", "operator", "real person", "менеджер", "человек", "оператор", "людин", "humano", "mensch")
EXACT_ONLY = {"person", "agent", "persona", "admin", "админ", "адмін"}
MAX_WORDS = 5

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def wants_human(text: str) -> bool:
    """Короткое сообщение, которое целиком является просьбой позвать человека."""
    cleaned = re.sub(r"\s+", " ", _PUNCT.sub(" ", text.lower())).strip()
    if not cleaned:
        return False
    if cleaned in EXACT_ONLY:
        return True
    if len(cleaned.split(" ")) > MAX_WORDS:
        return False
    return any(re.search(rf"\b{re.escape(stem)}", cleaned) for stem in HUMAN_STEMS)


def detect_language(text: str) -> str:
    t = text.lower()
    if re.search(r"[а-яёіїєґ]", t):
        if re.search(r"[іїєґ]", t):
            return "uk"
        return "ru"
    if re.search(r"[ñ¿¡]", t) or re.search(r"\b(hola|gracias|tatuaje|precio|cuánto)\b", t):
        return "es"
    if re.search(r"[äöüß]", t) or re.search(r"\b(hallo|danke|bitte|preis)\b", t):
        return "de"
    return "en"


def pick(table: dict[str, str], lang: str) -> str:
    return table.get(lang, table["en"])
