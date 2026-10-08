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


class PollOutput(BaseModel):
    question: str
    options: list[str]


class DistilledVoice(BaseModel):
    length: str | None = None
    language_mix: str | None = None
    emoji_habits: str | None = None
    punctuation: str | None = None
    tone: str | None = None


class DistillOutput(BaseModel):
    voice: DistilledVoice
    facts: list[str]


class AgentOutput(BaseModel):
    reply: str | None
    meals: list[ExtractedMeal]
    suggested_dishes: list[str]
    selected_dish: str | None
    is_food_related: bool
    react: str | None = None
    # True ONLY when the model is confident the user themself would have
    # tapped a reaction here (mirroring their habits) — such reactions are
    # never rate-limited; unsure-but-wants-to reactions are.
    user_would_react: bool = False
    # True ONLY when the chat references something the model lacks context
    # for (inside jokes, plans, people) AND answering without it would be
    # wrong. The agent then pauses (no reply), asks the operator once
    # (rate-limited), and enriches future replies via CHAT CONTEXT.
    needs_context: bool = False
    poll: PollOutput | None = None


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
        allowed_reactions: list[str] | None = None,
        chat_reactions: list[str] | None = None,
        chat_context: list[str] | None = None,
        voice_profile: str | None = None,
        facts: list[str] | None = None,
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
                        allowed_reactions,
                        chat_reactions,
                        chat_context,
                        voice_profile,
                        facts,
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
                operator_examples, allowed_reactions, chat_reactions, chat_context,
                voice_profile, facts,
            )
        assert last_error is not None
        raise last_error

    def distill(self, operator_samples: list[str], chat_lines: list[str]) -> DistillOutput:
        """The profiler agent: distill the operator's VOICE (how they write)
        and the chat's durable FACTS from message history. Runs in the
        background — never on the reply path."""
        if self._dry_run:
            return DistillOutput(
                voice=DistilledVoice(
                    length="short, 5-10 words",
                    language_mix="casual Hinglish",
                    emoji_habits="sparse, one every few messages",
                ),
                facts=["[dry-run] no facts extracted"],
            )
        return self._call_distill(self._primary, operator_samples, chat_lines)

    def _call_distill(self, model: str, operator_samples: list[str], chat_lines: list[str]) -> DistillOutput:
        response = self._client.models.generate_content(
            model=model,
            contents=(
                "OPERATOR'S RECENT MESSAGES (the person whose side you're on):\n"
                + "\n".join(operator_samples)
                + "\n\nTHE CHAT'S RECENT CONVERSATION:\n"
                + "\n".join(chat_lines)
            ),
            config=types.GenerateContentConfig(
                system_instruction=DISTILLER_PROMPT,
                temperature=0.3,
                response_mime_type="application/json",
                response_schema=DistillOutput,
            ),
        )
        if not response.parsed:
            raise RuntimeError(f"distiller returned unparsable output: {response.text!r}")
        return response.parsed

    def see_and_reply(self, image_bytes: bytes, mimetype: str, prompt_text: str) -> AgentOutput:
        """The SAME persona contract, but the message is an image/sticker/gif
        frame: contents = [media part, prompt]. Reply/react/etc. still come
        through the structured AgentOutput, so the transport is unchanged."""
        if self._dry_run:
            return AgentOutput(
                reply=f"[dry-run] saw the image and would reply: '{prompt_text[-30:]}'",
                meals=[],
                suggested_dishes=[],
                selected_dish=None,
                is_food_related=False,
            )
        return self._see_call(self._primary, image_bytes, mimetype, prompt_text)

    def _see_call(self, model: str, image_bytes: bytes, mimetype: str, prompt_text: str) -> AgentOutput:
        response = self._client.models.generate_content(
            model=model,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mimetype),
                prompt_text,
            ],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=1.0,
                response_mime_type="application/json",
                response_schema=AgentOutput,
            ),
        )
        if not response.parsed:
            raise RuntimeError(f"vision call returned unparsable output: {response.text!r}")
        return response.parsed

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
        allowed_reactions: list[str] | None = None,
        chat_reactions: list[str] | None = None,
        chat_context: list[str] | None = None,
        voice_profile: str | None = None,
        facts: list[str] | None = None,
    ) -> AgentOutput:
        response = self._client.models.generate_content(
            model=model,
            contents=build_user_prompt(
                history, meals_today, eaten_recent, suggestions, pool,
                peer_bot_sender, today, incoming_text, now, chat_id,
                operator_examples, allowed_reactions, chat_reactions, chat_context,
                voice_profile, facts,
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
