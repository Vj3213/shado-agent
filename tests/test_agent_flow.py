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
import base64
from datetime import date, datetime

os.environ["AGENT_DRY_RUN"] = "1"
from fastapi.testclient import TestClient

from agent.app import main as agent_main
del os.environ["AGENT_DRY_RUN"]

import dataclasses

import pytest
from psycopg import connect
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


def _incoming(
    text: str,
    chat: str = RUNNING_TEST_GROUP,
    sender: str = HUMAN,
    timestamp: str | None = None,
    sender_name: str | None = None,
) -> dict:
    payload = {
        "group_id": chat,
        "sender": sender,
        "text": text,
        # Fresh by default: consent/repairs look back one session window (6h).
        "timestamp": timestamp or datetime.now().astimezone().isoformat(),
    }
    if sender_name:
        payload["sender_name"] = sender_name
    return payload


def _media_message(**overrides) -> dict:
    """A tiny valid PNG for the media endpoint."""
    payload = {
        "group_id": RUNNING_TEST_GROUP,
        "sender": HUMAN,
        "timestamp": datetime.now().astimezone().isoformat(),
        "kind": "image",
        "mimetype": "image/png",
        "media_base64": base64.b64encode(
            bytes.fromhex("89504e470d0a1a0a0000000d494844520000000100000001080600000") + b"\x00" * 8
        ).decode(),
        "caption": "look at this",
    }
    payload.update(overrides)
    return payload


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

class TestConsentReplyDecision:
    """After YES, the MODEL decides whether to speak now (it may see the person
    was already answered by a hand-typed message) or join from the next one."""

    def _stub_model(self, monkeypatch, output: AgentOutput):
        monkeypatch.setattr(agent_main.gemini, "decide_and_extract", lambda **kw: output)

    def test_model_skip_delivers_nothing(self, client, monkeypatch):
        self._stub_model(monkeypatch, AgentOutput(
            reply=None, meals=[], suggested_dishes=[], selected_dish=None, is_food_related=False,
        ))
        client.post("/messages/incoming", json=_incoming("Hello", chat=UNKNOWN_GROUP))
        body = client.post("/messages/incoming", json=_incoming("yes", chat=OPERATOR, sender=OPERATOR)).json()
        assert body["reason"] == "console_consent_granted"
        assert body["deliver"] is None

    def test_model_acknowledge_delivers(self, client, monkeypatch):
        self._stub_model(monkeypatch, AgentOutput(
            reply="hello ji! aa gaya", meals=[], suggested_dishes=[],
            selected_dish=None, is_food_related=False,
        ))
        client.post("/messages/incoming", json=_incoming("Hello", chat=UNKNOWN_GROUP))
        body = client.post("/messages/incoming", json=_incoming("yes", chat=OPERATOR, sender=OPERATOR)).json()
        assert body["deliver"] == {"chat_id": UNKNOWN_GROUP, "text": "hello ji! aa gaya"}


class TestContacts:
    def test_sender_name_is_captured_and_shown_in_consent_ask(self, client, repo):
        body = client.post(
            "/messages/incoming",
            json=_incoming("Hello", chat=UNKNOWN_PRIVATE, sender=UNKNOWN_PRIVATE, sender_name="Rohit"),
        ).json()
        assert body["reason"] == "consent_requested"
        assert "Rohit" in body["self_prompt"] and UNKNOWN_PRIVATE in body["self_prompt"]
        assert repo.contact_name(UNKNOWN_PRIVATE) == "Rohit"

    def test_reaction_learning_is_per_chat(self, client, repo):
        client.post("/messages/reaction", json={"emoji": "🔥", "chat_id": RUNNING_TEST_GROUP})
        client.post("/messages/reaction", json={"emoji": "🔥", "chat_id": RUNNING_TEST_GROUP})
        client.post("/messages/reaction", json={"emoji": "🫡", "chat_id": UNKNOWN_GROUP})
        assert repo.chat_reactions(RUNNING_TEST_GROUP) == ["🔥"]  # frequency-ordered
        assert repo.chat_reactions(UNKNOWN_GROUP) == ["🫡"]
        assert "🔥" in repo.learned_reactions() and "🫡" in repo.learned_reactions()


