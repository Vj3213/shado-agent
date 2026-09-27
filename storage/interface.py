"""Repository interface for the whatsapp agent.

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

    def request_consent(self, chat_id: str, pending_timeout_minutes: int = 15) -> bool:
        """Ask the operator about this chat once. True if a NEW prompt was
        created (or re-created after revoke/expiry/ask-timeout); False if
        pending (fresh) or declined."""
        ...

    def grant_latest_pending(self, ttl_hours: int) -> str | None:
        """Approve the most recent pending chat; returns its chat_id."""
        ...

    def decline_latest_pending(self) -> str | None: ...

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

