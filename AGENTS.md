# AGENTS.md — instructions for AI agents working on Shado Agent

## What this project is

**Shado Agent** — a platform-agnostic AI companion that lives in a real private
WhatsApp group as a *normal linked device* (not Meta's Business API), chats like
a human family member (food suggestions, jokes, banter, small talk), mirrors the
operator's personal writing style, and reacts to messages like a friend would.
Future: more platforms (Telegram, Discord), posting capabilities (X, LinkedIn),
more niches (each gets its own database).

**Important**: this is a learning project for the operator (Ved). When
implementing anything, give short plain-English explanations of what and why —
not just code. No God-files, no hardcoded tunables, minimal comments.

## Architecture (strict module boundaries — keep them)

| Module | Owns | Must NOT know |
|---|---|---|
| `gateway/` (TypeScript, Baileys 6.7.24 pinned exact) | WhatsApp socket, receive/send, human-like delays, composing presence, reactions, native polls, consent-poll votes, relay HTTP server (:8090 `/send` `/initiate`) | AI, SQL, triggers |
| `agent/` (Python 3.10, FastAPI, uvicorn) | triggers, consent policy, persona prompt, Gemini call, message plans | WhatsApp protocol |
| `storage/` (psycopg 3) | ALL SQL, atomic loop-cap enforcement, repository `Protocol` | why the data exists |
| `config/settings.json` + `.env` | every tunable + secrets | nothing |

Gateway ↔ agent talk over local HTTP only: `POST /messages/incoming`,
`/messages/outgoing`, `/messages/initiate`, `/messages/console`,
`/messages/reaction`, `/messages/media`, `GET /health`. Agent binds 127.0.0.1:8100;
relay binds 127.0.0.1:8090.

## Data model (Postgres, database `meal_agent`)

- `messages(group_id, sender, text, direction in|out, source operator|agent, created_at)` —
  `source` distinguishes the human's hand from agent output (mimicry uses only `operator`)
- `meal_log(day, item, meal_type)` — global family-wide food memory
- `bot_loop_state(group_id, peer_sender, consecutive_turns)` — loop cap enforced
  *in SQL* (`WHERE consecutive_turns < cap`), reset by any human message
- `ingredient(name, category, common_names, season)` / `dish(name, meal_types)` /
  `dish_ingredient` — food KB, seeded by `scripts/food_data.py` (idempotent, ~72/86/184)
- `suggestion(group_id, dish, status suggested→pending→selected)` — bot never repeats itself
- `chat_consents(chat_id, status pending|granted|declined|revoked, expires_at)` —
  consent-gated dynamic allowlist; pending asks timeout (re-ask), declined = silent
  forever unless `ALLOW <n>`
- `learned_reactions(emoji, times)` — emojis the operator used manually
- `chat_reactions(chat_id, emoji, times)` — same, per chat (mirroring material)
- `contacts(jid, name)` — sender display names (WhatsApp pushName) for readable
  consent prompts and LIST output
- `trusted_names(name, number, scope, bound_jid, bound_chat)` — zero-first-ask
  allowlist (see Trusted entries below)
- `chat_context(chat_id, note, expires_at)` — operator-curated per-chat notes
  ("REMEMBER"); expired = ignored, pruned by the retention sweep
- `context_asks(chat_id, last_ask_at)` — ask-for-context rate limiting

## Running

```bash
bash scripts/run_all.sh        # ONLY when launchd services are uninstalled
bash scripts/service.sh install|status|logs|restart|uninstall   # launchd (3 services, boot-persistent)
```

- If launchd services are loaded, `run_all.sh` refuses to run — that's correct.
- `sudo pmset -a sleep 0` was needed once so the Mac never system-sleeps.
- Logs: `~/Library/Logs/shado/*.log`. Console noise from libsignal is filtered in `gateway/src/index.ts`.
- **Gateway single-instance rule**: only ONE process may ever touch `gateway/auth/`.
  Two instances → WhatsApp `440 connectionReplaced` war (endless reconnects).
  Before ANY gateway start: `pkill -f "tsx src/index.ts"` and verify zero remain.
- **Recovery runbook** (symptom → action):
  - `Connection closed (401). Logged out` → server evicted the session: archive
    `gateway/auth/` and re-link (pairing code via `LINK_PHONE_NUMBER` in .env, or QR
    with it empty). Nothing else is lost — DB state survives.
  - Connected but NO `[recv]` lines for minutes (zombie socket) →
    `launchctl kickstart -k gui/$(id -u)/com.shado.gateway` (fresh connect).
  - `440` reconnect loop → kill ALL gateway processes, keep exactly one.

## Key behaviors (all structural, not prompt-promises)

- **Consent**: unknown chats never get replies until the operator says `YES`
  in their DM (`OPERATOR_JID` = the console). `NO` = permanent silence, `ALLOW <n>` revives.
  Consent is STICKY: every grant sets `remembered` — on expiry the chat is
  **silently re-granted** with a fresh TTL (the ask fires at most once per
  chat, ever). Every STOP path (`STOP <id>`, bare `STOP`, LIST-number stop)
  clears `remembered`, so a stop really stops; the next message re-asks.
  Decline never re-asks, as before.
  A YES/ALLOW also REPLIES to the message that triggered the ask — but the
  MODEL decides whether to speak now: it receives `__CONSENT_GRANTED__` as the
  incoming text plus the chat's history (hand-typed operator messages are
  labeled "typed by hand") and may reply null to join from the next message
  instead. What it does decide is returned as `Decision.deliver
  {chat_id, text}`; the gateway sends it human-like. (Older than one session
  window → nothing pending → no deliver.)