class TestTrustCommands:
    def test_trust_multiple_users_share_one_scope(self, client, repo):
        body = client.post(
            "/messages/incoming",
            json=_incoming("TRUST Rohit, Priya, Amit", chat=OPERATOR, sender=OPERATOR),
        ).json()
        assert body["reason"] == "console_trusted"
        rows = {(n, s) for n, _, s, _ in repo.trusted_rows()}
        assert rows == {("Rohit", "any"), ("Priya", "any"), ("Amit", "any")}

    def test_trust_number_among_names(self, client, repo):
        body = client.post(
            "/messages/incoming",
            json=_incoming("TRUST Rohit, +919876543210, Priya", chat=OPERATOR, sender=OPERATOR),
        ).json()
        assert body["reason"] == "console_trusted"
        rows = {(name, number, scope) for name, number, scope, _ in repo.trusted_rows()}
        assert rows == {
            ("Rohit", None, "any"),
            (None, "919876543210", "any"),
            ("Priya", None, "any"),
        }

    def test_trust_trailing_chat_id_scopes_everyone(self, client, repo):
        body = client.post(
            "/messages/incoming",
            json=_incoming("TRUST Rohit, Priya 120363fam@g.us", chat=OPERATOR, sender=OPERATOR),
        ).json()
        assert body["reason"] == "console_trusted"
        rows = {(n, s) for n, _, s, _ in repo.trusted_rows()}
        assert ("Rohit", "120363fam@g.us") in rows and ("Priya", "120363fam@g.us") in rows

    def test_untrust_multiple_users(self, client, repo):
        client.post("/messages/incoming", json=_incoming("TRUST Rohit, Priya", chat=OPERATOR, sender=OPERATOR))
        body = client.post(
            "/messages/incoming",
            json=_incoming("untrust rohit, priya", chat=OPERATOR, sender=OPERATOR),
        ).json()
        assert body["reason"] == "console_untrusted" and "Removed 2" in body["reply"]
        assert repo.trusted_rows() == []

    def test_trust_match_on_declined_chat_hints_allow(self, client, repo):
        """Ved's sequence: message → NO (decline) → TRUST the name. The chat
        stays silent (only ALLOW revives a refusal), but the operator is told
        exactly how to revive instead of getting dead silence."""
        client.post("/messages/incoming", json=_incoming("Hello", chat=UNKNOWN_GROUP, sender=UNKNOWN_PRIVATE, sender_name="Rohit"))
        client.post("/messages/incoming", json=_incoming("no", chat=OPERATOR, sender=OPERATOR))
        assert repo.has_active_consent(UNKNOWN_GROUP) is False
        client.post("/messages/incoming", json=_incoming("TRUST Rohit", chat=OPERATOR, sender=OPERATOR))
        body = client.post("/messages/incoming", json=_incoming("anyone?", chat=UNKNOWN_GROUP, sender=UNKNOWN_PRIVATE, sender_name="Rohit")).json()
        assert body["reason"] == "no_consent"
        assert body["self_prompt"] and "DECLINED" in body["self_prompt"] and "ALLOW" in body["self_prompt"]

    def test_names_keep_their_casing(self, client, repo):
        client.post("/messages/incoming", json=_incoming("TRUST Rohit Sharma", chat=OPERATOR, sender=OPERATOR))
        assert repo.trusted_rows() == [("Rohit Sharma", None, "any", None)]

    def test_trust_by_number_grants_lid_sender(self, client, repo):
        """The gateway resolved the temp person's number via group metadata —
        a @lid sender still gets auto-approved by the trusted number."""
        client.post("/messages/incoming", json=_incoming("TRUST +919876543210", chat=OPERATOR, sender=OPERATOR))
        body = client.post(
            "/messages/incoming",
            json={
                "group_id": UNKNOWN_GROUP,
                "sender": "555lid@lid",
                "text": "Hello from the new one",
                "timestamp": datetime.now().astimezone().isoformat(),
                "sender_name": "Some Nickname",
                "sender_number": "919876543210",
            },
        ).json()
        assert body["reason"] == "ok"  # granted AND handled normally
        assert body["self_prompt"] and "919876543210" in body["self_prompt"]
        assert repo.has_active_consent(UNKNOWN_GROUP) is True


