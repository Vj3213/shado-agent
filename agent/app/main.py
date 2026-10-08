"""FastAPI service: the HTTP boundary between the gateway and the brain."""

from __future__ import annotations

import base64
import asyncio
import os
from datetime import date, datetime
from typing import Literal, NamedTuple

import re

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# Exact-match console commands; anything else in the operator DM is conversation.
# yes/no/stop/allow take an optional argument (a chat id or LIST number);
# trust/untrust manage the zero-first-ask allowlist; remember/forget manage
# per-chat context notes.
_COMMAND_RE = re.compile(
    r"^(yes( .*)?|y|agent|no( .*)?|n|deny|list|stop( .*)?|allow( .*)?|trust( .*)?|untrust( .*)?"
    r"|remember(-global)?( .*)?|forget(-global)?( .*)?)$",
    re.IGNORECASE,
)

from agent.app.config import load_config
from agent.app.gemini_client import GeminiClient, PollOutput
from agent.app.openrouter_client import OpenRouterClient
from agent.app.persona import build_user_prompt
from agent.app.policy import LoopCapPolicy
from agent.app.triggers import is_followup, matches_trigger
from storage import (
    DishRow,
    MealEntry,
    MessageRecord,
    PostgresRepository,
    SuggestionRow,
)

app = FastAPI(title="shado-agent", version="0.2.0")

config = load_config()
repo = PostgresRepository(config.database_url)
openrouter = (
    OpenRouterClient(config.openrouter_api_key, config.openrouter_models)
    if config.openrouter_api_key
    else None
)
gemini = GeminiClient(
    config.gemini_api_key,
    config.gemini_model,
    config.gemini_fallback_models,
    dry_run=config.dry_run,
    retry_rounds=config.gemini_retry_rounds,
    last_resort=openrouter,
)
if openrouter:
    print(f"[agent] OpenRouter fallback enabled: {', '.join(config.openrouter_models)}")
policy = None  # built at startup (after schema exists)


def _run_distill(chat_id: str) -> None:
    """One profiler round: voice samples + recent chat lines → one Gemini call
    → style_profile overwritten + facts merged (re-mentioned = refreshed)."""
    samples = repo.operator_exemplars(chat_id, config.distiller_voice_samples)
    lines = [
        f"{'YOU (operator)' if m.source == 'operator' else 'GROUP MEMBER (' + m.sender.split('@')[0][-4:] + ')'}: {m.text}"
        for m in repo.recent_messages(chat_id, config.distiller_fact_window_messages)
    ]
    if not samples and not lines:
        return
    output = gemini.distill(samples, lines)
    voice = output.voice
    profile_lines = "\n".join(
        f"  • {label}: {value}"
        for label, value in (
            ("typical length", voice.length),
            ("language mix", voice.language_mix),
            ("emoji habits", voice.emoji_habits),
            ("punctuation", voice.punctuation),
            ("tone", voice.tone),
        )
        if value
    )
    repo.save_style_profile(chat_id, profile_lines)
    repo.merge_facts(chat_id, output.facts, config.distiller_facts_ttl_days)
    repo.mark_distill_run(chat_id)
    print(f"[distiller] profiled {chat_id} ({len(output.facts)} facts)")


async def distill_loop() -> None:
    """Background distiller: every poll, run the profiler for chats whose
    operator-message count crossed the threshold (min gap respected)."""
    while True:
        await asyncio.sleep(config.distiller_poll_seconds)
        try:
            due = repo.chats_due_for_distill(
                config.distiller_operator_msgs_per_run, config.distiller_min_gap_hours
            )
            for chat_id in due:
                await asyncio.to_thread(_run_distill, chat_id)
        except asyncio.CancelledError:
            print("[distiller] stopping")
            raise
        except Exception as error:
            print(f"[distiller] round failed: {error!r}")


@app.on_event("startup")
async def ensure_schema() -> None:
    global policy
    repo.init_schema()
    policy = LoopCapPolicy(repo, config.peer_bot_sender, config.bot_to_bot_max_turns)
    if config.peer_bot_sender is None:
        print(
            "⚠ PEER_BOT_SENDER not set — peer-bot engagement + loop cap stay "
            "inactive until you add its JID to .env."
        )
    # The distiller never runs in dry-run (nothing real to profile) — override
    # with SHADO_DISTILL_IN_DRY_RUN=1 when testing it deliberately.
    if not config.dry_run or os.environ.get("SHADO_DISTILL_IN_DRY_RUN") == "1":
        asyncio.get_running_loop().create_task(distill_loop())
        print("[distiller] background profiler started")


