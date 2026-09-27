"""Gemini client with a structured-output contract and model fallback.

One call returns BOTH the reply text and any meals we should log.

Fallback chain (priority order):
  primary (GEMINI_MODEL) -> gemini.fallback_models from config/settings.json
  (overridable via GEMINI_FALLBACK_MODELS env var, comma-separated)

On capacity errors (429/5xx/404) the next model is tried after a short
pause; auth errors (401/403) abort immediately since no model change helps.
DRY_RUN mode lets you exercise the whole pipeline without an API key.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Literal

import httpx
from pydantic import BaseModel
from google import genai
from google.genai import errors, types

from agent.app.persona import SYSTEM_PROMPT, build_user_prompt
from storage.models import DishRow, MealEntry, MessageRecord, SuggestionRow


class ExtractedMeal(BaseModel):
    item: str
    meal_type: Literal["breakfast", "lunch", "dinner", "snack"]


class AgentOutput(BaseModel):
    reply: str | None
    meals: list[ExtractedMeal]
    suggested_dishes: list[str]
    selected_dish: str | None
    is_food_related: bool
    react: str | None = None


# Statuses where a different model may still work. Anything else
# (auth, malformed request) is not a capacity problem -> no fallback.
_FALLBACK_STATUSES = {404, 408, 429, 500, 502, 503, 504}


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        primary_model: str,
        fallback_models: list[str],
        dry_run: bool = False,
        retry_rounds: int = 2,
        last_resort=None,
    ):
        self._primary = primary_model
        self._fallbacks = list(fallback_models)
        self._dry_run = dry_run
        # When a cross-provider last resort exists, spend only one Gemini round
        # before handing off — the gateway's total wait budget stays bounded.
        self._rounds = 1 if last_resort is not None else max(1, retry_rounds)
        self._last_resort = last_resort
        if not dry_run:
            self._client = genai.Client(
                api_key=api_key,
                # The SDK default is 10 minutes per request — that would let one
                # hanging attempt stall the whole chain (and the gateway's wait).
                # Bound every attempt so the chain always finishes in time.
                http_options=types.HttpOptions(timeout=15_000),
            )

    @property
    def model_chain(self) -> list[str]:
        return [self._primary, *self._fallbacks]

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
        operator_examples: list[str] | None = None,
    ) -> AgentOutput:
        if self._dry_run:
            return AgentOutput(
                reply=f"[dry-run] Would reply to: '{incoming_text[:60]}'",
                meals=[ExtractedMeal(item="dry-run snack", meal_type="snack")],
                suggested_dishes=[],
                selected_dish=None,
                is_food_related=False,
            )

        last_error: Exception | None = None
        for round_no in range(self._rounds):
            for attempt, model in enumerate(self.model_chain):
                if attempt or round_no:
                    time.sleep(1.5)  # free-tier 503s are spikes; a beat helps
                try:
                    output = self._call_model(
                        model,
                        history,
                        meals_today,
                        eaten_recent,
                        suggestions,
                        pool,
                        peer_bot_sender,
                        incoming_text,
                        today,
                        now,
                        chat_id,
                        operator_examples,
                    )
                    if model != self._primary or round_no:
                        print(
                            f"[gemini] served by '{model}'"
                            + (f" (retry round {round_no + 1})" if round_no else "")
                        )
                    return output
                except (errors.APIError, httpx.HTTPError) as error:
                    if isinstance(error, httpx.TimeoutException):
                        last_error = error
                        print(f"[gemini] '{model}' timed out — trying next model...")
                        continue
                    status = getattr(error, "code", None)
                    if status in (401, 403):
                        raise  # key problem: no model change will help
                    if status not in _FALLBACK_STATUSES:
                        raise  # not a capacity error; don't mask it
                    last_error = error
                    print(f"[gemini] '{model}' unavailable (HTTP {status}) — trying next model...")
            if round_no < self._rounds - 1:
                print(f"[gemini] all models failed (round {round_no + 1}) — pausing before retry...")
                time.sleep(15)
        if self._last_resort is not None:
            print("[gemini] Google chain exhausted — falling back to OpenRouter...")
            return self._last_resort.decide_and_extract(
                history, meals_today, eaten_recent, suggestions, pool,
                peer_bot_sender, incoming_text, today, now, chat_id,
                operator_examples,
            )
        assert last_error is not None
        raise last_error

    def _call_model(
        self,
        model: str,
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
        operator_examples: list[str] | None = None,
    ) -> AgentOutput:
        response = self._client.models.generate_content(
            model=model,
            contents=build_user_prompt(
                history, meals_today, eaten_recent, suggestions, pool,
                peer_bot_sender, today, incoming_text, now, chat_id,
                operator_examples,
            ),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=1.0,
                response_mime_type="application/json",
                response_schema=AgentOutput,
            ),
        )
        if not response.parsed:
            raise RuntimeError(f"Gemini returned unparsable output: {response.text!r}")
        return response.parsed