class TestContextNotes:
    def _stub_model(self, monkeypatch, output: AgentOutput | callable):
        stub = output if callable(output) else (lambda **kw: output)
        monkeypatch.setattr(agent_main.gemini, "decide_and_extract", stub)

    def test_remember_and_forget_round_trip(self, client, repo):
        body = client.post(
            "/messages/incoming",
            json=_incoming(f"REMEMBER {UNKNOWN_GROUP} Papa is dieting — no fried stuff", chat=OPERATOR, sender=OPERATOR),
        ).json()
        assert body["reason"] == "console_context_noted"
        assert repo.context_notes(UNKNOWN_GROUP, 5) == ["Papa is dieting — no fried stuff"]

        body = client.post(
            "/messages/incoming",
            json=_incoming(f"FORGET {UNKNOWN_GROUP}", chat=OPERATOR, sender=OPERATOR),
        ).json()
        assert body["reason"] == "console_context_forgotten"
        assert repo.context_notes(UNKNOWN_GROUP, 5) == []

    def test_notes_are_injected_into_generation(self, client, monkeypatch, repo):
        repo.add_context_note(RUNNING_TEST_GROUP, "papa dieting", 7)
        seen: dict = {}

        def fake(**kwargs):
            seen["chat_context"] = kwargs.get("chat_context")
            return AgentOutput(reply="ok", meals=[], suggested_dishes=[], selected_dish=None, is_food_related=False)

        self._stub_model(monkeypatch, fake)
        client.post("/messages/incoming", json=_incoming("kya banau?"))
        assert "papa dieting" in seen["chat_context"]

    def test_needs_context_pauses_and_asks_once(self, client, monkeypatch, repo):
        self._stub_model(monkeypatch, AgentOutput(
            reply="haan bhai", meals=[], suggested_dishes=[], selected_dish=None,
            is_food_related=False, needs_context=True,
        ))
        first = client.post("/messages/incoming", json=_incoming("wo wala plan kya tha?")).json()
        assert first["reason"] == "waiting_for_context" and first["reply"] is None
        assert "REMEMBER" in first["self_prompt"]

        # Rate-limited: the next needs_context message replies normally.
        second = client.post("/messages/incoming", json=_incoming("bata na")).json()
        assert second["reason"] == "ok" and second["reply"] == "haan bhai"
        assert second["self_prompt"] is None

    def test_existing_context_means_no_pause(self, client, monkeypatch, repo):
        repo.add_context_note(RUNNING_TEST_GROUP, "exam season", 7)
        self._stub_model(monkeypatch, AgentOutput(
            reply="all the best!", meals=[], suggested_dishes=[], selected_dish=None,
            is_food_related=False, needs_context=True,  # spurious: notes exist
        ))
        body = client.post("/messages/incoming", json=_incoming("kaisa chal raha")).json()
        assert body["reason"] == "ok" and body["reply"] == "all the best!"

    def test_remember_resumes_a_pending_message(self, client, monkeypatch, repo):
        # An unanswered incoming sits in the chat (consent paused it earlier).
        client.post("/messages/incoming", json=_incoming("Hello??", chat=UNKNOWN_GROUP))
        repo.grant_consent(UNKNOWN_GROUP, ttl_hours=1)  # approved, but no deliver ran

        def fake(**kwargs):
            return AgentOutput(reply="aa gaya!", meals=[], suggested_dishes=[], selected_dish=None, is_food_related=False)

        self._stub_model(monkeypatch, fake)
        body = client.post(
            "/messages/incoming",
            json=_incoming(f"REMEMBER {UNKNOWN_GROUP} testing resume", chat=OPERATOR, sender=OPERATOR),
        ).json()
        assert body["deliver"] == {"chat_id": UNKNOWN_GROUP, "text": "aa gaya!"}

    def test_remember_does_not_resume_when_already_answered(self, client, monkeypatch, repo):
        client.post("/messages/incoming", json=_incoming("Hello??", chat=UNKNOWN_GROUP))
        repo.grant_consent(UNKNOWN_GROUP, ttl_hours=1)
        # the operator already replied by hand
        client.post("/messages/outgoing", json={
            "group_id": UNKNOWN_GROUP, "text": "hello ji!", "source": "operator",
            "timestamp": datetime.now().astimezone().isoformat(),
        })
        body = client.post(
            "/messages/incoming",
            json=_incoming(f"REMEMBER {UNKNOWN_GROUP} testing", chat=OPERATOR, sender=OPERATOR),
        ).json()
        assert body["deliver"] is None and "Noted" in body["reply"]

    def test_global_notes_reach_every_chat(self, client, monkeypatch, repo):
        body = client.post(
            "/messages/incoming",
            json=_incoming("REMEMBER-GLOBAL the family is visiting Manali in October", chat=OPERATOR, sender=OPERATOR),
        ).json()
        assert body["reason"] == "console_context_noted"

        seen: dict = {}

        def fake(**kwargs):
            seen["chat_context"] = kwargs.get("chat_context")
            return AgentOutput(reply="ok", meals=[], suggested_dishes=[], selected_dish=None, is_food_related=False)

        monkeypatch.setattr(agent_main.gemini, "decide_and_extract", fake)
        client.post("/messages/incoming", json=_incoming("hii"))
        assert any(n.startswith("GLOBAL:") and "Manali" in n for n in seen["chat_context"])

    def test_forget_global(self, client, repo):
        client.post("/messages/incoming", json=_incoming("REMEMBER-GLOBAL test note", chat=OPERATOR, sender=OPERATOR))
        body = client.post(
            "/messages/incoming",
            json=_incoming("FORGET-GLOBAL", chat=OPERATOR, sender=OPERATOR),
        ).json()
        assert body["reason"] == "console_context_forgotten"
        assert repo.context_chats() == [] or "__global__" not in repo.context_chats()

    def test_resume_waits_when_the_agent_just_replied(self, client, monkeypatch, repo):
        """Ved's spacing point: an agent reply seconds before the pending
        message must not produce a back-to-back robot burst on REMEMBER."""
        client.post("/messages/incoming", json=_incoming("Hello??", chat=UNKNOWN_GROUP))
        repo.grant_consent(UNKNOWN_GROUP, ttl_hours=1)
        # the agent replied a minute ago (to some earlier turn), then the
        # person's needs-context message is the last unanswered one
        with connect(TEST_DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO messages (group_id, sender, text, direction, source, created_at) "
                "VALUES (%s, 'self', 'poha khao', 'out', 'agent', now() - interval '1 minute')",
                (UNKNOWN_GROUP,),
            )
        body = client.post(
            "/messages/incoming",
            json=_incoming(f"REMEMBER {UNKNOWN_GROUP} spacing test", chat=OPERATOR, sender=OPERATOR),
        ).json()
        assert body["deliver"] is None
        assert "next message" in body["reply"]