class IncomingPayload(BaseModel):
    group_id: str
    sender: str
    text: str = Field(min_length=1)
    timestamp: str
    sender_name: str | None = None
    sender_number: str | None = None


class OutgoingPayload(BaseModel):
    group_id: str
    text: str = Field(min_length=1)
    timestamp: str
    source: Literal["operator", "agent"] = "agent"


class DeliverPayload(BaseModel):
    """A message the agent plans for a chat OTHER than the one being answered —
    the gateway transports it (e.g. the first reply to a just-consented chat)."""

    chat_id: str
    text: str


class Decision(BaseModel):
    reply: str | None
    reason: str
    self_prompt: str | None = None
    react: str | None = None
    user_would_react: bool = False
    poll: PollOutput | None = None
    deliver: DeliverPayload | None = None


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model": config.gemini_model, "dry_run": config.dry_run}


def _ingredient_mentions(text: str) -> list[int]:
    """Ingredient ids whose canonical name or Hindi alias appears in the text."""
    lowered = text.lower()
    ids: list[int] = []
    for ing in repo.list_ingredients():
        names = [ing.name.lower()] + [a.strip().lower() for a in ing.common_names.split(",") if a.strip()]
        if any(name and name in lowered for name in names):
            ids.append(ing.id)
    return ids


def _suggestion_pool(group_id: str, session_text: str) -> list[DishRow]:
    """RAG-lite retrieval: dishes matching ingredients discussed, minus
    out-of-season, recently-eaten and already-suggested dishes."""
    ids = _ingredient_mentions(session_text)
    pool = repo.dishes_for_ingredients(ids) if ids else repo.all_dishes()

    out_of_season = {name.lower() for name in repo.dishes_out_of_season(datetime.now().month)}
    eaten = {m.item.lower() for m in repo.meals_last_days(config.exclude_eaten_days)}
    blocked = {
        s.dish.lower()
        for s in repo.recent_suggestions(group_id, config.exclude_suggested_days)
        if s.status in ("suggested", "pending", "selected")
    }
    return [
        d
        for d in pool
        if d.name.lower() not in eaten | blocked | out_of_season
    ]


class _GeneratedReply(NamedTuple):
    reply: str | None
    react: str | None
    user_would_react: bool
    poll: PollOutput | None
    context_ask: str | None


def _sanitize_poll(output: AgentOutput) -> PollOutput | None:
    """Polls are transport-gated: only when enabled, only with a usable
    question, options clamped to the configured max."""
    if not config.polls_enabled or output.poll is None:
        return None
    poll = output.poll
    options = [o.strip() for o in poll.options if o.strip()][: config.poll_max_options]
    question = poll.question.strip()
    if len(options) < 2 or not question:
        print("[agent] poll dropped — needs a question and at least 2 options")
        return None
    return PollOutput(question=question, options=options)


def _generate_reply(group_id: str, history: list[MessageRecord], incoming_text: str) -> _GeneratedReply:
    output = gemini.decide_and_extract(
        history=history,
        meals_today=repo.meals_for_day(date.today()),
        eaten_recent=repo.meals_last_days(config.exclude_eaten_days),
        suggestions=repo.recent_suggestions(group_id, config.suggestion_history_days),
        pool=_suggestion_pool(group_id, _session_text(history, incoming_text)),
        peer_bot_sender=config.peer_bot_sender,
        incoming_text=incoming_text,
        today=date.today(),
        now=datetime.now().astimezone(),
        chat_id=group_id,
        operator_examples=repo.operator_exemplars(group_id, 10, before=_window_start(history)),
        allowed_reactions=_allowed_reactions(),
        chat_reactions=repo.chat_reactions(group_id),
        chat_context=_merged_context(group_id),
        voice_profile=repo.style_profile(group_id),
        facts=repo.active_facts(group_id, config.distiller_facts_in_prompt),
    )
    return _finalize_output(group_id, output, incoming_text)


