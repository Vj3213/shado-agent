"""OpenRouter fallback: cross-provider last resort when all Google models fail.

Uses OpenRouter's OpenAI-compatible chat completions with free models
(model ids ending in ':free'). The same persona prompt and structured
AgentOutput contract are reused, so the agent logic doesn't care which
provider served the reply.
"""

from __future__ import annotations

from datetime import date, datetime

import httpx
from pydantic import ValidationError

from agent.app.gemini_client import AgentOutput
from agent.app.persona import SYSTEM_PROMPT, build_user_prompt
from storage.models import DishRow, MealEntry, MessageRecord, SuggestionRow

API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODELS_URL = "https://openrouter.ai/api/v1/models"


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        models: list[str],
        timeout_s: int = 15,
    ):
        self._key = api_key
        self._models = list(models)
        self._timeout = timeout_s

    def _live_free_models(self) -> list[str]:
        """OpenRouter's free catalog rotates — fetch the current ids."""
        try:
            response = httpx.get(MODELS_URL, timeout=10)
            response.raise_for_status()
            return [m["id"] for m in response.json()["data"] if m["id"].endswith(":free")]
        except Exception as error:
            print(f"[openrouter] could not refresh free-model list: {error!r}")
            return []

    def decide_and_extract(
        self,
        history: list[MessageRecord],
        meals_today: list[MealEntry],
        eaten_recent: list[MealEntry],
        suggestions: list[SuggestionRow],
        pool: list[DishRow],
        peer_bot_sender: str | None,
        incoming_text: str,
        today: date,
        now: datetime,
        chat_id: str = "",
    ) -> AgentOutput:
        user_prompt = build_user_prompt(
            history, meals_today, eaten_recent, suggestions, pool,
            peer_bot_sender, today, incoming_text, now, chat_id,
        )
        payload = {
            "model": None,  # filled per attempt
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 1.0,
        }
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/vedjha/whatsapp-meal-agent",
            "X-Title": "WhatsApp Meal Agent",
        }

        last_error: Exception | None = None
        for model in self._models:
            try:
                response = httpx.post(
                    API_URL,
                    headers=headers,
                    json={**payload, "model": model},
                    timeout=self._timeout,
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                return self._parse(content)
            except Exception as error:
                last_error = error
                print(f"[openrouter] '{model}' failed ({type(error).__name__}) — trying next...")
        assert last_error is not None
        return self._try_live_free_models(payload, headers, exclude=set(self._models), last_error=last_error)

    def _try_live_free_models(
        self, payload: dict, headers: dict, exclude: set[str], last_error: Exception
    ) -> AgentOutput:
        """All configured ids failed — most likely a retired model id.
        Pick up to 2 fresh free models straight from OpenRouter's live catalog."""
        fresh = [m for m in self._live_free_models() if m not in exclude][:2]
        if not fresh:
            raise last_error
        for model in fresh:
            try:
                print(f"[openrouter] configured ids stale — trying live free model '{model}'...")
                response = httpx.post(
                    API_URL,
                    headers=headers,
                    json={**payload, "model": model},
                    timeout=self._timeout,
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                return self._parse(content)
            except Exception as error:
                last_error = error
                print(f"[openrouter] '{model}' failed ({type(error).__name__})")
        raise last_error

    @staticmethod
    def _parse(content: str) -> AgentOutput:
        try:
            return AgentOutput.model_validate_json(content)
        except ValidationError:
            # Strip markdown fences some models add despite json mode.
            cleaned = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            return AgentOutput.model_validate_json(cleaned)