class TestDistillerRound:
    def test_full_distill_round_end_to_end(self, client, monkeypatch, repo):
        from agent.app.gemini_client import DistillOutput, DistilledVoice

        # operator wrote messages → counter grows → chat becomes due
        client.post("/messages/outgoing", json={
            "group_id": UNKNOWN_GROUP, "text": "mast tha yaar", "source": "operator",
            "timestamp": datetime.now().astimezone().isoformat(),
        })
        for _ in range(100):
            repo.bump_distill_counter(UNKNOWN_GROUP)
        assert repo.chats_due_for_distill(100, 6) == [UNKNOWN_GROUP]

        canned = DistillOutput(
            voice=DistilledVoice(length="short", tone="warm"),
            facts=["papa dieting this month"],
        )
        seen: dict = {}
        monkeypatch.setattr(agent_main.gemini, "distill", lambda samples, lines: (seen.update(samples=samples, lines=lines) or canned))

        agent_main._run_distill(UNKNOWN_GROUP)

        assert any("mast tha yaar" in line for line in seen["samples"])
        assert repo.style_profile(UNKNOWN_GROUP) is not None and "tone" in repo.style_profile(UNKNOWN_GROUP)
        assert "papa dieting this month" in repo.active_facts(UNKNOWN_GROUP, 5)
        assert repo.chats_due_for_distill(100, 6) == []  # counter reset, stamped

    def test_distill_round_raises_and_loop_catches(self, client, monkeypatch, repo):
        """Contract: the profiler raises on model failure; the background loop
        is the one that swallows it (one round failing never stops profiling)."""
        client.post("/messages/outgoing", json={
            "group_id": UNKNOWN_GROUP, "text": "material", "source": "operator",
            "timestamp": datetime.now().astimezone().isoformat(),
        })

        def boom(samples, lines):
            raise RuntimeError("gemini down")

        monkeypatch.setattr(agent_main.gemini, "distill", boom)
        with pytest.raises(RuntimeError):
            agent_main._run_distill(UNKNOWN_GROUP)