def _generate_media_reply(
    group_id: str,
    history: list[MessageRecord],
    incoming_text: str,
    media_bytes: bytes,
    mimetype: str,
) -> _GeneratedReply:
    """Same persona contract, but the latest message is an image/sticker/gif:
    the model SEES the media plus the ordinary prompt context."""
    output = gemini.see_and_reply(
        media_bytes,
        mimetype,
        build_user_prompt(
            history=history,
            meals_today=repo.meals_for_day(date.today()),
            eaten_recent=repo.meals_last_days(config.exclude_eaten_days),
            suggestions=repo.recent_suggestions(group_id, config.suggestion_history_days),
            pool=_suggestion_pool(group_id, _session_text(history, incoming_text)),
            peer_bot_sender=config.peer_bot_sender,
            incoming_text=incoming_text,
            today=date.today(),
            now=datetime.now().astimezone(),
            chat_id=group_id,
            operator_examples=repo.operator_exemplars(group_id, 10, before=_window_start(history)),
            allowed_reactions=_allowed_reactions(),
            chat_reactions=repo.chat_reactions(group_id),
            chat_context=_merged_context(group_id),
            voice_profile=repo.style_profile(group_id),
            facts=repo.active_facts(group_id, config.distiller_facts_in_prompt),
        ),
    )
    return _finalize_output(group_id, output, incoming_text)


def _merged_context(group_id: str) -> list[str]:
    """Per-chat notes + GLOBAL notes (prefixed) — one CHAT CONTEXT list."""
    context = repo.context_notes(group_id, config.context_max_notes)
    global_notes = [f"GLOBAL: {n}" for n in repo.context_notes(GLOBAL_CONTEXT_KEY, config.context_max_notes)]
    return global_notes + context


def _session_text(history: list[MessageRecord], incoming_text: str) -> str:
    return " ".join(m.text.lower() for m in history) + " " + incoming_text.lower()


def _window_start(history: list[MessageRecord]):
    """Style exemplars must NOT overlap the conversation window — the
    operator's in-window messages are already visible as 'typed by hand'."""
    return min((m.created_at for m in history), default=None)


def _finalize_output(group_id: str, output, incoming_text: str) -> _GeneratedReply:
    """Everything AFTER a model call, shared by text and media paths: DB
    writes (meals/suggestions/selection), sanitization (reaction whitelist,
    poll clamps) and the ask-for-context pause."""
    per_chat_context = repo.context_notes(group_id, config.context_max_notes)
    print(
        f"[agent] react decision: {output.react!r}"
        f" (user_would_react={output.user_would_react})"
    )
    context_ask: str | None = None
    if output.needs_context and not per_chat_context and not repo.context_notes(GLOBAL_CONTEXT_KEY, 1):
        # Ask-first, rate-limited: pause this message (no reply to the
        # person), ask the operator once; later messages reply naturally.
        if repo.may_ask_for_context(group_id, config.context_ask_cooldown_hours):
            repo.record_context_ask(group_id)
            context_ask = (
                f"🧠 {_chat_label(group_id)} said something I lack context for:\n"
                f'"{incoming_text[:70]}"\n'
                "Reply REMEMBER <chat id> <note> — until then I reply naturally without it."
            )
            print("[agent] needs context — asking the operator")
    if output.selected_dish:
        if not repo.mark_suggestion_selected(group_id, output.selected_dish):
            print(f"[agent] selection not matched to a suggestion: {output.selected_dish}")
    allowed = set(_allowed_reactions())
    react = output.react if output.react in allowed else None
    if output.react and react is None:
        print(f"[agent] reaction '{output.react}' not in allowed set — dropped")
    if output.suggested_dishes:
        repo.add_suggestions(group_id, output.suggested_dishes)
    if output.meals:
        repo.add_meals(
            [
                MealEntry(day=date.today(), item=m.item, meal_type=m.meal_type)
                for m in output.meals
            ]
        )
    return _GeneratedReply(
        reply=output.reply,
        react=react,
        user_would_react=bool(react) and output.user_would_react,
        poll=_sanitize_poll(output),
        context_ask=context_ask,
    )


def _generate_reply_or_none(group_id: str, history: list[MessageRecord], incoming_text: str) -> Decision:
    """Generate a reply; any model failure becomes a clean no-reply decision
    (the gateway stays silent — never a 500 or a half-sent message)."""
    try:
        generated = _generate_reply(group_id, history, incoming_text)
    except Exception as error:
        print(f"[agent] reply generation failed: {error!r}")
        return Decision(reply=None, reason="model_unavailable")
    if generated.context_ask:
        return Decision(reply=None, reason="waiting_for_context", self_prompt=generated.context_ask)
    if generated.reply is None:
        return Decision(reply=None, reason="model_declined")
    return Decision(
        reply=generated.reply,
        reason="ok",
        react=generated.react,
        user_would_react=generated.user_would_react,
        poll=generated.poll,
    )


def _chat_access(chat_id: str) -> str:
    """static (always allowed) | consent (operator must approve) | blocked."""
    if chat_id == config.group_jid:
        return "static"
    if config.operator_jid and chat_id == config.operator_jid:
        return "static"  # the operator can always talk to their own agent
    if chat_id.endswith("@g.us"):
        return "consent"  # unknown groups: one-time operator approval
    return "consent" if config.allow_private_chats else "blocked"


