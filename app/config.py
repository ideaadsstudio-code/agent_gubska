"""Настройки из переменных окружения. Секреты никогда не хранятся в коде."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # Claude
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "medium"
    claude_max_tokens: int = 16000
    claude_timeout_seconds: float = 60.0
    max_tool_iterations: int = 4

    # Instagram
    ig_app_secret: str = ""
    ig_access_token: str = ""
    ig_verify_token: str = ""
    graph_api_version: str = "v24.0"

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_webhook_secret: str = ""

    # Поведение
    approval_mode: bool = True
    debounce_seconds: float = 4.0
    disclosure_pause_hours: int = 24
    reply_window_hours: int = 24
    retention_days: int = 90
    monthly_budget_usd: float = 100.0
    summary_hour_utc: int = 18
    token_reminder_days: int = 7
    token_refresh_days: int = 10

    # Хранилище и тексты
    db_path: str = str(BASE_DIR / "data" / "agent.db")
    system_prompt_path: str = str(BASE_DIR / "prompts" / "system.md")
    knowledge_base_path: str = str(BASE_DIR / "knowledge" / "knowledge_base.md")
    studio_name: str = "the studio"
    privacy_contact: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.claude_model = os.getenv("CLAUDE_MODEL", s.claude_model)
        s.claude_effort = os.getenv("CLAUDE_EFFORT", s.claude_effort)
        s.claude_max_tokens = int(os.getenv("CLAUDE_MAX_TOKENS", s.claude_max_tokens))
        s.claude_timeout_seconds = float(os.getenv("CLAUDE_TIMEOUT_SECONDS", s.claude_timeout_seconds))
        s.ig_app_secret = os.getenv("IG_APP_SECRET", "")
        s.ig_access_token = os.getenv("IG_ACCESS_TOKEN", "")
        s.ig_verify_token = os.getenv("IG_VERIFY_TOKEN", "")
        s.graph_api_version = os.getenv("GRAPH_API_VERSION", s.graph_api_version)
        s.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        s.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        s.telegram_webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
        s.approval_mode = _bool("APPROVAL_MODE", s.approval_mode)
        s.debounce_seconds = float(os.getenv("DEBOUNCE_SECONDS", s.debounce_seconds))
        s.retention_days = int(os.getenv("RETENTION_DAYS", s.retention_days))
        s.monthly_budget_usd = float(os.getenv("MONTHLY_BUDGET_USD", s.monthly_budget_usd))
        s.summary_hour_utc = int(os.getenv("SUMMARY_HOUR_UTC", s.summary_hour_utc))
        s.db_path = os.getenv("DB_PATH", s.db_path)
        s.studio_name = os.getenv("STUDIO_NAME", s.studio_name)
        s.privacy_contact = os.getenv("PRIVACY_CONTACT", "")
        return s