class TestDistillerRound:
    def test_full_distill_round_end_to_end(self, client, monkeypatch, repo):
        from agent.app.gemini_client import DistillOutput, DistilledVoice

        # operator wrote messages → counter grows → chat becomes due
        client.post("/messages/outgoing", json={
            "group_id": UNKNOWN_GROUP, "text": "mast tha yaar", "source": "operator",
            "timestamp": datetime.now().astimezone().isoformat(),
        })
        for _ in range(100):
            repo.bump_distill_counter(UNKNOWN_GROUP)
        assert repo.chats_due_for_distill(100, 6) == [UNKNOWN_GROUP]

        canned = DistillOutput(
            voice=DistilledVoice(length="short", tone="warm"),
            facts=["papa dieting this month"],
        )
        seen: dict = {}
        monkeypatch.setattr(agent_main.gemini, "distill", lambda samples, lines: (seen.update(samples=samples, lines=lines) or canned))

        agent_main._run_distill(UNKNOWN_GROUP)

        assert any("mast tha yaar" in line for line in seen["samples"])
        assert repo.style_profile(UNKNOWN_GROUP) is not None and "tone" in repo.style_profile(UNKNOWN_GROUP)
        assert "papa dieting this month" in repo.active_facts(UNKNOWN_GROUP, 5)
        assert repo.chats_due_for_distill(100, 6) == []  # counter reset, stamped

    def test_distill_round_raises_and_loop_catches(self, client, monkeypatch, repo):
        """Contract: the profiler raises on model failure; the background loop
        is the one that swallows it (one round failing never stops profiling)."""
        client.post("/messages/outgoing", json={
            "group_id": UNKNOWN_GROUP, "text": "material", "source": "operator",
            "timestamp": datetime.now().astimezone().isoformat(),
        })

        def boom(samples, lines):
            raise RuntimeError("gemini down")

        monkeypatch.setattr(agent_main.gemini, "distill", boom)
        with pytest.raises(RuntimeError):
            agent_main._run_distill(UNKNOWN_GROUP)


