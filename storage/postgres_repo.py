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

    def operator_exemplars(self, group_id: str, limit: int, before: datetime | None = None) -> list[str]:
        """Recent messages the operator personally wrote in this chat (style samples)."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT text FROM messages "
                "WHERE group_id = %s AND direction = 'out' AND source = 'operator' "
                "AND (%s::timestamptz IS NULL OR created_at < %s) "
                "ORDER BY id DESC LIMIT %s",
                (group_id, before, before, limit),
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

    def request_consent(
        self, chat_id: str, pending_timeout_minutes: int = 15, ttl_hours: int = 24
    ) -> bool:
        """Ask the operator about this chat. True if a NEW prompt was created
        (or re-created after revoke/ask-timeout); False if pending (fresh),
        declined — or if a previously-approved chat was SILENTLY re-granted
        (callers re-check has_active_consent)."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status, updated_at, remembered FROM chat_consents WHERE chat_id = %s",
                (chat_id,),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO chat_consents (chat_id, status) VALUES (%s, 'pending')",
                    (chat_id,),
                )
                return True
            status, updated_at, remembered = row[0], row[1], row[2]
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
            # granted (expired) or revoked.
            if remembered:
                # The operator approved this chat before: expiry is just a
                # freshness boundary — silently re-grant, never re-ask.
                cur.execute(
                    "UPDATE chat_consents SET status = 'granted', granted_at = now(), "
                    "expires_at = now() + make_interval(hours => %s), updated_at = now() "
                    "WHERE chat_id = %s",
                    (ttl_hours, chat_id),
                )
                return False
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
                "expires_at = now() + make_interval(hours => %s), updated_at = now(), "
                "remembered = TRUE "
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
                "UPDATE chat_consents SET status = 'declined', remembered = FALSE, "
                "updated_at = now() "
                "WHERE chat_id = (SELECT chat_id FROM chat_consents "
                "  WHERE status = 'pending' ORDER BY updated_at DESC LIMIT 1) "
                "RETURNING chat_id"
            )
            row = cur.fetchone()
            return row[0] if row else None

    def decline_consent(self, chat_id: str) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_consents SET status = 'declined', remembered = FALSE, "
                "updated_at = now() "
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
                "expires_at = now() + make_interval(hours => %s), updated_at = now(), "
                "remembered = TRUE "
                "WHERE chat_id = %s",
                (ttl_hours, chat_id),
            )
            return cur.rowcount > 0

    def revoke_active_consents(self) -> list[str]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_consents SET status = 'revoked', remembered = FALSE, "
                "updated_at = now() "
                "WHERE status = 'granted' AND expires_at > now() RETURNING chat_id"
            )
            revoked = [row[0] for row in cur.fetchall()]
            for chat in revoked:
                self.clear_trust_binding(chat)
            return revoked

    def clear_trust_binding(self, chat_id: str) -> None:
        """STOP clears the TOFU binding granted in (or scoped to) this chat —
        and deletes the watch row itself, so a stopped chat cannot be
        resurrected by the next same-named message. UNTRUST removes watches
        without touching consents; only a fresh TRUST re-arms a stopped chat."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM trusted_names WHERE bound_chat = %s OR scope = %s",
                (chat_id, chat_id),
            )

    def revoke_consent(self, chat_id: str) -> str | None:
        """Revoke one specific chat; also clears the remembered approval so
        the next message re-asks instead of silently re-granting."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_consents SET status = 'revoked', remembered = FALSE, "
                "updated_at = now() "
                "WHERE chat_id = %s AND status = 'granted' AND expires_at > now() "
                "RETURNING chat_id",
                (chat_id,),
            )
            row = cur.fetchone()
            chat = row[0] if row else None
        if chat:
            self.clear_trust_binding(chat)
        return chat

    def add_learned_reaction(self, emoji: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO learned_reactions (emoji, times) VALUES (%s, 1) "
                "ON CONFLICT (emoji) DO UPDATE SET times = learned_reactions.times + 1",
                (emoji,),
            )

    def chat_reactions(self, chat_id: str) -> list[str]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT emoji FROM chat_reactions WHERE chat_id = %s "
                "ORDER BY times DESC, updated_at DESC",
                (chat_id,),
            )
            return [row[0] for row in cur.fetchall()]

    def add_chat_reaction(self, chat_id: str, emoji: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_reactions (chat_id, emoji, times) VALUES (%s, %s, 1) "
                "ON CONFLICT (chat_id, emoji) DO UPDATE SET "
                "times = chat_reactions.times + 1, updated_at = now()",
                (chat_id, emoji),
            )

    # -- contacts ----------------------------------------------------------

    def upsert_contact(self, jid: str, name: str) -> None:
        name = (name or "").strip()
        if not name:
            return
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO contacts (jid, name) VALUES (%s, %s) "
                "ON CONFLICT (jid) DO UPDATE SET name = EXCLUDED.name, updated_at = now()",
                (jid, name),
            )

    def contact_name(self, jid: str) -> str | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT name FROM contacts WHERE jid = %s", (jid,))
            row = cur.fetchone()
            return row[0] if row else None

    def last_message(self, chat_id: str) -> str | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT text FROM messages WHERE group_id = %s "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (chat_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    # -- operator context notes --------------------------------------------

    def add_context_note(self, chat_id: str, note: str, ttl_days: int) -> None:
        note = (note or "").strip()
        if not note:
            return
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_context (chat_id, note, expires_at) "
                "VALUES (%s, %s, now() + make_interval(days => %s))",
                (chat_id, note, ttl_days),
            )

    def context_notes(self, chat_id: str, limit: int) -> list[str]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT note FROM chat_context WHERE chat_id = %s "
                "AND expires_at > now() ORDER BY id DESC LIMIT %s",
                (chat_id, limit),
            )
            return [row[0] for row in cur.fetchall()]

    def forget_context(self, chat_id: str) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM chat_context WHERE chat_id = %s", (chat_id,))
            return cur.rowcount

    def context_chats(self) -> list[str]:
        """Chat ids that currently hold at least one ACTIVE note."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT chat_id FROM chat_context WHERE expires_at > now()"
            )
            return [row[0] for row in cur.fetchall()]

    def last_unanswered(self, chat_id: str, within_hours: int) -> str | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT direction, text, created_at FROM messages "
                "WHERE group_id = %s ORDER BY created_at DESC, id DESC LIMIT 1",
                (chat_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            direction, text, created_at = row
            if direction != "in":
                return None  # anything outgoing (agent or operator hand) answers it
            age_h = (datetime.now(timezone.utc) - created_at).total_seconds() / 3600
            return text if age_h <= within_hours else None

    def last_outgoing_age_hours(self, chat_id: str) -> float | None:
        """How long ago the last outgoing message (agent or operator hand)
        was sent in a chat; None if the bot never spoke there."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT created_at FROM messages WHERE group_id = %s "
                "AND direction = 'out' ORDER BY created_at DESC, id DESC LIMIT 1",
                (chat_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return (datetime.now(timezone.utc) - row[0]).total_seconds() / 3600

    def may_ask_for_context(self, chat_id: str, cooldown_hours: int) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT last_ask_at FROM context_asks WHERE chat_id = %s",
                (chat_id,),
            )
            row = cur.fetchone()
            if row is None:
                return True
            age_h = (datetime.now(timezone.utc) - row[0]).total_seconds() / 3600
            return age_h >= cooldown_hours

    def record_context_ask(self, chat_id: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO context_asks (chat_id, last_ask_at) VALUES (%s, now()) "
                "ON CONFLICT (chat_id) DO UPDATE SET last_ask_at = now()",
                (chat_id,),
            )

    # -- background distiller (mimicry phase 2) -----------------------------

    def bump_distill_counter(self, chat_id: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO distill_state (chat_id, operator_msgs_since) "
                "VALUES (%s, 1) ON CONFLICT (chat_id) DO UPDATE SET "
                "operator_msgs_since = distill_state.operator_msgs_since + 1",
                (chat_id,),
            )

    def chats_due_for_distill(self, min_msgs: int, min_gap_hours: int) -> list[str]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT chat_id FROM distill_state "
                "WHERE operator_msgs_since >= %s "
                "AND (last_run_at IS NULL OR last_run_at < now() - make_interval(hours => %s))",
                (min_msgs, min_gap_hours),
            )
            return [row[0] for row in cur.fetchall()]

    def mark_distill_run(self, chat_id: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE distill_state SET operator_msgs_since = 0, last_run_at = now() "
                "WHERE chat_id = %s",
                (chat_id,),
            )

    def save_style_profile(self, chat_id: str, profile: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO style_profile (chat_id, profile, updated_at) "
                "VALUES (%s, %s, now()) "
                "ON CONFLICT (chat_id) DO UPDATE SET profile = EXCLUDED.profile, updated_at = now()",
                (chat_id, profile),
            )

    def style_profile(self, chat_id: str) -> str | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT profile FROM style_profile WHERE chat_id = %s", (chat_id,))
            row = cur.fetchone()
            return row[0] if row else None

    def merge_facts(self, chat_id: str, facts: list[str], ttl_days: int) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT lower(fact) FROM chat_facts "
                "WHERE chat_id = %s AND expires_at > now()",
                (chat_id,),
            )
            existing = {row[0] for row in cur.fetchall()}
            for fact in facts:
                fact = (fact or "").strip()
                if not fact:
                    continue
                if fact.lower() in existing:
                    cur.execute(
                        "UPDATE chat_facts SET expires_at = now() + make_interval(days => %s) "
                        "WHERE chat_id = %s AND lower(fact) = %s",
                        (ttl_days, chat_id, fact.lower()),
                    )
                else:
                    cur.execute(
                        "INSERT INTO chat_facts (chat_id, fact, expires_at) "
                        "VALUES (%s, %s, now() + make_interval(days => %s))",
                        (chat_id, fact, ttl_days),
                    )
                    existing.add(fact.lower())

    def active_facts(self, chat_id: str, limit: int) -> list[str]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT fact FROM chat_facts WHERE chat_id = %s "
                "AND expires_at > now() ORDER BY id DESC LIMIT %s",
                (chat_id, limit),
            )
            return [row[0] for row in cur.fetchall()]

    # -- trusted names (zero-first-ask allowlist) ---------------------------

    def trust_name(self, name: str, scope: str = "any") -> None:
        name = (name or "").strip()
        if not name:
            return
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO trusted_names (name, scope) VALUES (%s, %s) "
                "ON CONFLICT (name, scope) DO NOTHING",
                (name, scope or "any"),
            )

    def untrust_name(self, name: str) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM trusted_names WHERE lower(name) = lower(%s)",
                (name,),
            )
            return cur.rowcount

    def name_is_trusted(self, name: str) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM trusted_names WHERE lower(name) = lower(%s) LIMIT 1",
                (name,),
            )
            return cur.fetchone() is not None

    def trusted_rows(self) -> list[tuple[str | None, str | None, str, str | None]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT name, number, scope, bound_jid FROM trusted_names ORDER BY created_at"
            )
            return cur.fetchall()

    def trust_number(self, number: str, scope: str = "any") -> None:
        number = "".join(ch for ch in (number or "") if ch.isdigit())
        if not number:
            return
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO trusted_names (number, scope) VALUES (%s, %s) "
                "ON CONFLICT (number, scope) DO NOTHING",
                (number, scope or "any"),
            )

    def untrust_number(self, number: str) -> int:
        number = "".join(ch for ch in (number or "") if ch.isdigit())
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM trusted_names WHERE number = %s", (number,))
            return cur.rowcount

    def auto_grant_if_trusted(
        self,
        chat_id: str,
        sender: str,
        sender_name: str | None,
        ttl_hours: int,
        sender_number: str | None = None,
    ) -> str | None:
        sender_number = "".join(ch for ch in (sender_number or "") if ch.isdigit()) or None
        name = (sender_name or "").strip() or None
        if not sender_number and not name:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            # An explicit refusal (NO) outranks any trusted watch —
            # only ALLOW <n> revives a declined chat.
            cur.execute(
                "SELECT status FROM chat_consents WHERE chat_id = %s",
                (chat_id,),
            )
            row = cur.fetchone()
            if row and row[0] == "declined":
                return None
        # NUMBERS first (precise): match by digits, bind like names do.
        if sender_number:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT id, scope, bound_jid FROM trusted_names "
                    "WHERE number = %s AND (scope = 'any' OR scope = %s) "
                    "ORDER BY (scope = %s) DESC, id",
                    (sender_number, chat_id, chat_id),
                )
                rows = cur.fetchall()
                unbound = next((r for r in rows if r[2] is None), None)
                mine = next((r for r in rows if r[2] == sender), None)
                if mine is not None:
                    self._grant_sticky(chat_id, ttl_hours)
                    cur.execute(
                        "UPDATE trusted_names SET bound_chat = %s WHERE id = %s",
                        (chat_id, mine[0]),
                    )
                    return None
                if unbound is not None:
                    cur.execute(
                        "UPDATE trusted_names SET bound_jid = %s, bound_chat = %s WHERE id = %s",
                        (sender, chat_id, unbound[0]),
                    )
                    self._grant_sticky(chat_id, ttl_hours)
                    return (
                        f"🤝 Number {sender_number} ({sender}) auto-approved via trusted number "
                        "— reply STOP <chat id> to undo."
                    )
                # number trusted but bound to a different jid — fall through to name
        if not name:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            # Scoped entries win over 'any'; unbound (TOFU) entries bind first.
            cur.execute(
                "SELECT id, scope, bound_jid FROM trusted_names "
                "WHERE lower(name) = lower(%s) AND (scope = 'any' OR scope = %s) "
                "ORDER BY (scope = %s) DESC, id",
                (name, chat_id, chat_id),
            )
            rows = cur.fetchall()
            unbound = next((r for r in rows if r[2] is None), None)
            mine = next((r for r in rows if r[2] == sender), None)
            if mine is not None:
                # Already bound to this sender — just refresh the TTL, no spam.
                self._grant_sticky(chat_id, ttl_hours)
                cur.execute(
                    "UPDATE trusted_names SET bound_chat = %s WHERE id = %s",
                    (chat_id, mine[0]),
                )
                return None
            if unbound is None:
                # The name is trusted but bound to a different jid: impostor or
                # duplicate — fall back to the normal (sticky) consent ask.
                return None
            cur.execute(
                "UPDATE trusted_names SET bound_jid = %s, bound_chat = %s WHERE id = %s",
                (sender, chat_id, unbound[0]),
            )
        # Upserting grant: the chat may have no consent row yet.
        self._grant_sticky(chat_id, ttl_hours)
        return f"🤝 {name} ({sender}) auto-approved via trusted name — reply STOP <chat id> to undo."

    def _grant_sticky(self, chat_id: str, ttl_hours: int) -> None:
        """Grant (or re-grant) consent, creating the row if needed."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_consents (chat_id, status, granted_at, expires_at, remembered) "
                "VALUES (%s, 'granted', now(), now() + make_interval(hours => %s), TRUE) "
                "ON CONFLICT (chat_id) DO UPDATE SET status = 'granted', granted_at = now(), "
                "expires_at = now() + make_interval(hours => %s), remembered = TRUE, updated_at = now()",
                (chat_id, ttl_hours, ttl_hours),
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
