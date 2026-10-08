"""Gemini fallback-chain tests.

No real network: we monkeypatch GeminiClient._call_model to simulate model
behavior (capacity errors, auth errors, success). This pins the chain rules
from AGENTS.md: 503/504/429/404 fall through; 401/403 abort; OpenRouter is
the last resort; DRY_RUN short-circuits everything without keys."""

from __future__ import annotations

from datetime import date, datetime, timezone

import httpx
import pytest
from google.genai import errors

from agent.app.gemini_client import AgentOutput, ExtractedMeal, GeminiClient
from storage.models import DishRow, MealEntry, MessageRecord, SuggestionRow

MODEL_ARGS = dict(
    api_key="fake-key",
    primary_model="gemini-3.5-flash-lite",
    fallback_models=["gemini-3.6-flash", "gemini-3.8-flash"],
)


def _api_error(status: int) -> errors.APIError:
    return errors.APIError(code=status, response_json={}, response=None)


def _output(reply: str = "hi") -> AgentOutput:
    return AgentOutput(reply=reply, meals=[], suggested_dishes=[], selected_dish=None, is_food_related=True)


def _std_args() -> dict:
    return dict(history=[], meals_today=[], eaten_recent=[], suggestions=[],
                pool=[], peer_bot_sender=None, incoming_text="hello",
                today=date.today(), now=datetime.now(timezone.utc))


def _client(monkeypatch_or_none=None, **overrides) -> GeminiClient:
    """A real (non-dry-run) client whose SDK call is replaced by a stub
    _call_model — chain logic runs, no network happens."""
    kwargs = dict(MODEL_ARGS) | overrides
    client = GeminiClient(**kwargs)
    return client


def _stub(client: GeminiClient, monkeypatch, fake) -> None:
    monkeypatch.setattr(client, "_call_model", fake)


class TestChain:
    def test_primary_serves_without_fallback(self, monkeypatch):
        client = _client(last_resort=None)
        calls: list[str] = []

        def fake(model, *a, **k):
            calls.append(model)
            return _output(f"from {model}")

        _stub(client, monkeypatch, fake)
        out = client.decide_and_extract(**_std_args())
        assert out.reply == "from gemini-3.5-flash-lite"
        assert calls == ["gemini-3.5-flash-lite"]

    def test_capacity_error_falls_to_next_model(self, monkeypatch):
        client = _client(last_resort=None)
        attempts: list[str] = []

        def fake(model, *a, **k):
            attempts.append(model)
            if model == "gemini-3.5-flash-lite":
                raise _api_error(503)
            return _output(f"served by {model}")

        _stub(client, monkeypatch, fake)
        assert client.decide_and_extract(**_std_args()).reply == "served by gemini-3.6-flash"
        assert attempts == ["gemini-3.5-flash-lite", "gemini-3.6-flash"]

    def test_auth_error_aborts_immediately(self, monkeypatch):
        client = _client(last_resort=None)
        attempts: list[str] = []

        def fake(model, *a, **k):
            attempts.append(model)
            raise _api_error(401)

        _stub(client, monkeypatch, fake)
        with pytest.raises(errors.APIError):
            client.decide_and_extract(**_std_args())
        assert attempts == ["gemini-3.5-flash-lite"]  # no fallback on auth failure

    def test_exhausted_chain_hands_off_to_openrouter(self, monkeypatch):
        import agent.app.gemini_client as gc

        handed_off: dict = {}

        class FakeOR:
            def decide_and_extract(self, *args, **kwargs):
                handed_off["called"] = True
                return _output("openrouter reply")

        client = _client(last_resort=FakeOR())
        _stub(client, monkeypatch, lambda m, *a, **k: (_ for _ in ()).throw(_api_error(503)))
        monkeypatch.setattr(gc.time, "sleep", lambda s: None)
        assert client.decide_and_extract(**_std_args()).reply == "openrouter reply"
        assert handed_off["called"] is True

    def test_no_last_resort_reraises_last_error(self, monkeypatch):
        import agent.app.gemini_client as gc

        client = _client(last_resort=None)
        _stub(client, monkeypatch, lambda m, *a, **k: (_ for _ in ()).throw(_api_error(429)))
        monkeypatch.setattr(gc.time, "sleep", lambda s: None)
        with pytest.raises(Exception):
            client.decide_and_extract(**_std_args())

    def test_retry_rounds_collapse_to_one_when_openrouter_exists(self):
        assert GeminiClient(last_resort=object(), retry_rounds=2, **MODEL_ARGS)._rounds == 1
        assert GeminiClient(last_resort=None, retry_rounds=2, **MODEL_ARGS)._rounds == 2

    def test_model_chain_order(self):
        client = _client(last_resort=None)
        assert client.model_chain == ["gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.8-flash"]


