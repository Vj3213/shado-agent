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
`/messages/reaction`, `GET /health`. Agent binds 127.0.0.1:8100; relay binds 127.0.0.1:8090.

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

## Running

```bash
bash scripts/run_all.sh        # ONLY when launchd services are uninstalled
bash scripts/service.sh install|status|logs|restart|uninstall   # launchd (3 services, boot-persistent)
```

- If launchd services are loaded, `run_all.sh` refuses to run — that's correct.
- `sudo pmset -a sleep 0` was needed once so the Mac never system-sleeps.
- Logs: `~/Library/Logs/shado/*.log`. Console noise from libsignal is filtered in `gateway/src/index.ts`.

## Key behaviors (all structural, not prompt-promises)

- **Consent**: unknown chats never get replies until the operator says `YES`
  in their DM (`OPERATOR_JID` = the console). `NO` = permanent silence, `ALLOW <n>` revives.
  A YES/ALLOW also REPLIES to the message that triggered the ask: the agent
  generates a reply from the chat's recent history and returns it as
  `Decision.deliver {chat_id, text}`; the gateway sends it human-like. (Older
  than one session window → nothing pending → no deliver.)
- **Consent polls**: the gateway attaches a YES/NO poll after every consent
  prompt; a tap is decrypted by the gateway (`consent-poll.ts`, own
  messageSecret per poll, persisted in `gateway/auth/consent-polls.json`,
  votes map by SHA-256(optionName) hex) and forwarded as a targeted
  `yes <chat>` / `no <chat>` console command — more precise than plain
  `yes`/`no`, which still work and stay the fallback if decryption fails.
- **Model fallback chain**: `gemini-3.5-flash-lite` → `3.6-flash` → `3.8-flash`
  (1 Gemini round if OpenRouter configured) → OpenRouter `:free` models →
  live-catalog self-heal → clean silence on total failure. 503/504/429/404 fall
  through; 401/403 abort.
- **Human-like send**: randomized delay = reading(incoming len) + typing(reply len)
  + base + extra 1–5s, capped 20s; composing presence; one message per turn.
- **Reactions**: two-tier policy. PRIMARY: the model reacts only when it is
  confident the USER themself would have tapped a reaction (`user_would_react`
  in the contract) — mirroring their habits; never rate-limited. SECONDARY:
  the model's own impulse — gated by a per-chat cooldown
  (`config.reactions.cooldown_seconds`, 120s). The model outputs the emoji
  only; the gateway attaches it to the triggering message key (the model can't
  pick targets). Whitelist: `learned_reactions` first (the user's own
  vocabulary, frequency-ordered) ∪ `config.reactions.allowed`.
- **Style mirroring**: last 10 operator messages of the chat injected as
  `STYLE EXAMPLES`; persona mirrors length/Hinglish/emoji but never claims to BE
  the operator. Warmth rules: never rude/dismissive/repetitive; jokes get told.
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

All three launchd services healthy, real Gemini active, WhatsApp linked via
`gateway/auth/`. Secrets in `.env` (never committed — verified). LICENSE (MIT)
added. NOT yet done (roadmap, in rough order):

1. Commit + push to GitHub (repo name `shado-agent`)
2. Consolidate tests into pytest + a couple of gateway vitest specs — DONE
   (tests/test_agent_flow.py replaced scripts/test_agent_flow.sh; vitest
   specs for delay math, extractText, chatAllowed; pytest 9.1.1; vitest
   4.1.11 pinned; both audits clean)
3. GitHub Actions CI: typecheck + both audits on every push — DONE
   (.github/workflows/ci.yml)
4. WhatsApp polls ("kya banau aaj?" as a native poll) — DONE (v1 send-only:
   AgentOutput.poll contract, gateway sendPollIfAny; votes not read yet)
5. Mimicry Phase 2: learned voice fingerprint (one Gemini call per ~100 operator
   messages → structured profile → `style_profile` table → injected into prompts)
6. Mimicry Phase 3: mirror the operator's reply-latency distribution
7. Voice notes: Gemini TTS → ffmpeg → OGG/Opus → `{ ptt: true }` (needs ffmpeg)
8. Telegram gateway (official Bot API — first platform-agnosticism proof)
9. Posting: draft → operator approval → X/LinkedIn (public blast radius — always approval-gated)
10. Retention job: prune messages/suggestions older than N days

## Persona notes (agent/app/persona.py — edit carefully)

The persona is a warm Indian family-group member: light Hinglish, food-helpful,
joke-telling when asked, never automated-sounding, never rude/dismissive/repetitive
("Friends > schedule"). Structured output contract (`AgentOutput`): reply, meals,
suggested_dishes, selected_dish, is_food_related, react. If you change the
contract, update the persona JSON description in the same edit.