class ReactionPayload(BaseModel):
    emoji: str = Field(min_length=1, max_length=12)
    chat_id: str | None = None


@app.post("/messages/reaction")
def handle_reaction(payload: ReactionPayload) -> dict:
    """The operator reacted manually — learn the emoji globally and, when the
    gateway says which chat, also how the operator reacts THERE (mirroring
    material for that chat)."""
    emoji = payload.emoji.strip()
    repo.add_learned_reaction(emoji)
    if payload.chat_id:
        repo.add_chat_reaction(payload.chat_id, emoji)
    return {"learned": emoji}


def _allowed_reactions() -> list[str]:
    """Emojis the operator actually used FIRST (frequency order) — the model
    mirrors their vocabulary — then the static configured set."""
    learned = [e for e in repo.learned_reactions() if e not in config.reactions]
    return learned + list(config.reactions)


def _consent_prompt(chat_id: str, text: str) -> str:
    name = repo.contact_name(chat_id)
    label = f"{name} ({chat_id})" if name else chat_id
    preview = text.strip().replace("\n", " ")[:80]
    return (
        "📩 New chat wants a reply:\n"
        f"Chat: {label}\n"
        f"Said: \"{preview}\"\n\n"
        f"YES → agent handles this chat for {config.consent_ttl_hours}h\n"
        f"NO → stay silent here\n"
        f"(tap the poll below or reply here)"
    )


def _chat_label(chat_id: str) -> str:
    name = repo.contact_name(chat_id)
    return f"{name} ({chat_id})" if name else chat_id


def _pending_delivery(chat_id: str) -> DeliverPayload | None:
    """After consent is granted, decide whether to speak now — the MODEL
    decides (it may see the person was already answered by a human hand);
    otherwise the chat joins from its next message. Looks back one session."""
    history = repo.recent_messages_since(chat_id, config.session_hours, config.context_window)
    last_incoming = next((m for m in reversed(history) if m.direction == "in"), None)
    if last_incoming is None:
        return None
    decision = _generate_reply_or_none(chat_id, history, "__CONSENT_GRANTED__")
    if decision.reply:
        print(f"[agent] pending reply prepared for {chat_id}")
        return DeliverPayload(chat_id=chat_id, text=decision.reply)
    print(f"[agent] pending reply skipped for {chat_id} ({decision.reason}) — joins from next message")
    return None


class ConsolePayload(BaseModel):
    text: str


def _trust_tokens(arg: str) -> tuple[list[str], list[str], str]:
    """Split a TRUST/UNTRUST argument into (names, numbers, scope).
    A trailing @-token is the scope for every entry; an entry of 8+ digits
    (ignoring +/spaces/dashes) is a phone number, anything else a name."""
    blob, scope = arg, "any"
    tokens = arg.rsplit(" ", 1)
    if len(tokens) == 2 and "@" in tokens[1]:
        blob, scope = tokens[0].strip(), tokens[1]
    names: list[str] = []
    numbers: list[str] = []
    for token in (t.strip() for t in blob.split(",")):
        if not token:
            continue
        digits = "".join(ch for ch in token if ch.isdigit())
        if digits == "".join(ch for ch in token if ch.isalnum()) and len(digits) >= 8:
            numbers.append(digits)
        else:
            names.append(token)
    return names, numbers, scope


