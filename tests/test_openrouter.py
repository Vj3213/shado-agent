"""OpenRouter fallback-client tests (all HTTP stubbed via monkeypatch).

Pins: JSON/fence parsing into the AgentOutput contract, configured-model
rotation, and the live free-catalog self-heal when every configured id fails."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import httpx
import pytest

import agent.app.openrouter_client as orc
from agent.app.gemini_client import AgentOutput
from agent.app.openrouter_client import OpenRouterClient


def _std_args() -> dict:
    return dict(history=[], meals_today=[], eaten_recent=[], suggestions=[],
                pool=[], peer_bot_sender=None, incoming_text="hello",
                today=date.today(), now=datetime.now(timezone.utc))


def _chat_response(content: str, status: int = 200) -> httpx.Response:
    # httpx >= 0.28: raise_for_status() needs the request attached, as it is on
    # real responses — fake ones must carry it too or the client sees RuntimeError.
    request = httpx.Request("POST", orc.API_URL)
    return httpx.Response(
        status, json={"choices": [{"message": {"content": content}}]}, request=request
    )


def _catalog_response(models: list[str]) -> httpx.Response:
    request = httpx.Request("GET", orc.MODELS_URL)
    return httpx.Response(200, json={"data": [{"id": m} for m in models]}, request=request)


class TestParse:
    def test_plain_json_parses(self):
        content = json.dumps({"reply": "poha khao", "meals": [], "suggested_dishes": [],
                              "selected_dish": None, "is_food_related": True, "react": None})
        out = OpenRouterClient._parse(content)
        assert isinstance(out, AgentOutput) and out.reply == "poha khao"

    def test_markdown_fences_are_stripped(self):
        content = '```json\n{"reply": "ok", "meals": [], "suggested_dishes": [], "selected_dish": null, "is_food_related": false, "react": null}\n```'
        out = OpenRouterClient._parse(content)
        assert out.reply == "ok"

    def test_garbage_raises(self):
        with pytest.raises(Exception):
            OpenRouterClient._parse("not json at all")


class TestRotation:
    def test_first_configured_model_wins(self, monkeypatch):
        posts: list[str] = []

        def fake_post(url, **kw):
            payload = kw["json"]
            posts.append(payload["model"])
            return _chat_response(json.dumps({"reply": "yes", "meals": [], "suggested_dishes": [],
                                              "selected_dish": None, "is_food_related": True}))

        monkeypatch.setattr(orc.httpx, "post", fake_post)
        out = OpenRouterClient("key", ["model-a:free", "model-b:free"]).decide_and_extract(**_std_args())
        assert out.reply == "yes" and posts == ["model-a:free"]

    def test_falls_through_to_next_configured_model(self, monkeypatch):
        posts: list[str] = []

        def fake_post(url, **kw):
            payload = kw["json"]
            posts.append(payload["model"])
            if payload["model"] == "stale:free":
                return _chat_response(json.dumps({"error": "retired"}), status=404)
            return _chat_response(json.dumps({"reply": "recovered", "meals": [], "suggested_dishes": [],
                                              "selected_dish": None, "is_food_related": True}))

        monkeypatch.setattr(orc.httpx, "post", fake_post)
        out = OpenRouterClient("key", ["stale:free", "fresh:free"]).decide_and_extract(**_std_args())
        assert out.reply == "recovered" and posts == ["stale:free", "fresh:free"]

    def test_all_configured_fail_triggers_live_catalog_self_heal(self, monkeypatch):
        posts: list[str] = []

        def fake_get(url, timeout=None):
            assert url == orc.MODELS_URL
            return _catalog_response(["live-1:free", "live-2:free", "live-3:free"])

        def fake_post(url, **kw):
            payload = kw["json"]
            posts.append(payload["model"])
            if payload["model"] in ("old-1:free", "old-2:free"):
                return _chat_response(json.dumps({"error": "bad"}), status=400)
            return _chat_response(json.dumps({"reply": "healed", "meals": [], "suggested_dishes": [],
                                              "selected_dish": None, "is_food_related": True}))

        monkeypatch.setattr(orc.httpx, "get", fake_get)
        monkeypatch.setattr(orc.httpx, "post", fake_post)
        out = OpenRouterClient("key", ["old-1:free", "old-2:free"]).decide_and_extract(**_std_args())
        assert out.reply == "healed"
        assert "live-1:free" in posts  # fresh catalog models were tried

    def test_everything_dead_raises_last_error(self, monkeypatch):
        def fake_get(url, timeout=None):
            return _catalog_response([])  # catalog empty

        def fake_post(url, **kw):
            payload = kw["json"]
            return _chat_response(json.dumps({"error": "down"}), status=502)

        monkeypatch.setattr(orc.httpx, "get", fake_get)
        monkeypatch.setattr(orc.httpx, "post", fake_post)
        with pytest.raises(httpx.HTTPStatusError):
            OpenRouterClient("key", ["old-1:free"]).decide_and_extract(**_std_args())

    def test_prompt_uses_shared_persona_bundle(self, monkeypatch):
        """OpenRouter must reuse the exact persona prompt builder (the same
        bundle as Gemini) — that's the platform/provider symmetry."""
        seen: dict = {}

        class RecordingClient(OpenRouterClient):
            pass

        real_build = orc.build_user_prompt

        def spy(*args, **kwargs):
            seen["called"] = True
            return real_build(*args, **kwargs)

        monkeypatch.setattr(orc, "build_user_prompt", spy)

        def fake_post(url, **kw):
            payload = kw["json"]
            messages = payload["messages"]
            seen["system"] = messages[0]["content"]
            seen["temperature"] = payload["temperature"]
            seen["json_mode"] = payload["response_format"]
            return _chat_response(json.dumps({"reply": "r", "meals": [], "suggested_dishes": [],
                                              "selected_dish": None, "is_food_related": False}))

        monkeypatch.setattr(orc.httpx, "post", fake_post)
        OpenRouterClient("key", ["m:free"]).decide_and_extract(**_std_args())
        assert seen["called"] and seen["system"].startswith("You are a warm")
        assert seen["json_mode"] == {"type": "json_object"}
