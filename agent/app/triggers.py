"""Trigger logic: decides which incoming messages are worth reacting to.

Three layers, all config-driven:
  prefixes  — "ved:", "@ved" — explicit addressing
  keywords  — plain substrings (khana, bhookh, lunch, ...)
  patterns  — regex for Hinglish spelling families (kha+khan/khaane/...)
  follow-up — if the bot talked to this sender seconds ago, keep chatting

Pure functions, no I/O — easy to unit test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from storage.models import MessageRecord


@dataclass(frozen=True)
class TriggerConfig:
    prefixes: tuple[str, ...]
    keywords: tuple[str, ...]
    patterns: tuple[re.Pattern, ...] = ()


@dataclass(frozen=True)
class TriggerMatch:
    kind: str  # "prefix" | "keyword" | "pattern"


def matches_trigger(text: str, triggers: TriggerConfig) -> TriggerMatch | None:
    """Return a TriggerMatch if the message mentions us or is meal-related."""
    lowered = text.lower().strip()

    for prefix in triggers.prefixes:
        if lowered.startswith(prefix.lower()) or prefix.lower() in lowered:
            return TriggerMatch(kind="prefix")

    for keyword in triggers.keywords:
        if keyword.lower() in lowered:
            return TriggerMatch(kind="keyword")

    for pattern in triggers.patterns:
        if pattern.search(lowered):
            return TriggerMatch(kind="pattern")

    return None


def is_followup(
    history: list[MessageRecord],
    sender: str,
    window_seconds: int,
    now: datetime | None = None,
) -> bool:
    """True if `sender` is continuing a conversation the bot just took part in.

    Reads the recent history (oldest -> newest): if the bot's most recent
    message was an outgoing reply sent within `window_seconds`, and the
    nearest human message before it was from this same sender, they're
    mid-conversation — a human friend would obviously keep replying.
    """
    if not history:
        return False
    current = now or datetime.now(timezone.utc)

    last_out_idx = None
    for i in range(len(history) - 1, -1, -1):
        if history[i].direction == "out":
            last_out_idx = i
            break
    if last_out_idx is None:
        return False

    age = (current - history[last_out_idx].created_at).total_seconds()
    if age > window_seconds or age < 0:
        return False

    # Who was the bot talking to? The nearest incoming message before our reply.
    for i in range(last_out_idx - 1, -1, -1):
        if history[i].direction == "in":
            return history[i].sender == sender
    return False