@app.post("/messages/console", response_model=Decision)
def handle_console(payload: ConsolePayload) -> Decision:
    """Operator commands typed in the bot phone's 'Message yourself' chat —
    or forwarded here as `yes <chat>` / `no <chat>` when the operator taps a
    consent poll (the gateway knows which chat each poll was about)."""
    raw = payload.text.strip()
    cmd = raw.lower()
    if cmd == "yes" or cmd.startswith("yes ") or cmd in ("y", "agent"):
        arg = raw[3:].strip()
        if arg:
            if repo.grant_consent(arg, config.consent_ttl_hours):
                return Decision(
                    reply=f"✅ Agent will reply in {arg} for {config.consent_ttl_hours}h. Send 'stop' to end sooner.",
                    reason="consent_granted",
                    deliver=_pending_delivery(arg),
                )
            return Decision(
                reply=f"No pending request for {arg} — nothing to approve.",
                reason="unknown_chat",
            )
        chat = repo.grant_latest_pending(config.consent_ttl_hours)
        if chat is None:
            return Decision(reply="No pending chat requests right now.", reason="no_pending")
        return Decision(
            reply=f"✅ Agent will reply in {chat} for {config.consent_ttl_hours}h. Send 'stop' to end sooner.",
            reason="consent_granted",
            deliver=_pending_delivery(chat),
        )
    if cmd.startswith("no ") or cmd in ("no", "n", "deny"):
        arg = raw[2:].strip() if cmd.startswith("no ") else ""
        if arg:
            if repo.decline_consent(arg):
                return Decision(reply=f"🚫 Understood — staying silent in {arg}.", reason="consent_declined")
            return Decision(reply=f"No pending request for {arg} — nothing to decline.", reason="unknown_chat")
        chat = repo.decline_latest_pending()
        if chat is None:
            return Decision(reply="No pending chat requests right now.", reason="no_pending")
        return Decision(reply=f"🚫 Understood — staying silent in {chat}.", reason="consent_declined")
    if cmd == "list":
        rows = repo.active_consents()
        pending = repo.pending_consents()
        declined = repo.declined_consents()
        trusted = repo.trusted_rows()
        context_chats = repo.context_chats()
        global_notes = repo.context_notes(GLOBAL_CONTEXT_KEY, config.context_max_notes)
        if not rows and not pending and not declined and not trusted \
                and not context_chats and not global_notes:
            return Decision(reply="Nothing tracked yet — no chats, watches or notes.", reason="empty")
        lines = []
        if rows:
            lines.append("🟢 Active:")
            lines += [
                f"{n}. {_chat_label(chat)} (until {expires:%d %b %H:%M})"
                for n, (chat, expires) in enumerate(rows, 1)
            ]
        if pending:
            lines.append("🟡 Awaiting your YES/NO:")
            lines += [
                f"• {_chat_label(chat)} — \"{(repo.last_message(chat) or '?')[:60]}\""
                for chat, in pending
            ]
        if declined:
            lines.append("⛔ Declined (ALLOW <n> to re-activate):")
            lines += [f"{n}. {_chat_label(chat)}" for n, (chat,) in enumerate(declined, 1)]
        if trusted:
            lines.append("🤍 Trusted (first match auto-approves):")
            lines += [
                f'• +{number} ({scope}) — bound: {bound or "not yet"}' if number
                else f'• "{name}" ({scope}) — bound: {bound or "not yet"}'
                for name, number, scope, bound in trusted
            ]
        global_notes = repo.context_notes(GLOBAL_CONTEXT_KEY, config.context_max_notes)
        if global_notes:
            lines.append("🌍 Global context (injected into every chat):")
            lines += [f"  • {n}" for n in global_notes]
        for chat in context_chats:
            notes = repo.context_notes(chat, config.context_max_notes)
            if notes:
                lines.append(f"🧠 Context for {_chat_label(chat)}:")
                lines += [f"  • {n}" for n in notes]
        return Decision(reply="\n".join(lines), reason="listed")
    if cmd.startswith("allow"):
        arg = raw[5:].strip()
        declined = repo.declined_consents()
        if not arg.isdigit() or not (0 <= int(arg) - 1 < len(declined)):
            return Decision(reply=f"Usage: ALLOW <n> — send LIST to see declined chats.", reason="help")
        chat = declined[int(arg) - 1][0]
        repo.grant_consent(chat, config.consent_ttl_hours)
        return Decision(
            reply=f"✅ Agent will reply in {chat} for {config.consent_ttl_hours}h again.",
            reason="re_allowed",
            deliver=_pending_delivery(chat),
        )
    if cmd.startswith("trust"):
        arg = raw[5:].strip()
        if not arg:
            return Decision(
                reply="Usage: TRUST <name or +number> [chat id] — the first sender matching is "
                "auto-approved for 24h without asking you (numbers win over names). "
                "Multiple: TRUST Rohit, +9198xxxxxxx, Priya. Note: a chat you DECLINED (NO) "
                "stays silent — revive it with ALLOW <n>.",
                reason="help",
            )
        names, numbers, scope = _trust_tokens(arg)
        if not names and not numbers:
            return Decision(reply="Usage: TRUST <name or +number> [chat id]", reason="help")
        for name in names:
            repo.trust_name(name, scope)
        for number in numbers:
            repo.trust_number(number, scope)
        where = f"within {scope}" if scope != "any" else "in any chat"
        watched = ", ".join([f'"{n}"' for n in names] + numbers)
        return Decision(
            reply=f"🤍 Watching {watched} {where} — their first message auto-approves for {config.consent_ttl_hours}h without asking you.",
            reason="trusted",
        )
    if cmd.startswith("untrust"):
        arg = raw[7:].strip()
        if not arg:
            return Decision(reply="Usage: UNTRUST <name or number> — remove trusted watching.", reason="help")
        names, numbers, _ = _trust_tokens(arg)
        removed = sum(repo.untrust_name(n) for n in names)
        removed += sum(repo.untrust_number(n) for n in numbers)
        return Decision(
            reply=f"🧹 Removed {removed} trust entr{'y' if removed == 1 else 'ies'}.",
            reason="untrusted",
        )
    if re.match(r"^remember[- ]global\s*", cmd):
        note = re.sub(r"^remember[- ]global\s*", "", raw, flags=re.IGNORECASE).strip()
        if not note:
            return Decision(
                reply="Usage: REMEMBER-GLOBAL <note> — a private fact injected into EVERY chat "
                f"(expires in {config.context_note_ttl_days} days; REMEMBER-GLOBAL again to refresh).",
                reason="help",
            )
        repo.add_context_note(GLOBAL_CONTEXT_KEY, note, config.context_note_ttl_days)
        return Decision(reply=f"🌍 Noted globally (until +{config.context_note_ttl_days}d).", reason="context_noted")
    if re.match(r"^forget[- ]global\s*$", cmd):
        removed = repo.forget_context(GLOBAL_CONTEXT_KEY)
        return Decision(
            reply=f"🌍 Cleared {removed} global note{'s' if removed != 1 else ''}.",
            reason="context_forgotten",
        )
    if cmd.startswith("remember"):
        arg = raw[8:].strip()
        tokens = arg.split(" ", 1)
        if len(tokens) < 2 or "@" not in tokens[0]:
            return Decision(
                reply="Usage: REMEMBER <chat id> <note> — a private fact about that chat, "
                "injected into every prompt there (expires in "
                f"{config.context_note_ttl_days} days; REMEMBER again to refresh).",
                reason="help",
            )
        chat_id, note = tokens[0].strip(), tokens[1].strip()
        repo.add_context_note(chat_id, note, config.context_note_ttl_days)
        reply = f"🧠 Noted for {_chat_label(chat_id)} (until +{config.context_note_ttl_days}d)."
        # Resume: if the chat's newest message is a recent unanswered incoming,
        # reply to it now with the fresh context (consent-deliver machinery) —
        # but only when the chat has been quiet: a reply seconds after the
        # agent's own last message would look robotic.
        pending = repo.last_unanswered(chat_id, config.session_hours)
        deliver = None
        if pending:
            out_age_h = repo.last_outgoing_age_hours(chat_id)
            quiet = out_age_h is None or out_age_h * 60 >= config.context_resume_min_gap_minutes
            if quiet:
                decision = _generate_reply_or_none(
                    chat_id,
                    repo.recent_messages_since(chat_id, config.session_hours, config.context_window),
                    pending,
                )
                if decision.reply:
                    deliver = DeliverPayload(chat_id=chat_id, text=decision.reply)
                    reply = f"🧠 Noted — and replied to {_chat_label(chat_id)} using it."
            else:
                reply += " The chat is still active — the next message uses it."
        return Decision(reply=reply, reason="context_noted", deliver=deliver)
    if cmd.startswith("forget"):
        arg = raw[6:].strip()
        if "@" not in arg:
            return Decision(reply="Usage: FORGET <chat id> — clears that chat's context notes.", reason="help")
        removed = repo.forget_context(arg.strip())
        return Decision(
            reply=f"🧠 Cleared {removed} note{'s' if removed != 1 else ''} for {_chat_label(arg.strip())}.",
            reason="context_forgotten",
        )
    if cmd == "stop" or cmd.startswith("stop"):
        arg = raw[4:].strip()
        if "@" in arg:  # a chat id — stop that one specifically
            chat = repo.revoke_consent(arg)
            if chat:
                return Decision(reply=f"🛑 Auto-reply stopped for {_chat_label(chat)}.", reason="revoked")
            return Decision(reply=f"{arg} is not active — nothing to stop. Send LIST to see active chats.", reason="unknown_chat")
        if arg.isdigit():
            rows = repo.active_consents()
            idx = int(arg) - 1
            if 0 <= idx < len(rows):
                chat = repo.revoke_consent(rows[idx][0])
                return Decision(reply=f"🛑 Auto-reply stopped for {_chat_label(chat)}.", reason="revoked")
            return Decision(reply=f"No chat number {arg}. Send LIST to see active chats.", reason="bad_index")
        revoked = repo.revoke_active_consents()
        if not revoked:
            return Decision(reply="Nothing to stop — no chats have auto-reply active.", reason="none_active")
        watches = repo.trusted_rows()
        reply = "🛑 Auto-reply stopped for:\n" + "\n".join(f"• {_chat_label(c)}" for c in revoked)
        if watches:
            reply += "\n\n🤍 Still watching (UNTRUST <name> to remove):\n" + "\n".join(
                f'• "{n}" ({s})' for n, s, _ in watches
            )
        return Decision(reply=reply, reason="revoked")
    return Decision(
        reply="Commands: YES (approve latest request) · NO (decline) · LIST (active chats) · STOP (end auto-reply)",
        reason="help",
    )






