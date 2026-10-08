"""Agent-module logic tests: triggers, loop-cap policy, prompt assembly.

All pure logic (no network, no real Gemini). The trap from AGENTS.md applies:
AGENT_DRY_RUN is unset in conftest, so nothing here silently short-circuits —
each module decides its own dry-run behavior explicitly."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import pytest

from agent.app.persona import (
    SYSTEM_PROMPT,
    build_conversation_block,
    build_food_context,
    build_style_block,
    build_time_block,
    build_user_prompt,
)
from agent.app.policy import LoopCapPolicy
from agent.app.triggers import TriggerConfig, is_followup, matches_trigger
from storage.models import BotLoopState, DishRow, MealEntry, MessageRecord, SuggestionRow

TRIGGERS = TriggerConfig(
    prefixes=("ved:", "@ved"),
    keywords=("khana", "breakfast"),
    patterns=(re.compile(r"kha+n[ei]?"),),
)

PEER = "919999000111@s.whatsapp.net"
HUMAN = "918888877777@s.whatsapp.net"


def _msg(text: str, direction: str, at: datetime, sender: str = HUMAN) -> MessageRecord:
    return MessageRecord(
        group_id="g", sender=sender, text=text, direction=direction,  # type: ignore[arg-type]
        created_at=at,
    )


def _history(entries: list[tuple[str, str, int, str]]) -> list[MessageRecord]:
    base = datetime.now(timezone.utc)
    return [
        _msg(text, direction, base - timedelta(seconds=ago), sender)
        for (text, direction, ago, sender) in entries
    ]


class TestTriggers:
    cfg = TRIGGERS

    def test_prefix_and_keyword_and_pattern_hits(self):
        assert matches_trigger("ved: kya banau?", self.cfg).kind == "prefix"
        assert matches_trigger("@ved bhookh lagi", self.cfg).kind == "prefix"
        assert matches_trigger("khana kya banau", self.cfg).kind == "keyword"
        assert matches_trigger("MAIN KHANA CHAHTA HU", self.cfg).kind == "keyword"
        assert matches_trigger("khaana banaya", self.cfg).kind == "pattern"

    def test_non_trigger_passes_through(self):
        assert matches_trigger("good morning all", self.cfg) is None
        assert matches_trigger("", self.cfg) is None

    def test_mention_inside_sentence_is_prefix_hit(self):
        # "prefix.lower() in lowered" means @ved anywhere counts — intended so
        # "hey @ved khana?" works; verified here so the semantics stay pinned.
        assert matches_trigger("hey @ved khana?", self.cfg).kind == "prefix"


class TestFollowup:
    def test_keeps_chatting_within_window_with_same_person(self):
        history = _history([
            ("bhookh lagi", "in", 40, HUMAN),
            ("poha khao", "out", 20, HUMAN),   # bot's most recent message
            ("aur kuch?", "in", 10, HUMAN),    # same person now
        ])
        assert is_followup(history, HUMAN, window_seconds=300) is True

    def test_window_expiry_stops_followup(self):
        history = _history([
            ("hi", "in", 400, HUMAN),
            ("hello!", "out", 390, HUMAN),
            ("still there?", "in", 380, HUMAN),
        ])
        assert is_followup(history, HUMAN, window_seconds=300) is False


class TestLoopCapPolicy:
    class _Repo:
        def __init__(self, turns: int | None = None):
            self.state = (
                BotLoopState(group_id="g", peer_sender=PEER, consecutive_turns=turns, last_reset_at=None)
                if turns is not None else None
            )
            self.reset_calls: list[str] = []
            self.increment_calls: list[tuple[str, str, int]] = []

        def get_loop_state(self, group_id, peer_sender):
            return self.state

        def reset_loop_state(self, group_id, peer_sender):
            self.reset_calls.append(group_id)
            self.state = BotLoopState("g", PEER, 0, None)

        def try_increment_bot_turns(self, group_id, peer_sender, cap):
            self.increment_calls.append((group_id, peer_sender, cap))
            return 1

    def test_peer_detected_only_when_configured(self):
        both = LoopCapPolicy(self._Repo(), PEER, max_turns=2)
        dormant = LoopCapPolicy(self._Repo(), None, max_turns=2)
        assert both.is_peer_bot(PEER) is True
        assert dormant.is_peer_bot(PEER) is False  # PEER_BOT_SENDER unset => logic dormant

    def test_cap_blocks_and_human_resets(self):
        repo = self._Repo(turns=2)
        policy = LoopCapPolicy(repo, PEER, max_turns=2)
        assert policy.cap_reached("g") is True
        policy.note_human_activity("g")
        assert repo.reset_calls == ["g"]
        assert policy.cap_reached("g") is False

    def test_count_reply_checks_with_storage(self):
        repo = self._Repo()
        policy = LoopCapPolicy(repo, PEER, max_turns=2)
        assert policy.count_reply_to_peer("g") is True
        assert repo.increment_calls == [("g", PEER, 2)]


class TestPersona:
    def test_time_block_uses_real_clock_and_names_season(self):
        block = build_time_block(datetime(2026, 9, 28, 1, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))))
        assert "late night" in block and "2026" in block and "season" in block

    def test_conversation_block_labels_peer_bot_and_self(self):
        history = [
            _msg("suggestion: dal", "in", datetime.now(timezone.utc), sender=PEER),
            _msg("poha khao", "out", datetime.now(timezone.utc), sender=HUMAN),
        ]
        lines = build_conversation_block(history, PEER).splitlines()
        assert lines[0].startswith("Other meal-suggestion app:")
        assert lines[1].startswith("You (earlier):")

    def test_conversation_block_marks_hand_typed_messages(self):
        # The model needs to tell "the human already answered this" apart from
        # the agent's own earlier messages (consent-reply decision).
        history = [
            _msg("Hello", "in", datetime.now(timezone.utc)),
            MessageRecord(
                group_id="g", sender=HUMAN, text="hello ji", direction="out",  # type: ignore[arg-type]
                created_at=datetime.now(timezone.utc), source="operator",
            ),
        ]
        lines = build_conversation_block(history, None).splitlines()
        assert lines[1].startswith("You (earlier, typed by hand):")

    def test_food_context_hides_suggestions_when_empty(self):
        ctx = build_food_context(date(2026, 9, 28), [], [], [], [])
        assert "nothing logged yet" in ctx and "none yet" in ctx and "(database empty)" in ctx

    def test_style_block_reflects_exemplars(self):
        assert build_style_block([], hand_typed_in_chat=False) == "STYLE EXAMPLES: none yet — write naturally."
        block = build_style_block(["mast tha yaar"])
        assert '"mast tha yaar"' in block

    def test_style_block_points_at_hand_typed_messages_when_no_exemplars(self):
        # No out-of-window samples, but the operator's messages are visible in
        # the conversation — the mirror instruction must survive.
        block = build_style_block([], hand_typed_in_chat=True)
        assert "typed by hand" in block and "mirror" in block.lower()

    def test_voice_and_facts_blocks(self):
        from agent.app.persona import build_facts_block, build_voice_block

        assert build_voice_block(None) == "VOICE PROFILE: none distilled yet — learn from STYLE EXAMPLES."
        assert "trust it" in build_voice_block("• tone: warm")
        assert build_facts_block(None) == "FACTS: none extracted yet for this chat."
        facts_block = build_facts_block(["Priya exam Oct 12"])
        assert "Priya exam Oct 12" in facts_block and "treat as true" in facts_block

    def test_context_block_anchors_note_pronouns_to_the_persona(self):
        from agent.app.persona import build_context_block

        block = build_context_block(["its my birthday today"])
        # The model must read note pronouns as the REPLOYING persona's own life,
        # never as the chat partner's (the "tera birthday" misfire).
        assert '"I/my/mera/mere"' in block and "YOU" in block
        assert "birthday" in block  # the worked example is pinned 

    def test_prompt_sections_present(self):
        now = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
        prompt = build_user_prompt(
            history=[], meals_today=[], eaten_recent=[],
            suggestions=[SuggestionRow(dish="poha", status="pending", created_at=now)],
            pool=[DishRow(name="poha", meal_types="breakfast")],
            peer_bot_sender=PEER, today=date(2026, 9, 28),
            incoming_text="kya banau?", now=now, chat_id="120363x@g.us",
            operator_examples=["mast hai"], allowed_reactions=["👍"],
        )
        assert "CURRENT TIME" in prompt
        assert "CHAT TYPE: group chat" in prompt
        assert "ALLOWED REACTIONS: 👍" in prompt
        assert '"mast hai"' in prompt
        assert "poha (pending)" in prompt
        assert "RECENT GROUP CHAT" in prompt

    def test_private_chat_is_labeled(self):
        now = datetime.now(timezone.utc)
        prompt = build_user_prompt(
            history=[], meals_today=[], eaten_recent=[], suggestions=[],
            pool=[], peer_bot_sender=None, today=date.today(),
            incoming_text="hello ji", now=now, chat_id="9198888@s.whatsapp.net",
        )
        assert "PRIVATE 1:1 chat" in prompt
