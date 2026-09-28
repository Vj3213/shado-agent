"""FastAPI service: the HTTP boundary between the gateway and the brain."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, NamedTuple

import re

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# Exact-match console commands; anything else in the operator DM is conversation.
# yes/no take an optional chat id (poll votes target a specific chat's ask).
_COMMAND_RE = re.compile(
    r"^(yes( .*)?|y|agent|no( .*)?|n|deny|list|stop( .*)?|allow( .*)?)$", re.IGNORECASE
)

from agent.app.config import load_config
from agent.app.gemini_client import GeminiClient, PollOutput
from agent.app.openrouter_client import OpenRouterClient
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


@app.on_event("startup")
def ensure_schema() -> None:
    global policy
    repo.init_schema()
    policy = LoopCapPolicy(repo, config.peer_bot_sender, config.bot_to_bot_max_turns)
    if config.peer_bot_sender is None:
        print(
            "⚠ PEER_BOT_SENDER not set — peer-bot engagement + loop cap stay "
            "inactive until you add its JID to .env."
        )


class IncomingPayload(BaseModel):
    group_id: str
    sender: str
    text: str = Field(min_length=1)
    timestamp: str


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
    session_text = " ".join(m.text.lower() for m in history) + " " + incoming_text.lower()
    pool = _suggestion_pool(group_id, session_text)
    output = gemini.decide_and_extract(
        history=history,
        meals_today=repo.meals_for_day(date.today()),
        eaten_recent=repo.meals_last_days(config.exclude_eaten_days),
        suggestions=repo.recent_suggestions(group_id, config.suggestion_history_days),
        pool=pool,
        peer_bot_sender=config.peer_bot_sender,
        incoming_text=incoming_text,
        today=date.today(),
        now=datetime.now().astimezone(),
        chat_id=group_id,
        operator_examples=repo.operator_exemplars(group_id, 10),
        allowed_reactions=_allowed_reactions(),
    )
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
    )


def _generate_reply_or_none(group_id: str, history: list[MessageRecord], incoming_text: str) -> Decision:
    """Generate a reply; any model failure becomes a clean no-reply decision
    (the gateway stays silent — never a 500 or a half-sent message)."""
    try:
        generated = _generate_reply(group_id, history, incoming_text)
    except Exception as error:
        print(f"[agent] reply generation failed: {error!r}")
        return Decision(reply=None, reason="model_unavailable")
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


@app.post("/messages/reaction")
def handle_reaction(payload: ReactionPayload) -> dict:
    """The operator reacted manually on the bot phone — learn that emoji."""
    emoji = payload.emoji.strip()
    repo.add_learned_reaction(emoji)
    return {"learned": emoji}


def _allowed_reactions() -> list[str]:
    """Emojis the operator actually used FIRST (frequency order) — the model
    mirrors their vocabulary — then the static configured set."""
    learned = [e for e in repo.learned_reactions() if e not in config.reactions]
    return learned + list(config.reactions)


def _consent_prompt(chat_id: str, text: str) -> str:
    preview = text.strip().replace("\n", " ")[:80]
    return (
        "📩 New chat wants a reply:\n"
        f"Chat: {chat_id}\n"
        f"Said: \"{preview}\"\n\n"
        f"YES → agent handles this chat for {config.consent_ttl_hours}h\n"
        f"NO → stay silent here\n"
        f"(reply in this self-chat)"
    )


def _pending_delivery(chat_id: str) -> DeliverPayload | None:
    """After consent is granted, reply to the message that triggered the ask —
    otherwise YES would approve silence and the chat would wait for a new
    message that may never come. Looks back one session window only."""
    history = repo.recent_messages_since(chat_id, config.session_hours, config.context_window)
    last_incoming = next((m for m in reversed(history) if m.direction == "in"), None)
    if last_incoming is None:
        return None
    decision = _generate_reply_or_none(chat_id, history, last_incoming.text)
    if decision.reply:
        return DeliverPayload(chat_id=chat_id, text=decision.reply)
    return None


class ConsolePayload(BaseModel):
    text: str


@app.post("/messages/console", response_model=Decision)
def handle_console(payload: ConsolePayload) -> Decision:
    """Operator commands typed in the bot phone's 'Message yourself' chat —
    or forwarded here as `yes <chat>` / `no <chat>` when the operator taps a
    consent poll (the gateway knows which chat each poll was about)."""
    cmd = payload.text.strip().lower()
    if cmd == "yes" or cmd.startswith("yes ") or cmd in ("y", "agent"):
        arg = cmd[3:].strip()
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
        arg = cmd[2:].strip() if cmd.startswith("no ") else ""
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
        if not rows and not pending and not declined:
            return Decision(reply="No chats currently auto-handled, nothing pending.", reason="empty")
        lines = []
        if rows:
            lines.append("🟢 Active:")
            lines += [f"{n}. {chat} (until {expires:%d %b %H:%M})" for n, (chat, expires) in enumerate(rows, 1)]
        if pending:
            lines.append("🟡 Awaiting your YES/NO:")
            lines += [f"• {chat}" for chat, in pending]
        if declined:
            lines.append("⛔ Declined (ALLOW <n> to re-activate):")
            lines += [f"{n}. {chat}" for n, (chat,) in enumerate(declined, 1)]
        return Decision(reply="\n".join(lines), reason="listed")
    if cmd.startswith("allow"):
        arg = cmd[5:].strip()
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
    if cmd == "stop" or cmd.startswith("stop"):
        arg = cmd[4:].strip()
        if arg.isdigit():
            rows = repo.active_consents()
            idx = int(arg) - 1
            if 0 <= idx < len(rows):
                chat = repo.revoke_consent(rows[idx][0])
                return Decision(reply=f"🛑 Auto-reply stopped for {chat}.", reason="revoked")
            return Decision(reply=f"No chat number {arg}. Send LIST to see active chats.", reason="bad_index")
        revoked = repo.revoke_active_consents()
        if not revoked:
            return Decision(reply="Nothing to stop — no chats have auto-reply active.", reason="none_active")
        return Decision(reply="🛑 Auto-reply stopped for:\n" + "\n".join(f"• {c}" for c in revoked), reason="revoked")
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

    # 1. Always record the message so future replies have context.
    repo.add_message(
        MessageRecord(
            group_id=payload.group_id,
            sender=payload.sender,
            text=payload.text,
            direction="in",
            created_at=_parse_ts(payload.timestamp),
        )
    )

    # 2. Access: static allowlists first, then operator-granted consent.
    access = _chat_access(payload.group_id)
    if access == "blocked":
        return Decision(reply=None, reason="chat_blocked")
    if access == "consent" and not repo.has_active_consent(payload.group_id):
        asked = repo.request_consent(payload.group_id, config.consent_pending_timeout_minutes)
        if asked:
            return Decision(
                reply=None,
                reason="consent_requested",
                self_prompt=_consent_prompt(payload.group_id, payload.text),
            )
        return Decision(reply=None, reason="no_consent")

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
    return decision


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
    if config.operator_jid and payload.group_id == config.operator_jid:
        if payload.source == "operator" and _COMMAND_RE.match(payload.text.strip()):
            decision = handle_console(ConsolePayload(text=payload.text))
            decision.reason = f"console_{decision.reason}"
            return decision.model_dump()
        return result
    access = _chat_access(payload.group_id)
    if access == "consent" and not repo.has_active_consent(payload.group_id):
        if repo.request_consent(payload.group_id):
            result["self_prompt"] = _consent_prompt(payload.group_id, payload.text)
    return result


def _parse_ts(raw: str) -> datetime:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now().astimezone()
