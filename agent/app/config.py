"""Centralized agent configuration: secrets from .env, tunables from config/settings.json."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
import os

from agent.app.triggers import TriggerConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

with open(PROJECT_ROOT / "config" / "settings.json") as fh:
    _SETTINGS = json.load(fh)


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value or value == "TODO":
        raise RuntimeError(
            f"Missing required env var: {name} — fill it in .env (see README: "
            f"'Finding GROUP_JID and PEER_BOT_SENDER')."
        )
    return value


def _optional(name: str) -> str | None:
    value = os.environ.get(name)
    if not value or value == "TODO":
        return None
    return value


@dataclass(frozen=True)
class AgentConfig:
    database_url: str
    gemini_api_key: str
    gemini_model: str
    gemini_fallback_models: list[str]
    gemini_retry_rounds: int
    group_jid: str
    # Optional until discovered: None disables peer-bot + loop-cap logic.
    peer_bot_sender: str | None
    bot_to_bot_max_turns: int
    context_window: int
    session_hours: int
    followup_window_seconds: int
    reply_to_everything: bool
    allow_private_chats: bool
    consent_ttl_hours: int
    consent_pending_timeout_minutes: int
    operator_jid: str | None
    exclude_eaten_days: int
    exclude_suggested_days: int
    suggestion_history_days: int
    openrouter_api_key: str
    openrouter_models: list[str]
    dry_run: bool
    triggers: TriggerConfig


def load_config() -> AgentConfig:
    config = AgentConfig(
        database_url=_required("DATABASE_URL"),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
        gemini_fallback_models=(
            os.environ.get("GEMINI_FALLBACK_MODELS", "").split(",")
            if os.environ.get("GEMINI_FALLBACK_MODELS")
            else list(_SETTINGS["gemini"]["fallback_models"])
        ),
        gemini_retry_rounds=int(_SETTINGS["gemini"].get("retry_rounds", 2)),
        group_jid=_required("GROUP_JID"),
        peer_bot_sender=_optional("PEER_BOT_SENDER"),
        bot_to_bot_max_turns=int(os.environ.get("BOT_TO_BOT_MAX_TURNS", "20")),
        context_window=int(_SETTINGS["context"]["window"]),
        session_hours=int(_SETTINGS["context"].get("session_hours", 6)),
        followup_window_seconds=int(_SETTINGS["trigger"].get("followup_window_seconds", 300)),
        reply_to_everything=_SETTINGS["trigger"].get("reply_to_everything", False),
        allow_private_chats=_SETTINGS.get("allow_private_chats", False),
        consent_ttl_hours=int(_SETTINGS.get("consent_ttl_hours", 24)),
        consent_pending_timeout_minutes=int(_SETTINGS.get("consent_pending_timeout_minutes", 15)),
        operator_jid=_optional("OPERATOR_JID"),
        exclude_eaten_days=int(_SETTINGS["context"].get("exclude_eaten_days", 3)),
        exclude_suggested_days=int(_SETTINGS["context"].get("exclude_suggested_days", 2)),
        suggestion_history_days=int(_SETTINGS["context"].get("suggestion_history_days", 3)),
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""),
        openrouter_models=(
            os.environ.get("OPENROUTER_MODELS", "").split(",")
            if os.environ.get("OPENROUTER_MODELS")
            else ["z-ai/glm-5.2:free", "google/gemma-4-31b-it:free", "qwen/qwen3.8-27b:free"]
        ),
        dry_run=os.environ.get("AGENT_DRY_RUN", "").lower() in ("1", "true", "yes"),
        triggers=TriggerConfig(
            prefixes=tuple(_SETTINGS["trigger"]["prefixes"]),
            keywords=tuple(_SETTINGS["trigger"]["keywords"]),
            patterns=tuple(re.compile(p) for p in _SETTINGS["trigger"].get("patterns", [])),
        ),
    )
    if not config.dry_run and config.gemini_api_key in ("", "TODO"):
        raise RuntimeError(
            "GEMINI_API_KEY is not set in .env — get a free key at "
            "https://aistudio.google.com/apikey, or run with AGENT_DRY_RUN=1 to test."
        )
    return config
