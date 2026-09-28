"""Storage module tests: real Postgres, real SQL — the guarantee is IN the
(SQL) WHERE clause, so tests run against the actual PostgresRepository with
an isolated throwaway database (see tests/conftest.py)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from psycopg import connect

from tests.conftest import TEST_DATABASE_URL
from storage.models import MealEntry, MessageRecord

GROUP = "120363storage@g.us"
PEER = "919999storage@s.whatsapp.net"


def _record(text: str, direction: str, at: datetime, source: str = "agent", sender: str = "918888800000@s.whatsapp.net") -> MessageRecord:
    return MessageRecord(
        group_id=GROUP, sender=sender, text=text, direction=direction,  # type: ignore[arg-type]
        created_at=at, source=source,
    )


class TestMessages:
    def test_round_trip_and_ordering(self, repo):
        now = datetime.now(timezone.utc)
        old, mid, new = now - timedelta(hours=3), now - timedelta(hours=1), now
        for rec in (_record("newest", "in", new), _record("oldest", "in", old), _record("mIDDLE", "out", mid)):
            repo.add_message(rec)

        rows = repo.recent_messages(GROUP, limit=10)
        assert [r.text for r in rows] == ["oldest", "mIDDLE", "newest"]

        latest_only = repo.recent_messages(GROUP, limit=2)
        assert [r.text for r in latest_only] == ["mIDDLE", "newest"]

    def test_messages_are_scoped_by_group(self, repo):
        repo.add_message(_record("here", "in", datetime.now(timezone.utc)))
        repo.add_message(MessageRecord(
            group_id="120363other@g.us", sender="s", text="elsewhere",  # type: ignore[arg-type]
            direction="in", created_at=datetime.now(timezone.utc),
        ))
        assert [r.text for r in repo.recent_messages(GROUP, 10)] == ["here"]

    def test_recent_messages_since_excludes_old_context(self, repo):
        now = datetime.now(timezone.utc)
        repo.add_message(_record("ancient", "in", now - timedelta(days=2)))
        repo.add_message(_record("fresh", "in", now - timedelta(minutes=5)))
        rows = repo.recent_messages_since(GROUP, hours=6, limit=20)
        assert [r.text for r in rows] == ["fresh"]

    def test_operator_exemplars_only_operator_hand(self, repo):
        """Mimicry samples must come from the human's manual sends only."""
        now = datetime.now(timezone.utc)
        repo.add_message(_record("typed by ved on bot phone", "out", now, source="operator"))
        repo.add_message(_record("bot generated text", "out", now, source="agent"))
        repo.add_message(_record("incoming human text", "in", now, source="operator"))
        assert repo.operator_exemplars(GROUP, limit=10) == ["typed by ved on bot phone"]


class TestMeals:
    def test_meal_round_trip(self, repo):
        today = date.today()
        repo.add_meals([MealEntry(day=today, item="poha", meal_type="breakfast")])
        assert [m.item for m in repo.meals_for_day(today)] == ["poha"]

    def test_meals_last_days_filters_by_logged_time(self, repo):
        """meals_last_days filters on created_at (insert time), not the `day`
        column — that's the current semantic, so test THAT."""
        repo.add_meals([MealEntry(day=date.today(), item="poha", meal_type="breakfast")])
        assert [m.item for m in repo.meals_last_days(3)] == ["poha"]


class TestLoopCap:
    """The bot-to-bot cap is enforced atomically in SQL — these tests exercise
    try_increment_bot_turns directly, which is exactly what policy uses."""

    def test_counts_up_to_cap_then_blocks(self, repo):
        for i in range(1, 3):  # BOT_TO_BOT_MAX_TURNS=2 in the test env
            assert repo.try_increment_bot_turns(GROUP, PEER, cap=2) == i
        assert repo.try_increment_bot_turns(GROUP, PEER, cap=2) is None
        state = repo.get_loop_state(GROUP, PEER)
        assert state is not None and state.consecutive_turns == 2

    def test_human_message_resets_counter(self, repo):
        repo.try_increment_bot_turns(GROUP, PEER, cap=2)
        repo.reset_loop_state(GROUP, PEER)
        assert repo.get_loop_state(GROUP, PEER).consecutive_turns == 0
        assert repo.try_increment_bot_turns(GROUP, PEER, cap=2) == 1

    def test_counters_are_per_group_and_peer(self, repo):
        repo.try_increment_bot_turns(GROUP, PEER, cap=5)
        assert repo.get_loop_state(GROUP, "other-bot@s.whatsapp.net") is None
        assert repo.get_loop_state("120363elsewhere@g.us", PEER) is None