@app.post("/messages/incoming", response_model=Decision)
def handle_incoming(payload: IncomingPayload) -> Decision:
    # Operator DM is dual-mode: exact commands run the console; anything else
    # is normal conversation with the agent (the chat is always allowed).
    if config.operator_jid and payload.sender == config.operator_jid and not payload.group_id.endswith("@g.us"):
        if _COMMAND_RE.match(payload.text.strip()):
            decision = handle_console(ConsolePayload(text=payload.text))
            decision.reason = f"console_{decision.reason}"
            return decision
        # fall through: handled as conversation below

    decision, trusted_notify = _intake(payload.group_id, payload.sender, payload.sender_name, payload.sender_number, payload.text, payload.timestamp)
    if decision:
        return decision

    # 2. Bot-to-bot path: cap-checked, counted in storage.
    if policy.is_peer_bot(payload.sender):
        if policy.cap_reached(payload.group_id):
            return Decision(reply=None, reason="bot_loop_cap_reached")
        decision = _generate_reply_or_none(
            payload.group_id,
            repo.recent_messages_since(payload.group_id, config.session_hours, config.context_window),
            payload.text,
        )
        if not policy.count_reply_to_peer(payload.group_id):
            return Decision(reply=None, reason="bot_loop_cap_reached")
        return decision if decision.reply else Decision(reply=None, reason=decision.reason)

    # 3. Human path: a human speaking resets the bot-to-bot counter.
    policy.note_human_activity(payload.group_id)
    history = repo.recent_messages_since(payload.group_id, config.session_hours, config.context_window)

    if not config.reply_to_everything:
        trigger = matches_trigger(payload.text, config.triggers)
        followup = trigger is None and is_followup(
            history, payload.sender, config.followup_window_seconds
        )
        if trigger is None and not followup:
            return Decision(reply=None, reason="no_trigger")

    decision = _generate_reply_or_none(payload.group_id, history, payload.text)
    if trusted_notify:
        decision.self_prompt = trusted_notify
    return decision