- **Consent commands** are symmetric: `yes`/`no`/`stop` each take an optional
  argument — a chat id (`stop 120363...@g.us`) or a LIST number (`stop 2`) —
  and no argument means ALL (stop) / LATEST pending (yes/no).
- **Consent polls**: the gateway attaches a YES/NO poll after every consent
  prompt; a tap is decrypted by the gateway (`consent-poll.ts`, own
  messageSecret per poll, persisted in `gateway/auth/consent-polls.json`,
  votes map by SHA-256(optionName) hex) and forwarded as a targeted
  `yes <chat>` / `no <chat>` console command — more precise than plain
  `yes`/`no`, which still work and stay the fallback if decryption fails.
- **Names**: the gateway captures `pushName` per message; storage keeps
  `contacts(jid, name)`; consent prompts and LIST show "Name (jid)" and the
  pending chat's last message.
- **Trusted entries (zero-first-ask)**: `TRUST <name | +number> [chat id]` in
  the operator DM watches a sender by NAME or PHONE NUMBER (numbers win on
  precedence — precise, unspoofable; commas list several, a trailing chat id
  scopes all). The gateway resolves group senders via group metadata
  (`sender_number` + the operator's SAVED contact name, cached
  `group_metadata.cache_minutes`); the first sender matching an unbound entry
  is auto-approved (fresh TTL) and bound to their jid (TOFU) — the operator
  gets one notification and `STOP <chat id>` deletes the watch row (the
  stopped chat cannot resurrect). A different jid claiming a bound name is NOT
  auto-approved — it falls back to the normal (sticky) consent ask. Scoped
  entries win over global ones; a DECLINED chat outranks any watch (only
  `ALLOW <n>` revives).
- **Context notes**: `REMEMBER <chat> <note>` appends (TTL `context_notes.default_ttl_days`
  — re-REMEMBER refreshes), `FORGET <chat>` clears; active notes are injected
  as a CHAT CONTEXT prompt block (last `context_notes.max_notes`). When the
  model sets `needs_context` AND no notes exist: the message is PAUSED (no
  reply to the person) and the operator is asked once, rate-limited to
  `context_notes.ask_cooldown_hours` per chat; if never answered, later
  messages reply naturally without exposing anything. `REMEMBER` also
  RESUMES: if the chat's newest message is a recent unanswered incoming, a
  context-informed reply is delivered then (consent-deliver machinery).
- **Model fallback chain**: `gemini-3.5-flash-lite` → `3.6-flash` → `3.8-flash`
  (1 Gemini round if OpenRouter configured) → OpenRouter `:free` models →
  live-catalog self-heal → clean silence on total failure. 503/504/429/404 fall
  through; 401/403 abort.
- **Human-like send**: randomized delay = reading(incoming len) + typing(reply len)
  + base + extra 1–5s, capped 20s; composing presence; one message per turn.