class TestSuggestions:
    def test_lifecycle_suggested_then_pending_then_selected(self, repo):
        repo.add_suggestions(GROUP, ["aloo paratha"])
        rows = repo.recent_suggestions(GROUP, days=1)
        assert [(r.dish, r.status) for r in rows] == [("aloo paratha", "suggested")]

        repo.add_suggestions(GROUP, ["poha"])  # demotes earlier 'suggested' to 'pending'
        rows = {(r.dish, r.status) for r in repo.recent_suggestions(GROUP, days=1)}
        assert rows == {("aloo paratha", "pending"), ("poha", "suggested")}

        assert repo.mark_suggestion_selected(GROUP, "aloo paratha") is True
        statuses = {r.dish: r.status for r in repo.recent_suggestions(GROUP, days=1)}
        assert statuses == {"aloo paratha": "selected", "poha": "suggested"}
        assert repo.mark_suggestion_selected(GROUP, "never-suggested") is False


class TestConsents:
    def _stale(self, chat_id: str, minutes: int) -> None:
        """Age a consent row so the pending-ask timeout logic can fire."""
        with connect(TEST_DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_consents SET updated_at = %s WHERE chat_id = %s",
                (datetime.now(timezone.utc) - timedelta(minutes=minutes), chat_id),
            )

    def test_request_then_grant_flow(self, repo):
        assert repo.request_consent("unknown@g.us") is True        # asks the operator
        assert repo.request_consent("unknown@g.us") is False       # pending, no nagging
        assert repo.grant_latest_pending(ttl_hours=1) == "unknown@g.us"
        assert repo.has_active_consent("unknown@g.us") is True

    def test_pending_ask_times_out_and_reasks(self, repo):
        assert repo.request_consent("unknown@g.us") is True
        assert repo.request_consent("unknown@g.us") is False
        self._stale("unknown@g.us", minutes=16)  # > consent_pending_timeout_minutes (15)
        assert repo.request_consent("unknown@g.us") is True  # self-heals with a fresh prompt

    def test_declined_is_silent_forever(self, repo):
        assert repo.request_consent("unknown@g.us") is True
        assert repo.decline_latest_pending() == "unknown@g.us"
        self._stale("unknown@g.us", minutes=60)
        assert repo.request_consent("unknown@g.us") is False  # even stale, never re-ask

    def test_allow_revives_declined_chat(self, repo):
        repo.request_consent("unknown@g.us")
        repo.decline_latest_pending()
        assert repo.declined_consents() == [("unknown@g.us",)]
        assert repo.grant_consent("unknown@g.us", ttl_hours=1) is True
        assert repo.has_active_consent("unknown@g.us") is True

    def test_stop_revokes_active_chats(self, repo):
        repo.request_consent("unknown@g.us")
        repo.grant_latest_pending(ttl_hours=1)
        assert [c for c, _ in repo.active_consents()] == ["unknown@g.us"]
        assert repo.revoke_consent("unknown@g.us") == "unknown@g.us"
        assert repo.has_active_consent("unknown@g.us") is False
        assert repo.revoke_active_consents() == []  # nothing left to revoke

    def test_expired_consent_no_longer_active(self, repo):
        repo.request_consent("unknown@g.us")
        repo.grant_consent("unknown@g.us", ttl_hours=0)  # expires immediately
        assert repo.has_active_consent("unknown@g.us") is False


class TestLearnedReactions:
    def test_frequency_counts_and_ordering(self, repo):
        repo.add_learned_reaction("🫡")
        repo.add_learned_reaction("🔥")
        repo.add_learned_reaction("🔥")
        learned = repo.learned_reactions()
        assert learned == ["🔥", "🫡"]


def test_schema_init_is_idempotent(repo):
    repo.init_schema()
    repo.init_schema()  # second call must not fail