class IncomingMediaPayload(BaseModel):
    group_id: str
    sender: str
    timestamp: str
    kind: str  # image | sticker | gif
    mimetype: str
    media_base64: str
    caption: str | None = None
    sender_name: str | None = None
    sender_number: str | None = None


GLOBAL_CONTEXT_KEY = "__global__"  # chat_id sentinel: notes injected EVERYWHERE


@app.post("/messages/media", response_model=Decision)
def handle_media(payload: IncomingMediaPayload) -> Decision:
    """Images/stickers/GIF frames: same access rules as text (consent FIRST —
    an unapproved chat's media is never processed), then the model SEES the
    media via the ordinary prompt context. Media is untrusted content: the
    persona carries an explicit never-follow-instructions-inside-it rule."""
    if not config.media_reply_to_media:
        return Decision(reply=None, reason="media_disabled")
    try:
        media_bytes = base64.b64decode(payload.media_base64, validate=True)
    except Exception:
        return Decision(reply=None, reason="media_corrupt")
    if not media_bytes:
        return Decision(reply=None, reason="media_corrupt")
    if len(media_bytes) > config.media_max_mb * 1024 * 1024:
        print(f"[agent] media too large ({len(media_bytes)} bytes) — skipped")
        return Decision(reply=None, reason="media_too_large")
    if payload.mimetype not in config.media_allowed_mimetypes:
        print(f"[agent] media type {payload.mimetype!r} not allowed — skipped")
        return Decision(reply=None, reason="media_rejected")

    placeholder = f"[{payload.kind}]" + (f" {payload.caption.strip()}" if payload.caption else "")
    decision, trusted_notify = _intake(
        payload.group_id,
        payload.sender,
        payload.sender_name,
        payload.sender_number,
        placeholder,
        payload.timestamp,
    )
    if decision:
        return decision

    history = repo.recent_messages_since(payload.group_id, config.session_hours, config.context_window)
    caption_text = (payload.caption or "").strip() or "[image]"
    try:
        generated = _generate_media_reply(
            payload.group_id, history, caption_text, media_bytes, payload.mimetype
        )
    except Exception as error:
        print(f"[agent] media reply generation failed: {error!r}")
        return Decision(reply=None, reason="model_unavailable")
    if generated.context_ask:
        return Decision(reply=None, reason="waiting_for_context", self_prompt=generated.context_ask)
    if generated.reply is None:
        return Decision(reply=None, reason="model_declined")
    decision = Decision(
        reply=generated.reply,
        reason="ok",
        react=generated.react,
        user_would_react=generated.user_would_react,
        poll=generated.poll,
    )
    if trusted_notify:
        decision.self_prompt = trusted_notify
    return decision



