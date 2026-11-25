"""Инструменты агента. Порядок и содержимое фиксированы, чтобы не ломать кэш промпта."""

from __future__ import annotations

ESCALATE = {
    "name": "escalate_to_manager",
    "description": (
        "Hand the conversation to the human studio manager. Use it when the client asks for a person, "
        "is upset or complains, asks about something not covered by the knowledge base, wants to pay or "
        "discuss a deposit, asks a medical question, wants to reschedule or cancel an existing appointment, "
        "or whenever you are unsure. After calling it, the bot stops replying in this conversation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Short reason for the manager, in English or Russian (one or two sentences).",
            },
            "urgency": {
                "type": "string",
                "enum": ["low", "normal", "high"],
                "description": "high: angry client, health concern after a session, time-critical; normal: default; low: can wait.",
            },
        },
        "required": ["reason", "urgency"],
        "additionalProperties": False,
    },
    "strict": True,
}

BOOKING = {
    "name": "create_booking_request",
    "description": (
        "Send a preliminary booking request to the manager once the client wants to book and you have "
        "collected the details. This does NOT confirm the appointment: only the manager confirms bookings and "
        "handles deposits. Use 'unknown' for any detail the client did not provide. The conversation is handed "
        "to the manager afterwards."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "idea": {"type": "string", "description": "What the client wants tattooed."},
            "size": {"type": "string", "description": "Approximate size, e.g. '10x5 cm' or 'palm-sized'."},
            "placement": {"type": "string", "description": "Body placement."},
            "style": {"type": "string", "description": "Style, e.g. fine line, blackwork, realism."},
            "references": {"type": "string", "description": "Whether references were sent and what they show."},
            "preferred_dates": {"type": "string", "description": "Dates or days the client said suit them."},
            "client_name": {"type": "string", "description": "Name if the client gave it."},
            "notes": {"type": "string", "description": "Anything else the manager should know (first tattoo, cover-up, budget)."},
        },
        "required": ["idea", "size", "placement", "style", "references", "preferred_dates", "client_name", "notes"],
        "additionalProperties": False,
    },
    "strict": True,
}

TOOLS = [ESCALATE, BOOKING]