class TestMediaEndpoint:
    """Images/stickers/gif frames: same consent gates, model SEES the media."""

    def _png(self) -> str:
        # 8x8 red PNG
        return (
            "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAAF0lEQVR4nGP8z8"
            "DAwMDAxMDAwMDAxAAqARmkD0VJAAAAAElFTkSuQmCC"
        )

    def _media(self, **overrides) -> dict:
        payload = {
            "group_id": RUNNING_TEST_GROUP,
            "sender": HUMAN,
            "text_placeholder": None,
            "timestamp": datetime.now().astimezone().isoformat(),
            "kind": "image",
            "mimetype": "image/png",
            "media_base64": self._png(),
            "caption": "look at this",
            "sender_name": "Ved",
        }
        payload.pop("text_placeholder")
        payload.update(overrides)
        return payload

    def test_captioned_image_gets_a_reply(self, client):
        body = client.post("/messages/media", json=self._media()).json()
        assert body["reason"] == "ok" and body["reply"].startswith("[dry-run]")

    def test_media_consent_gates_before_any_processing(self, client, repo):
        body = client.post("/messages/media", json=self._media(group_id=UNKNOWN_GROUP)).json()
        assert body["reason"] == "consent_requested"  # no AI call, just the ask
        # stored so context exists when consent comes
        assert any(m.text.startswith("[image]") for m in repo.recent_messages(UNKNOWN_GROUP, 5))

    def test_oversized_media_rejected(self, client):
        big = {"media_base64": base64.b64encode(b"x" * (11 * 1024 * 1024)).decode()}
        body = client.post("/messages/media", json=self._media(**big)).json()
        assert body["reason"] == "media_too_large"

    def test_disallowed_mimetype_rejected(self, client):
        body = client.post("/messages/media", json=self._media(mimetype="application/pdf")).json()
        assert body["reason"] == "media_rejected"

    def test_corrupt_base64_rejected(self, client):
        body = client.post("/messages/media", json=self._media(media_base64="!!not-base64!!")).json()
        assert body["reason"] == "media_corrupt"

    def test_media_stored_with_placeholder_when_no_caption(self, client, repo):
        body = client.post(
            "/messages/media",
            json=self._media(caption=None, sender="555lid@lid", sender_name="Nick"),
        ).json()
        assert body["reason"] == "ok"
        stored = repo.recent_messages(RUNNING_TEST_GROUP, 5)[0]
        assert stored.text == "[image]" and stored.source == "agent"

    def test_vision_uses_the_same_persona_contract(self, client, monkeypatch):
        seen: dict = {}

        def fake_see(image_bytes, mimetype, prompt_text):
            seen["bytes"] = image_bytes
            seen["mime"] = mimetype
            seen["prompt"] = prompt_text
            return AgentOutput(reply="nice pic!", meals=[], suggested_dishes=[], selected_dish=None, is_food_related=False)

        monkeypatch.setattr(agent_main.gemini, "see_and_reply", fake_see)
        body = client.post("/messages/media", json=self._media()).json()
        assert body["reply"] == "nice pic!"
        assert seen["bytes"].startswith(b"\x89PNG")
        assert seen["mime"] == "image/png"
        assert "CHAT TYPE" in seen["prompt"]  # the full persona context rides along

    def test_stop_with_chat_id_stops_only_that_chat(self, client, repo):
        """Ved hit this: 'stop <jid>' fell through to stopping ALL chats."""
        other = "120363other@g.us"
        for chat in (UNKNOWN_GROUP, other):
            client.post("/messages/incoming", json=_incoming("Hello", chat=chat))
            client.post("/messages/incoming", json=_incoming("yes", chat=OPERATOR, sender=OPERATOR))
        body = client.post(
            "/messages/incoming",
            json=_incoming(f"stop {UNKNOWN_GROUP}", chat=OPERATOR, sender=OPERATOR),
        ).json()
        assert body["reason"] == "console_revoked"
        assert repo.has_active_consent(UNKNOWN_GROUP) is False
        assert repo.has_active_consent(other) is True

    def test_stop_with_unknown_chat_id_is_helpful(self, client, repo):
        body = client.post(
            "/messages/incoming",
            json=_incoming("stop 999nothere@g.us", chat=OPERATOR, sender=OPERATOR),
        ).json()
        assert body["reason"] == "console_unknown_chat"
        assert repo.active_consents() == []

    def test_remembered_consent_silently_regrants_no_ask(self, client, repo):
        """Sticky consent: after one YES, expiry re-grants silently and the
        message is handled normally — never a repeat consent ask."""
        client.post("/messages/incoming", json=_incoming("Hello", chat=UNKNOWN_GROUP))
        client.post("/messages/incoming", json=_incoming("yes", chat=OPERATOR, sender=OPERATOR))
        assert repo.has_active_consent(UNKNOWN_GROUP) is True
        with connect(TEST_DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_consents SET expires_at = now() - interval '1 minute' "
                "WHERE chat_id = %s",
                (UNKNOWN_GROUP,),
            )
        body = client.post("/messages/incoming", json=_incoming("anyone here?", chat=UNKNOWN_GROUP)).json()
        assert body["reason"] == "ok" and body["reply"].startswith("[dry-run]")
        assert repo.has_active_consent(UNKNOWN_GROUP) is True  # fresh 24h row

    def test_stop_removes_memory_so_next_message_asks_again(self, client, repo):
        client.post("/messages/incoming", json=_incoming("Hello", chat=UNKNOWN_GROUP))
        client.post("/messages/incoming", json=_incoming("yes", chat=OPERATOR, sender=OPERATOR))
        client.post("/messages/incoming", json=_incoming(f"stop {UNKNOWN_GROUP}", chat=OPERATOR, sender=OPERATOR))
        body = client.post("/messages/incoming", json=_incoming("anyone?", chat=UNKNOWN_GROUP)).json()
        assert body["reason"] == "consent_requested"  # asks again, never silent re-grant


class TestOperatorConsole:
    def test_commands_via_operator_dm(self, client):
        body = client.post("/messages/incoming", json=_incoming("LIST", chat=OPERATOR, sender=OPERATOR)).json()
        assert body["reason"] == "console_empty" and "Nothing tracked" in body["reply"]

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
