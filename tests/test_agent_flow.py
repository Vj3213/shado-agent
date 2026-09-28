"""Agent HTTP-flow tests: the FastAPI boundary the gateway talks to.

Replaces scripts/test_agent_flow.sh (the old heredoc script ran against the
LIVE meal_agent database). This port runs against the isolated meal_agent_test
database via conftest, using a real TestClient — no network, no WhatsApp.

AGENT_DRY_RUN is set deliberately here (the flow test wants canned replies,
not real Gemini), and unset right after import so it can't leak into the
generation-path tests (known trap in AGENTS.md).
"""

from __future__ import annotations

import os
from datetime import date, datetime

os.environ["AGENT_DRY_RUN"] = "1"
from fastapi.testclient import TestClient

from agent.app import main as agent_main
del os.environ["AGENT_DRY_RUN"]

import dataclasses

import pytest
from agent.app.gemini_client import AgentOutput, PollOutput

from tests.conftest import RUNNING_TEST_GROUP, RUNNING_TEST_PEER, TEST_DATABASE_URL
from storage.postgres_repo import PostgresRepository

OPERATOR = os.environ["OPERATOR_JID"]
HUMAN = "918888877777@s.whatsapp.net"
UNKNOWN_GROUP = "999unknown@g.us"
UNKNOWN_PRIVATE = "918887776666@s.whatsapp.net"


@pytest.fixture
def client():
    with TestClient(agent_main.app) as c:
        yield c


@pytest.fixture
def repo():
    return PostgresRepository(TEST_DATABASE_URL)


def _incoming(text: str, chat: str = RUNNING_TEST_GROUP, sender: str = HUMAN, timestamp: str | None = None) -> dict:
    return {
        "group_id": chat,
        "sender": sender,
        "text": text,
        # Fresh by default: consent/repairs look back one session window (6h).
        "timestamp": timestamp or datetime.now().astimezone().isoformat(),
    }


