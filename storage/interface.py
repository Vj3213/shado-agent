"""Repository interface for the shado agent.

The agent depends on this protocol, never on Postgres directly, so the
backend can be swapped (e.g. to SQLite) without touching agent logic.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from storage.models import BotLoopState, MealEntry, MessageRecord


class IngredientInfo(Protocol):
    id: int
    name: str
    common_names: str


class DishInfo(Protocol):
    name: str
    meal_types: str


class SuggestionInfo(Protocol):
    dish: str
    status: str
    created_at: object


class MealAgentRepository(Protocol):
    def init_schema(self) -> None:
        """Create tables if they don't exist. Idempotent."""
        ...

    # -- conversation history -------------------------------------------

    def add_message(self, record: MessageRecord) -> None: ...

    def recent_messages(self, group_id: str, limit: int) -> list[MessageRecord]:
        """Most recent `limit` messages, returned oldest -> newest."""
        ...

    def recent_messages_since(
        self, group_id: str, hours: int, limit: int
    ) -> list[MessageRecord]:
        """Session context: messages from the last `hours`, oldest -> newest."""
        ...

    def operator_exemplars(self, group_id: str, limit: int, before: datetime | None = None) -> list[str]:
        """The operator's style samples for a chat, newest first. `before`
        excludes messages already visible in the conversation window (pass
        the window's oldest timestamp) — no duplicated tokens in the prompt."""
        """Recent messages the operator personally wrote in this chat (style samples)."""
        ...

    # -- meal log ---------------------------------------------------------

    def add_meals(self, entries: list[MealEntry]) -> int: ...

    def meals_for_day(self, day: date) -> list[MealEntry]: ...

    def meals_last_days(self, days: int) -> list[MealEntry]: ...

    # -- bot-to-bot loop cap ----------------------------------------------

    def get_loop_state(self, group_id: str, peer_sender: str) -> BotLoopState | None: ...

    def reset_loop_state(self, group_id: str, peer_sender: str) -> None:
        """Reset the counter (a human spoke)."""
        ...

    def try_increment_bot_turns(self, group_id: str, peer_sender: str, cap: int) -> int | None:
        """Atomically bump the consecutive-turns counter, refusing past `cap`.

        Returns the new count if allowed, or None if the cap blocks it.
        Enforced in SQL so the policy cannot be bypassed by a buggy agent.
        """
        ...

    # -- food knowledge base (RAG-lite retrieval) -------------------------

    def list_ingredients(self) -> list[IngredientInfo]: ...

    def all_dishes(self) -> list[DishInfo]: ...

    def dishes_for_ingredients(self, ingredient_ids: list[int]) -> list[DishInfo]: ...

    # -- suggestion lifecycle ----------------------------------------------

    def add_suggestions(self, group_id: str, dishes: list[str]) -> int:
        """Demote earlier 'suggested' rows to 'pending', insert new ones."""
        ...

    def recent_suggestions(self, group_id: str, days: int) -> list[SuggestionInfo]: ...

    def mark_suggestion_selected(self, group_id: str, dish: str) -> bool: ...

    # -- consent-gated dynamic allowlist -----------------------------------

    def request_consent(
        self, chat_id: str, pending_timeout_minutes: int = 15, ttl_hours: int = 24
    ) -> bool:
        """Ask the operator about this chat once. True if a NEW prompt was
        created (or re-created after revoke/expiry/ask-timeout); False if
        pending (fresh), declined — or a previously-approved chat was silently
        re-granted (callers re-check has_active_consent)."""
        ...

    def grant_latest_pending(self, ttl_hours: int) -> str | None:
        """Approve the most recent pending chat; returns its chat_id."""
        ...

    def decline_latest_pending(self) -> str | None: ...

    def decline_consent(self, chat_id: str) -> bool:
        """Decline ONE specific chat (e.g. a NO vote naming the chat). True
        only if it was pending."""
        ...

    def has_active_consent(self, chat_id: str) -> bool: ...

    def active_consents(self) -> list[tuple[str, object]]:
        """[(chat_id, expires_at)] for currently granted chats."""
        ...

    def pending_consents(self) -> list[tuple[str]]:
        """[(chat_id,)] awaiting the operator's YES/NO."""
        ...

    def declined_consents(self) -> list[tuple[str]]:
        """[(chat_id,)] the operator explicitly refused."""
        ...

    def grant_consent(self, chat_id: str, ttl_hours: int) -> bool:
        """Activate a specific chat (e.g. re-allowing a declined one)."""
        ...

    def revoke_active_consents(self) -> list[str]:
        """Revoke all granted chats; returns the revoked chat ids."""
        ...

    def revoke_consent(self, chat_id: str) -> str | None:
        """Revoke one specific chat; returns its id if it was active."""
        ...

    # -- learned reactions --------------------------------------------------

    def add_learned_reaction(self, emoji: str) -> None:
        """Record an emoji the operator personally used as a reaction."""
        ...

    def chat_reactions(self, chat_id: str) -> list[str]:
        """Emojis the operator reacted with in THIS chat, most-used first."""
        ...

    def add_chat_reaction(self, chat_id: str, emoji: str) -> None:
        """Record one operator reaction within a specific chat."""
        ...

    # -- contacts ----------------------------------------------------------

    def upsert_contact(self, jid: str, name: str) -> None:
        """Remember a sender's display name (empty/None never overwrites)."""
        ...

    def contact_name(self, jid: str) -> str | None:
        """Display name for a jid, if we've seen one."""
        ...

    def last_message(self, chat_id: str) -> str | None:
        """Text of the newest message in a chat (for consent labels)."""
        ...

    # -- operator context notes --------------------------------------------

    def add_context_note(self, chat_id: str, note: str, ttl_days: int) -> None:
        """Append a context note for a chat, expiring after ttl_days."""

    def context_notes(self, chat_id: str, limit: int) -> list[str]:
        """Active (non-expired) notes for a chat, newest first."""

    def forget_context(self, chat_id: str) -> int:
        """Delete ALL notes for a chat; returns how many."""

    def context_chats(self) -> list[str]:
        """Chat ids holding at least one ACTIVE note (for LIST)."""

    def last_unanswered(self, chat_id: str, within_hours: int) -> str | None:
        """Text of the newest message in a chat IF it's an unanswered incoming
        (last row is direction='in') and within the freshness window."""

    def last_outgoing_age_hours(self, chat_id: str) -> float | None:
        """Age of the newest outgoing message (agent or operator hand); None
        if never spoke there — used to keep resumed replies spaced out."""

    def may_ask_for_context(self, chat_id: str, cooldown_hours: int) -> bool:
        """True when no ask was recorded for this chat within the cooldown."""

    def record_context_ask(self, chat_id: str) -> None:
        """Stamp the ask cooldown for a chat."""

    # -- background distiller (mimicry phase 2) -----------------------------

    def bump_distill_counter(self, chat_id: str) -> None:
        """Count one operator message toward the next distill run."""

    def chats_due_for_distill(self, min_msgs: int, min_gap_hours: int) -> list[str]:
        """Chats whose operator-message count crossed the threshold and whose
        last run is older than the min gap."""

    def mark_distill_run(self, chat_id: str) -> None:
        """Reset the counter and stamp the run time."""

    def save_style_profile(self, chat_id: str, profile: str) -> None:
        """Overwrite the chat's voice profile (one row per chat)."""

    def style_profile(self, chat_id: str) -> str | None:
        """The chat's voice profile block, or None."""

    def merge_facts(self, chat_id: str, facts: list[str], ttl_days: int) -> None:
        """Insert new facts; refresh the expiry of re-mentioned ones."""

    def active_facts(self, chat_id: str, limit: int) -> list[str]:
        """Non-expired facts for a chat, newest first."""

    # -- trusted names (zero-first-ask allowlist) ---------------------------

    def trust_name(self, name: str, scope: str = "any") -> None:
        """Watch a sender's display name (optionally within one chat/group)."""

    def trust_number(self, number: str, scope: str = "any") -> None:
        """Watch a sender's phone number (digits); beats name matching."""

    def untrust_name(self, name: str) -> int:
        """Remove all trust entries for a name; returns how many."""

    def untrust_number(self, number: str) -> int:
        """Remove all trust entries for a number; returns how many."""

    def name_is_trusted(self, name: str) -> bool:
        """True if any trust entry exists for this name (any scope)."""

    def trusted_rows(self) -> list[tuple[str | None, str | None, str, str | None]]:
        """[(name, number, scope, bound_jid)] for LIST output."""
    def auto_grant_if_trusted(
        self,
        chat_id: str,
        sender: str,
        sender_name: str | None,
        ttl_hours: int,
        sender_number: str | None = None,
    ) -> str | None:
        """Auto-approve an unknown chat when its sender matches a trusted
        NUMBER (precedence) or trusted unbound NAME (TOFU). Grants a fresh TTL
        and returns a notification line for the operator, or None to fall back
        to the normal ask."""
        ...

    def learned_reactions(self) -> list[str]:
        """Emojis learned from operator reactions (beyond the static whitelist)."""
        ...

