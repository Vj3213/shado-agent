"""Postgres implementation of MealAgentRepository (psycopg 3)."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import psycopg
from psycopg.rows import class_row

from storage.interface import MealAgentRepository
from storage.models import (
    BotLoopState,
    DishRow,
    IngredientRow,
    MealEntry,
    MessageRecord,
    SuggestionRow,
)

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_INCREMENT_SQL = """
INSERT INTO bot_loop_state (group_id, peer_sender, consecutive_turns, last_reset_at)
VALUES (%s, %s, 1, now())
ON CONFLICT (group_id, peer_sender)
DO UPDATE SET consecutive_turns = bot_loop_state.consecutive_turns + 1
WHERE bot_loop_state.consecutive_turns < %s
RETURNING consecutive_turns
"""

_RESET_SQL = """
INSERT INTO bot_loop_state (group_id, peer_sender, consecutive_turns, last_reset_at)
VALUES (%s, %s, 0, now())
ON CONFLICT (group_id, peer_sender)
DO UPDATE SET consecutive_turns = 0, last_reset_at = now()
"""


class PostgresRepository(MealAgentRepository):
    def __init__(self, database_url: str):
        self._database_url = database_url

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._database_url, autocommit=True)

    def init_schema(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(_SCHEMA_PATH.read_text())

    # -- conversation history -------------------------------------------

    def add_message(self, record: MessageRecord) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO messages (group_id, sender, text, direction, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (record.group_id, record.sender, record.text, record.direction, record.created_at),
            )

    def recent_messages(self, group_id: str, limit: int) -> list[MessageRecord]:
        with self._connect() as conn, conn.cursor(row_factory=class_row(MessageRecord)) as cur:
            cur.execute(
                "SELECT group_id, sender, text, direction, created_at FROM ("
                "  SELECT group_id, sender, text, direction, created_at, id"
                "  FROM messages WHERE group_id = %s"
                "  ORDER BY created_at DESC, id DESC LIMIT %s"
                ") recent ORDER BY created_at ASC, id ASC",
                (group_id, limit),
            )
            return cur.fetchall()

    def recent_messages_since(
        self, group_id: str, hours: int, limit: int
    ) -> list[MessageRecord]:
        with self._connect() as conn, conn.cursor(row_factory=class_row(MessageRecord)) as cur:
            cur.execute(
                "SELECT group_id, sender, text, direction, created_at FROM ("
                "  SELECT group_id, sender, text, direction, created_at, id"
                "  FROM messages WHERE group_id = %s"
                "    AND created_at > now() - make_interval(hours => %s)"
                "  ORDER BY created_at DESC, id DESC LIMIT %s"
                ") recent ORDER BY created_at ASC, id ASC",
                (group_id, hours, limit),
            )
            return cur.fetchall()

    # -- meal log ---------------------------------------------------------

    def add_meals(self, entries: list[MealEntry]) -> int:
        if not entries:
            return 0
        with self._connect() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO meal_log (day, item, meal_type) VALUES (%s, %s, %s)",
                [(e.day, e.item, e.meal_type) for e in entries],
            )
            return len(entries)

    def meals_for_day(self, day: date) -> list[MealEntry]:
        with self._connect() as conn, conn.cursor(row_factory=class_row(MealEntry)) as cur:
            cur.execute(
                "SELECT day, item, meal_type FROM meal_log WHERE day = %s ORDER BY id",
                (day,),
            )
            return cur.fetchall()

    def meals_last_days(self, days: int) -> list[MealEntry]:
        with self._connect() as conn, conn.cursor(row_factory=class_row(MealEntry)) as cur:
            cur.execute(
                "SELECT day, item, meal_type FROM meal_log "
                "WHERE created_at > now() - make_interval(days => %s) ORDER BY id",
                (days,),
            )
            return cur.fetchall()

    # -- food knowledge base (RAG-lite retrieval) -------------------------

    def list_ingredients(self) -> list[IngredientRow]:
        with self._connect() as conn, conn.cursor(row_factory=class_row(IngredientRow)) as cur:
            cur.execute("SELECT id, name, common_names FROM ingredient ORDER BY id")
            return cur.fetchall()

    def all_dishes(self) -> list[DishRow]:
        with self._connect() as conn, conn.cursor(row_factory=class_row(DishRow)) as cur:
            cur.execute("SELECT name, meal_types FROM dish ORDER BY id")
            return cur.fetchall()

    def dishes_for_ingredients(self, ingredient_ids: list[int]) -> list[DishRow]:
        if not ingredient_ids:
            return []
        with self._connect() as conn, conn.cursor(row_factory=class_row(DishRow)) as cur:
            cur.execute(
                "SELECT DISTINCT d.name, d.meal_types FROM dish d "
                "JOIN dish_ingredient di ON di.dish_id = d.id "
                "WHERE di.ingredient_id = ANY(%s) ORDER BY d.name",
                (ingredient_ids,),
            )
            return cur.fetchall()

    def dishes_out_of_season(self, month: int) -> list[str]:
        """Dish names that include an ingredient whose season csv excludes `month`."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT d.name FROM dish d "
                "JOIN dish_ingredient di ON di.dish_id = d.id "
                "JOIN ingredient i ON i.id = di.ingredient_id "
                "WHERE i.season <> 'all' "
                "  AND NOT %s = ANY(string_to_array(i.season, ',')::int[])",
                (month,),
            )
            return [row[0] for row in cur.fetchall()]

    # -- suggestion lifecycle ----------------------------------------------

    def add_suggestions(self, group_id: str, dishes: list[str]) -> int:
        if not dishes:
            return 0
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE suggestion SET status = 'pending' "
                "WHERE group_id = %s AND status = 'suggested'",
                (group_id,),
            )
            for dish in dishes:
                cur.execute(
                    "INSERT INTO suggestion (group_id, dish, status) "
                    "VALUES (%s, %s, 'suggested')",
                    (group_id, dish),
                )
            return len(dishes)

    def recent_suggestions(self, group_id: str, days: int) -> list[SuggestionRow]:
        with self._connect() as conn, conn.cursor(row_factory=class_row(SuggestionRow)) as cur:
            cur.execute(
                "SELECT dish, status, created_at FROM suggestion "
                "WHERE group_id = %s AND created_at > now() - make_interval(days => %s) "
                "ORDER BY created_at DESC",
                (group_id, days),
            )
            return cur.fetchall()

    def mark_suggestion_selected(self, group_id: str, dish: str) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE suggestion SET status = 'selected' "
                "WHERE id = (SELECT id FROM suggestion WHERE group_id = %s "
                "  AND dish ILIKE %s AND status IN ('suggested', 'pending') "
                "  ORDER BY created_at DESC LIMIT 1)",
                (group_id, dish),
            )
            return cur.rowcount > 0

    # -- bot-to-bot loop cap ----------------------------------------------

    def get_loop_state(self, group_id: str, peer_sender: str) -> BotLoopState | None:
        with self._connect() as conn, conn.cursor(row_factory=class_row(BotLoopState)) as cur:
            cur.execute(
                "SELECT group_id, peer_sender, consecutive_turns, last_reset_at "
                "FROM bot_loop_state WHERE group_id = %s AND peer_sender = %s",
                (group_id, peer_sender),
            )
            return cur.fetchone()

    def reset_loop_state(self, group_id: str, peer_sender: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(_RESET_SQL, (group_id, peer_sender))

    def try_increment_bot_turns(self, group_id: str, peer_sender: str, cap: int) -> int | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(_INCREMENT_SQL, (group_id, peer_sender, cap))
            row = cur.fetchone()
            return row[0] if row else None