class TestDryRun:
    def test_dry_run_works_without_keys_and_does_not_build_sdk_client(self):
        client = GeminiClient(api_key="", primary_model="x", fallback_models=[], dry_run=True)
        out = client.decide_and_extract(**_std_args())
        assert out.reply and out.reply.startswith("[dry-run]")
        assert not hasattr(client, "_client")

    def test_read_model_output_shape(self):
        out = _output()
        assert out.meals == [] and out.suggested_dishes == [] and out.is_food_related is True
        meal = ExtractedMeal(item="poha", meal_type="breakfast")
        assert meal.meal_type == "breakfast"

    def test_poll_contract_parses_and_defaults_to_none(self):
        # The poll field is additive: models that omit it stay valid.
        with_poll = AgentOutput.model_validate_json(
            '{"reply": "vote karo", "meals": [], "suggested_dishes": [], '
            '"selected_dish": null, "is_food_related": true, '
            '"poll": {"question": "Aaj kya banau?", "options": ["poha", "upma"]}}'
        )
        assert with_poll.poll is not None and with_poll.poll.options == ["poha", "upma"]
        assert _output().poll is None

    def test_user_would_react_defaults_to_false(self):
        # Models that omit the flag stay valid — and default to the
        # rate-limited reaction path.
        assert _output().user_would_react is False
        confident = AgentOutput.model_validate_json(
            '{"reply": "😂", "meals": [], "suggested_dishes": [], '
            '"selected_dish": null, "is_food_related": true, '
            '"react": "😂", "user_would_react": true}'
        )
        assert confident.user_would_react is True and confident.react == "😂"

    def test_needs_context_defaults_to_false(self):
        # Models that omit the field stay valid — pause only when declared.
        assert _output().needs_context is False


class TestDistiller:
    """The profiler agent (#2): VOICE + FACTS from message history."""

    def test_dry_run_returns_canned_profile_without_sdk(self):
        client = GeminiClient(api_key="", primary_model="x", fallback_models=[], dry_run=True)
        out = client.distill(["hello"], ["chat line"])
        assert out.facts and "[dry-run]" in out.facts[0]
        assert out.voice.length is not None
        assert not hasattr(client, "_client")

    def test_distill_calls_the_primary_model_with_both_inputs(self, monkeypatch):
        from agent.app.gemini_client import DistillOutput, DistilledVoice

        client = _client(last_resort=None)
        seen: dict = {}
        canned = DistillOutput(voice=DistilledVoice(tone="teasing"), facts=["trip next week"])

        def fake(model, samples, lines):
            seen["model"], seen["samples"], seen["lines"] = model, samples, lines
            return canned

        monkeypatch.setattr(client, "_call_distill", fake)
        out = client.distill(["ved: khana"], ["you: poha khao"])
        assert out is canned
        assert seen["model"] == "gemini-3.5-flash-lite"
        assert seen["samples"] == ["ved: khana"] and seen["lines"] == ["you: poha khao"]


# The layered-signature trap (AGENTS.md): persona, GeminiClient, OpenRouterClient
# and the shared prompt must all agree on the same parameter bundle. This test
# breaks the day someone edits one layer only.
def test_layered_signatures_stay_in_sync():
    import inspect

    from agent.app.openrouter_client import OpenRouterClient
    from agent.app.persona import build_user_prompt

    def params(sig) -> list[str]:
        return list(sig.parameters)

    gem = params(inspect.signature(GeminiClient.decide_and_extract))[1:]  # drop self
    opn = params(inspect.signature(OpenRouterClient.decide_and_extract))[1:]
    prompt = params(inspect.signature(build_user_prompt))

    # Both clients must agree exactly (same order, same names) — they call
    # each other positionally in the fallback hand-off.
    assert gem == opn, "GeminiClient and OpenRouterClient signatures diverged"

    # The prompt builder takes the same bundle; build_user_prompt orders
    # today/incoming_text differently today, so compare as sets and pin the
    # per-layer orders explicitly (any edit to one layer breaks these).
    assert set(gem) == set(prompt), "prompt layer is missing a shared parameter"
    assert gem[:16] == [
        "history", "meals_today", "eaten_recent", "suggestions", "pool",
        "peer_bot_sender", "incoming_text", "today", "now", "chat_id",
        "operator_examples", "allowed_reactions", "chat_reactions", "chat_context",
        "voice_profile", "facts",
    ]
    assert prompt[:16] == [
        "history", "meals_today", "eaten_recent", "suggestions", "pool",
        "peer_bot_sender", "today", "incoming_text", "now", "chat_id",
        "operator_examples", "allowed_reactions", "chat_reactions", "chat_context",
        "voice_profile", "facts",
    ]