def _intake(
    group_id: str,
    sender: str,
    sender_name: str | None,
    sender_number: str | None,
    text: str,
    timestamp_raw: str,
) -> tuple[Decision | None, str | None]:
    """Shared message intake for text AND media: store, remember the sender,
    then access (static allowlists → consent/trust). Returns a Decision to
    answer with (blocked / ask / no_consent) plus any trusted-auto-grant
    notification to attach to the eventual reply — or (None, None) to proceed
    with generation."""
    repo.upsert_contact(sender, sender_name or "")
    repo.add_message(
        MessageRecord(
            group_id=group_id,
            sender=sender,
            text=text,
            direction="in",
            created_at=_parse_ts(timestamp_raw),
        )
    )
    access = _chat_access(group_id)
    if access == "blocked":
        return Decision(reply=None, reason="chat_blocked"), None
    if access == "consent" and not repo.has_active_consent(group_id):
        trusted_notify = repo.auto_grant_if_trusted(
            group_id, sender, sender_name, config.consent_ttl_hours, sender_number
        )
        if trusted_notify:
            print(f"[agent] trusted auto-grant: {trusted_notify}")
            return None, trusted_notify  # proceed; notify rides the reply
        asked = repo.request_consent(
            group_id,
            config.consent_pending_timeout_minutes,
            config.consent_ttl_hours,
        )
        if asked:
            return Decision(
                reply=None,
                reason="consent_requested",
                self_prompt=_consent_prompt(group_id, text),
            ), None
        if not repo.has_active_consent(group_id):
            hint = (
                f'🤍 "{sender_name}" is trusted, but this chat was DECLINED earlier '
                "— reply ALLOW <n> (send LIST first) to revive it."
                if sender_name and repo.name_is_trusted(sender_name)
                else None
            )
            return Decision(reply=None, reason="no_consent", self_prompt=hint), None
        # A previously-approved chat was silently re-granted — fall through.
    return None, None


class InitiatePayload(BaseModel):
    chat_id: str


@app.post("/messages/initiate", response_model=Decision)
def handle_initiate(payload: InitiatePayload) -> Decision:
    """The bot starting a conversation itself (operator-triggered)."""
    _validate_chat(payload.chat_id)
    history = repo.recent_messages_since(
        payload.chat_id, config.session_hours, config.context_window
    )
    return _generate_reply_or_none(payload.chat_id, history, "__INITIATE__")


@app.post("/messages/outgoing")
def handle_outgoing(payload: OutgoingPayload) -> dict:
    repo.add_message(
        MessageRecord(
            group_id=payload.group_id,
            sender="self",
            text=payload.text,
            direction="out",
            source=payload.source,
            created_at=_parse_ts(payload.timestamp),
        )
    )
    # The operator DM is the console from BOTH phones: a command typed here
    # (from the bot phone or the operator's phone) runs the console.
    result: dict = {"stored": True}
    if payload.source == "operator" and payload.group_id != config.operator_jid:
        repo.bump_distill_counter(payload.group_id)  # voice-memory material
    if config.operator_jid and payload.group_id == config.operator_jid:
        if payload.source == "operator" and _COMMAND_RE.match(payload.text.strip()):
            decision = handle_console(ConsolePayload(text=payload.text))
            decision.reason = f"console_{decision.reason}"
            return decision.model_dump()
        return result
    access = _chat_access(payload.group_id)
    if access == "consent" and not repo.has_active_consent(payload.group_id):
        asked = repo.request_consent(
            payload.group_id,
            config.consent_pending_timeout_minutes,
            config.consent_ttl_hours,
        )
        if asked:
            result["self_prompt"] = _consent_prompt(payload.group_id, payload.text)
        # A silently re-granted chat needs no prompt — just stored.
    return result


def _parse_ts(raw: str) -> datetime:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now().astimezone()
