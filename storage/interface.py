"""Repository interface for the meal agent.

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

