"""Postgres implementation of MealAgentRepository (psycopg 3)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
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


class OperatorExemplar:
    def __init__(self, text: str, created_at: datetime):
        self.text = text
        self.created_at = created_at

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
                "INSERT INTO messages (group_id, sender, text, direction, source, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (record.group_id, record.sender, record.text, record.direction, record.source, record.created_at),
            )

    def recent_messages(self, group_id: str, limit: int) -> list[MessageRecord]:
        with self._connect() as conn, conn.cursor(row_factory=class_row(MessageRecord)) as cur:
            cur.execute(
                "SELECT group_id, sender, text, direction, source, created_at FROM ("
                "  SELECT group_id, sender, text, direction, source, created_at, id"
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
                "SELECT group_id, sender, text, direction, source, created_at FROM ("
                "  SELECT group_id, sender, text, direction, source, created_at, id"
                "  FROM messages WHERE group_id = %s"
                "    AND created_at > now() - make_interval(hours => %s)"
                "  ORDER BY created_at DESC, id DESC LIMIT %s"
                ") recent ORDER BY created_at ASC, id ASC",
                (group_id, hours, limit),
            )
            return cur.fetchall()

    def operator_exemplars(self, group_id: str, limit: int) -> list[str]:
        """Recent messages the operator personally wrote in this chat (style samples)."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT text FROM messages "
                "WHERE group_id = %s AND direction = 'out' AND source = 'operator' "
                "ORDER BY id DESC LIMIT %s",
                (group_id, limit),
            )
            return [row[0] for row in cur.fetchall()]

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

    # -- consent-gated dynamic allowlist -----------------------------------

    def request_consent(self, chat_id: str, pending_timeout_minutes: int = 15) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status, updated_at FROM chat_consents WHERE chat_id = %s",
                (chat_id,),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO chat_consents (chat_id, status) VALUES (%s, 'pending')",
                    (chat_id,),
                )
                return True
            status, updated_at = row[0], row[1]
            if status == "pending":
                # A pending ask that nobody answered (or whose prompt never
                # delivered) must not silence the chat forever — re-ask after
                # the configured timeout so transient failures self-heal.
                stale = (datetime.now(timezone.utc) - updated_at).total_seconds() / 60
                if stale >= pending_timeout_minutes:
                    cur.execute(
                        "UPDATE chat_consents SET updated_at = now() WHERE chat_id = %s",
                        (chat_id,),
                    )
                    return True
                return False
            if status == "declined":
                return False  # explicit refusal: never re-ask
            # granted (expired) or revoked -> ask again
            cur.execute(
                "UPDATE chat_consents SET status = 'pending', updated_at = now() "
                "WHERE chat_id = %s",
                (chat_id,),
            )
            return True

    def grant_latest_pending(self, ttl_hours: int) -> str | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_consents SET status = 'granted', granted_at = now(), "
                "expires_at = now() + make_interval(hours => %s), updated_at = now() "
                "WHERE chat_id = (SELECT chat_id FROM chat_consents "
                "  WHERE status = 'pending' ORDER BY updated_at DESC LIMIT 1) "
                "RETURNING chat_id",
                (ttl_hours,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def decline_latest_pending(self) -> str | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_consents SET status = 'declined', updated_at = now() "
                "WHERE chat_id = (SELECT chat_id FROM chat_consents "
                "  WHERE status = 'pending' ORDER BY updated_at DESC LIMIT 1) "
                "RETURNING chat_id"
            )
            row = cur.fetchone()
            return row[0] if row else None

    def decline_consent(self, chat_id: str) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_consents SET status = 'declined', updated_at = now() "
                "WHERE chat_id = %s AND status = 'pending'",
                (chat_id,),
            )
            return cur.rowcount > 0

    def has_active_consent(self, chat_id: str) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM chat_consents WHERE chat_id = %s "
                "AND status = 'granted' AND (expires_at IS NULL OR expires_at > now())",
                (chat_id,),
            )
            return cur.fetchone() is not None

    def active_consents(self) -> list[tuple[str, object]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT chat_id, expires_at FROM chat_consents "
                "WHERE status = 'granted' AND expires_at > now() "
                "ORDER BY granted_at DESC"
            )
            return cur.fetchall()

    def pending_consents(self) -> list[tuple[str]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT chat_id FROM chat_consents WHERE status = 'pending' "
                "ORDER BY updated_at DESC"
            )
            return cur.fetchall()

    def declined_consents(self) -> list[tuple[str]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT chat_id FROM chat_consents WHERE status = 'declined' "
                "ORDER BY updated_at DESC"
            )
            return cur.fetchall()

    def grant_consent(self, chat_id: str, ttl_hours: int) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_consents SET status = 'granted', granted_at = now(), "
                "expires_at = now() + make_interval(hours => %s), updated_at = now() "
                "WHERE chat_id = %s",
                (ttl_hours, chat_id),
            )
            return cur.rowcount > 0

    def revoke_active_consents(self) -> list[str]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_consents SET status = 'revoked', updated_at = now() "
                "WHERE status = 'granted' AND expires_at > now() RETURNING chat_id"
            )
            return [row[0] for row in cur.fetchall()]

    def revoke_consent(self, chat_id: str) -> str | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_consents SET status = 'revoked', updated_at = now() "
                "WHERE chat_id = %s AND status = 'granted' AND expires_at > now() "
                "RETURNING chat_id",
                (chat_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def add_learned_reaction(self, emoji: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO learned_reactions (emoji, times) VALUES (%s, 1) "
                "ON CONFLICT (emoji) DO UPDATE SET times = learned_reactions.times + 1",
                (emoji,),
            )

    def learned_reactions(self) -> list[str]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT emoji FROM learned_reactions ORDER BY times DESC")
            return [row[0] for row in cur.fetchall()]

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