- **Read receipts**: the gateway blue-ticks a message the moment it ENGAGES
  (reply or react) — `sock.readMessages` (WhatsApp-Web-style); ignored messages
  stay unticked (humans don't read everything instantly). Needs "Read receipts"
  ON in the phone's WhatsApp privacy settings; toggle: settings.json
  `read_receipts.enabled`.
- **Reactions**: two-tier policy. PRIMARY: the model reacts only when it is
  confident the USER themself would have tapped a reaction (`user_would_react`
  in the contract) — mirroring their habits; never rate-limited. SECONDARY:
  the model's own impulse — gated by a per-chat cooldown
  (`config.reactions.cooldown_seconds`, 120s). The model outputs the emoji
  only; the gateway attaches it to the triggering message key (the model can't
  pick targets). Whitelist: `learned_reactions` first (the user's own
  vocabulary, frequency-ordered) ∪ `config.reactions.allowed`. Every react
  decision is logged (`[agent] react decision: …`). The prompt includes
  `REACTIONS THE USER MAKES IN THIS CHAT` (from `chat_reactions`) — reactions
  are learned from the bot phone AND the operator's personal phone (DM only;
  group reactions by others are ignored).
- **Style mirroring**: operator style samples strictly OUTSIDE the conversation
  window (no duplicated tokens — in-window messages are visible as "typed by
  hand"); if none exist, the style block points at the typed-by-hand lines.
- **Media (images/stickers/GIFs)**: the gateway downloads bytes (GIFs/videos
  via the inline jpegThumbnail frame) and forwards to `/messages/media`; the
  agent gate-consents FIRST (unapproved chats' media is never processed),
  enforces size (`media.max_mb`) + mimetype allowlist, then the model SEES the
  media through the ordinary prompt (same AgentOutput contract). Media is
  UNTRUSTED content: persona carries an explicit never-follow-instructions-
  inside-it rule; storage uses a `[image]` placeholder + caption.
- **Global context**: `REMEMBER-GLOBAL <note>` / `FORGET-GLOBAL` — notes with a
  `__global__` sentinel chat id, injected into EVERY chat's prompt prefixed
  `GLOBAL:`; LIST shows them.
- **Polls**: on "kya banau aaj?"-style food-choice questions the model may
  attach ONE native poll (`AgentOutput.poll`, question + options from the
  suggestion pool); agent sanitizes (settings.json `polls`: enabled, max
  options) and Decision carries it; gateway sends it right after the reply
  text with a short delay and reports `[poll] q (a / b)` as outgoing context.
  Votes are NOT read yet (v1 send-only). A poll accompanies the reply — it
  never replaces it.

## Conventions

- Dependencies pinned exact (`agent/requirements.txt`, `gateway/package.json`).
  `pip-audit` and `npm audit` must stay clean — run both after any dep change.
- **When adding a parameter that crosses layers** (persona → `_call_model` →
  `decide_and_extract` → OpenRouter), add it to EVERY layer in the same edit and
  run a layered-signature check, or better: bundle the params into one object
  (preferred refactor — the list is at 12 already).
- SQL always parameterized. Tests live in `tests/` (pytest, isolated
  `meal_agent_test` DB — see tests/conftest.py) and `gateway/src/*.spec.ts`
  (vitest). Run: `.venv/bin/python -m pytest tests/` and
  `cd gateway && npm test`. CI (`.github/workflows/ci.yml`) runs the gateway
  typecheck + pip-audit + npm audit on every push.
- ⚠️ `AGENT_DRY_RUN=1` in the shell makes GeminiClient short-circuit — never
  carry that env var into tests of the generation path. tests/conftest.py
  deletes it up front; only tests/test_agent_flow.py sets it (deliberately,
  for the HTTP boundary) and unsets it right after importing `agent.app.main`.
- ⚠️ Fake httpx responses in tests must carry `request=` — httpx 0.28's
  `raise_for_status()` raises RuntimeError without it.
- ⚠️ Renaming the project folder breaks `.venv` shebangs → rebuild:
  `python3 -m venv --clear .venv && .venv/bin/pip install -r agent/requirements.txt`.

## Current state (as of this handoff)

All three launchd services healthy, real Gemini active, WhatsApp linked (agent
runs on the operator's personal number; console = "Message yourself"). Secrets
in `.env` (never committed — verified). LICENSE (MIT) added.

**Requirements + priorities live in ROADMAP.md — keep it updated when work
completes; don't grow a roadmap here.** Known deferred designs (context
mechanism constraints, TRUST-by-number, poll vote tallying) are captured there.

## Persona notes (agent/app/persona.py — edit carefully)

The persona is a warm Indian family-group member: light Hinglish, food-helpful,
joke-telling when asked, never automated-sounding, never rude/dismissive/repetitive
("Friends > schedule"). Structured output contract (`AgentOutput`): reply, meals,
suggested_dishes, selected_dish, is_food_related, react. If you change the
contract, update the persona JSON description in the same edit.