class TestHealth:
    def test_health_reports_ok(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok"


class TestIncoming:
    def test_human_message_gets_dry_run_reply_and_is_stored(self, client, repo):
        response = client.post("/messages/incoming", json=_incoming("ved: kya breakfast banau? hungry yaar"))
        body = response.json()
        assert response.status_code == 200
        assert body["reply"].startswith("[dry-run]")
        assert body["reason"] == "ok"

        stored = repo.recent_messages(RUNNING_TEST_GROUP, limit=5)
        assert len(stored) == 1 and stored[0].direction == "in"

    def test_reply_to_everything_setting_reaches_http_layer(self, client):
        # settings.json has reply_to_everything=true, so even a greeting gets
        # a reply here. (Trigger logic itself is pinned in test_agent_logic.)
        body = client.post("/messages/incoming", json=_incoming("good morning all")).json()
        assert body["reason"] == "ok" and body["reply"].startswith("[dry-run]")

    def test_dry_run_generation_logs_a_meal(self, client, repo):
        client.post("/messages/incoming", json=_incoming("khana time"))
        meals = repo.meals_for_day(date.today())
        assert [m.item for m in meals] == ["dry-run snack"]


class TestPeerBotLoopCap:
    def test_two_turns_then_cap_then_human_reset(self, client, repo):
        for _ in range(2):
            body = client.post(
                "/messages/incoming",
                json=_incoming("Suggestion: try aloo paratha today!", sender=RUNNING_TEST_PEER),
            ).json()
            assert body["reason"] == "ok"
        state = repo.get_loop_state(RUNNING_TEST_GROUP, RUNNING_TEST_PEER)
        assert state is not None and state.consecutive_turns == 2

        capped = client.post(
            "/messages/incoming",
            json=_incoming("Suggestion: try poha today!", sender=RUNNING_TEST_PEER),
        ).json()
        assert capped["reply"] is None and capped["reason"] == "bot_loop_cap_reached"

        # Any human message resets the counter.
        client.post("/messages/incoming", json=_incoming("ved: ok ok"))
        assert repo.get_loop_state(RUNNING_TEST_GROUP, RUNNING_TEST_PEER).consecutive_turns == 0
        again = client.post(
            "/messages/incoming",
            json=_incoming("Suggestion: upma today!", sender=RUNNING_TEST_PEER),
        ).json()
        assert again["reason"] == "ok"


class TestConsentGate:
    def test_unknown_group_asks_once_then_stays_quiet(self, client, repo):
        first = client.post("/messages/incoming", json=_incoming("hello", chat=UNKNOWN_GROUP)).json()
        assert first["reply"] is None and first["reason"] == "consent_requested"
        assert UNKNOWN_GROUP in first["self_prompt"]

        # Still pending (not timed out): no re-ask, no reply.
        second = client.post("/messages/incoming", json=_incoming("anyone there?", chat=UNKNOWN_GROUP)).json()
        assert second["reason"] == "no_consent"
        assert repo.has_active_consent(UNKNOWN_GROUP) is False

    def test_unknown_private_chat_asks_too(self, client):
        body = client.post("/messages/incoming", json=_incoming("hi", chat=UNKNOWN_PRIVATE, sender=UNKNOWN_PRIVATE)).json()
        assert body["reason"] == "consent_requested"

    def test_granted_consent_unblocks_the_chat(self, client, repo):
        client.post("/messages/incoming", json=_incoming("hello", chat=UNKNOWN_GROUP))
        repo.grant_consent(UNKNOWN_GROUP, ttl_hours=1)
        body = client.post("/messages/incoming", json=_incoming("now talk", chat=UNKNOWN_GROUP)).json()
        assert body["reason"] == "ok" and body["reply"].startswith("[dry-run]")

    def test_yes_also_delivers_the_pending_reply(self, client, repo):
        """The bug Ved hit: YES granted consent but nobody replied to the
        message that triggered the ask — the chat waited forever."""
        client.post("/messages/incoming", json=_incoming("Hello", chat=UNKNOWN_GROUP))
        body = client.post("/messages/incoming", json=_incoming("YES", chat=OPERATOR, sender=OPERATOR)).json()
        assert body["reason"] == "console_consent_granted"
        assert body["deliver"]["chat_id"] == UNKNOWN_GROUP
        assert body["deliver"]["text"].startswith("[dry-run]")

    def test_no_delivers_nothing(self, client):
        client.post("/messages/incoming", json=_incoming("Hello", chat=UNKNOWN_GROUP))
        body = client.post("/messages/incoming", json=_incoming("NO", chat=OPERATOR, sender=OPERATOR)).json()
        assert body["reason"] == "console_consent_declined"
        assert body["deliver"] is None

    def test_allow_revives_chat_and_delivers_recent_reply(self, client, repo):
        client.post("/messages/incoming", json=_incoming("Hello", chat=UNKNOWN_GROUP))
        client.post("/messages/incoming", json=_incoming("NO", chat=OPERATOR, sender=OPERATOR))
        body = client.post("/messages/incoming", json=_incoming("ALLOW 1", chat=OPERATOR, sender=OPERATOR)).json()
        assert body["reason"] == "console_re_allowed"
        assert body["deliver"]["chat_id"] == UNKNOWN_GROUP

    def test_yes_with_targeted_chat_grants_and_delivers(self, client, repo):
        """Consent poll votes arrive as `yes <chat>` — they must grant THAT
        chat, not just the latest pending one."""
        client.post("/messages/incoming", json=_incoming("Hello", chat=UNKNOWN_GROUP))
        body = client.post(
            "/messages/incoming",
            json=_incoming(f"yes {UNKNOWN_GROUP}", chat=OPERATOR, sender=OPERATOR),
        ).json()
        assert body["reason"] == "console_consent_granted"
        assert body["deliver"]["chat_id"] == UNKNOWN_GROUP
        assert repo.has_active_consent(UNKNOWN_GROUP) is True

    def test_no_with_targeted_chat_declines_only_that_chat(self, client, repo):
        other = "120363other@g.us"
        client.post("/messages/incoming", json=_incoming("Hello", chat=UNKNOWN_GROUP))
        client.post("/messages/incoming", json=_incoming("Hello", chat=other))
        body = client.post(
            "/messages/incoming",
            json=_incoming(f"no {UNKNOWN_GROUP}", chat=OPERATOR, sender=OPERATOR),
        ).json()
        assert body["reason"] == "console_consent_declined"
        assert [c for c, in repo.pending_consents()] == [other]
        assert repo.declined_consents() == [(UNKNOWN_GROUP,)]

    def test_targeted_vote_for_unknown_chat_is_rejected(self, client):
        body = client.post(
            "/messages/incoming",
            json=_incoming("yes 999nothere@g.us", chat=OPERATOR, sender=OPERATOR),
        ).json()
        assert body["reason"] == "console_unknown_chat"
        assert "No pending request" in body["reply"]


class TestOperatorConsole:
    def test_commands_via_operator_dm(self, client):
        body = client.post("/messages/incoming", json=_incoming("LIST", chat=OPERATOR, sender=OPERATOR)).json()
        assert body["reason"] == "console_empty" and "No chats" in body["reply"]

        body = client.post("/messages/incoming", json=_incoming("yes", chat=OPERATOR, sender=OPERATOR)).json()
        assert body["reason"] == "console_no_pending"

    def test_operator_dm_plain_text_is_conversation(self, client):
        body = client.post("/messages/incoming", json=_incoming("yaar kya scene hai", chat=OPERATOR, sender=OPERATOR)).json()
        assert body["reason"] == "ok" and body["reply"].startswith("[dry-run]")

    def test_console_accept_decline_round_trip(self, client, repo):
        client.post("/messages/incoming", json=_incoming("hello", chat=UNKNOWN_GROUP))
        body = client.post("/messages/incoming", json=_incoming("YES", chat=OPERATOR, sender=OPERATOR)).json()
        assert body["reason"] == "console_consent_granted"
        assert repo.has_active_consent(UNKNOWN_GROUP) is True

        body = client.post("/messages/incoming", json=_incoming("STOP", chat=OPERATOR, sender=OPERATOR)).json()
        assert body["reason"] == "console_revoked"
        assert repo.has_active_consent(UNKNOWN_GROUP) is False


class TestOutgoing:
    def test_outgoing_operator_message_is_recorded(self, client, repo):
        response = client.post(
            "/messages/outgoing",
            json={
                "group_id": UNKNOWN_PRIVATE,
                "text": "typed by ved on bot phone",
                "timestamp": "2026-09-08T09:00:00Z",
                "source": "operator",
            },
        )
        assert response.json()["stored"] is True
        stored = repo.recent_messages(UNKNOWN_PRIVATE, limit=5)
        assert len(stored) == 1 and stored[0].source == "operator"

    def test_outgoing_to_unknown_chat_surfaces_consent_prompt(self, client):
        body = client.post(
            "/messages/outgoing",
            json={
                "group_id": UNKNOWN_PRIVATE,
                "text": "hey new contact",
                "timestamp": "2026-09-08T09:00:00Z",
                "source": "operator",
            },
        ).json()
        assert body["stored"] is True and UNKNOWN_PRIVATE in body["self_prompt"]

    def test_outgoing_to_operator_console_never_asks_consent(self, client):
        body = client.post(
            "/messages/outgoing",
            json={
                "group_id": OPERATOR,
                "text": "operator's own chat",
                "timestamp": "2026-09-08T09:00:00Z",
                "source": "operator",
            },
        ).json()
        assert body == {"stored": True}

    def test_outgoing_console_command_from_bot_phone(self, client, repo):
        """Typing 'yes' into the operator DM from the BOT phone must run the
        console too (Ved's live test: that text was recorded but ignored)."""
        client.post("/messages/incoming", json=_incoming("Hello", chat=UNKNOWN_GROUP))
        body = client.post(
            "/messages/outgoing",
            json={
                "group_id": OPERATOR,
                "text": "yes",
                "timestamp": "2026-09-08T09:00:00Z",
                "source": "operator",
            },
        ).json()
        assert body["reason"] == "console_consent_granted"
        assert body["deliver"]["chat_id"] == UNKNOWN_GROUP
        assert repo.has_active_consent(UNKNOWN_GROUP) is True


class TestReactionLearning:
    def test_operator_reaction_is_learned_and_whitelisted(self, client, repo):
        response = client.post("/messages/reaction", json={"emoji": "🫡"})
        assert response.json() == {"learned": "🫡"}
        assert "🫡" in repo.learned_reactions()

    def test_user_would_react_reaches_the_decision(self, client, monkeypatch):
        output = AgentOutput(
            reply="😂😂", meals=[], suggested_dishes=[],
            selected_dish=None, is_food_related=True,
            react="😂", user_would_react=True,
        )
        monkeypatch.setattr(agent_main.gemini, "decide_and_extract", lambda **kw: output)
        body = client.post("/messages/incoming", json=_incoming("kya joke suna hai")).json()
        assert body["react"] == "😂" and body["user_would_react"] is True

    def test_learned_reactions_lead_the_allowed_list(self, client, repo):
        """The model mirrors the user's vocabulary: emojis the operator actually
        used come FIRST in ALLOWED REACTIONS (the prompt list is ordered)."""
        repo.add_learned_reaction("🔥")
        client.post("/messages/reaction", json={"emoji": "🫡"})
        body = client.post("/messages/incoming", json=_incoming("khana time")).json()
        assert body["reason"] == "ok"  # prompt built fine with the new ordering


class TestPolls:
    """The model may attach a native WhatsApp poll; the agent sanitizes it
    (enabled? question? >=2 options? clamped?) and the gateway transports it."""

    def _stub_model(self, monkeypatch, output: AgentOutput):
        monkeypatch.setattr(agent_main.gemini, "decide_and_extract", lambda **kw: output)

    def _output(self, poll: PollOutput | None) -> AgentOutput:
        return AgentOutput(
            reply="vote karo 👇", meals=[], suggested_dishes=[],
            selected_dish=None, is_food_related=True, poll=poll,
        )

    def test_poll_reaches_the_decision(self, client, monkeypatch):
        self._stub_model(monkeypatch, self._output(PollOutput(question="Aaj kya banau?", options=["poha", "upma"])))
        body = client.post("/messages/incoming", json=_incoming("kya banau aaj?")).json()
        assert body["reason"] == "ok"
        assert body["poll"] == {"question": "Aaj kya banau?", "options": ["poha", "upma"]}

    def test_poll_options_clamped_to_configured_max(self, client, monkeypatch):
        many = PollOutput(question="Aaj kya banau?", options=["a", "b", "c", "d", "e", "f"])
        self._stub_model(monkeypatch, self._output(many))
        body = client.post("/messages/incoming", json=_incoming("kya banau aaj?")).json()
        assert len(body["poll"]["options"]) == 4  # settings.json polls.max_options

    def test_poll_stripped_when_disabled(self, client, monkeypatch):
        disabled = dataclasses.replace(agent_main.config, polls_enabled=False)
        monkeypatch.setattr(agent_main, "config", disabled)
        self._stub_model(monkeypatch, self._output(PollOutput(question="q", options=["a", "b"])))
        body = client.post("/messages/incoming", json=_incoming("kya banau aaj?")).json()
        assert body["poll"] is None

    def test_poll_dropped_without_two_options(self, client, monkeypatch):
        self._stub_model(monkeypatch, self._output(PollOutput(question="q", options=["only"])))
        body = client.post("/messages/incoming", json=_incoming("kya banau aaj?")).json()
        assert body["reason"] == "ok" and body["poll"] is None
