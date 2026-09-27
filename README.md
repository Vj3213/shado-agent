# WhatsApp Agent

A two-way AI agent that joins an existing private WhatsApp group **as a normal
linked device** (not a Meta Business bot), chats like a human member, suggests
meals, tracks what the family has already eaten, and interacts naturally with
the other meal-suggestion bot already in the group — with a hard cap on
bot-to-bot conversations.

> ⚠️ Honest caveats: Baileys is an unofficial library and automating a WhatsApp
> account technically violates WhatsApp's ToS — there is a real risk of the
> number being banned. Use a **spare number**, keep traffic low (this project's
> trigger + loop-cap design exists for that reason). `npm audit` will also
> complain about transitive deps inside Baileys; that is normal for this library.

## Architecture

```
                    ┌───────────────────────────────────────────┐
 WhatsApp group ───▶│  gateway/ (TypeScript + Baileys 6.7.24)   │
                    │  connect / receive / send ONLY            │
                    │  - allowlist filter (GROUP_JID)           │
                    │  - ignore own sends (fromMe)              │
                    │  - human-like delay + composing presence  │
                    └──────────────┬────────────▲───────────────┘
                                   │ local HTTP │ reply text
                                   ▼            │
                    ┌───────────────────────────────────────────┐
                    │  agent/ (Python + FastAPI + Gemini Flash) │
                    │  triggers → policy → persona prompt → LLM │
                    └──────────────┬────────────────────────────┘
                                   │ repository interface only
                                   ▼
                    ┌───────────────────────────────────────────┐
                    │  storage/ (psycopg + Postgres)            │
                    │  messages · meal_log · bot_loop_state     │
                    └───────────────────────────────────────────┘
```

Four decoupled modules — none of them reaches past its own boundary:

| Module | Owns | Deliberately does NOT know |
|---|---|---|
| `gateway/` | WhatsApp socket, filters, delays, composing presence | AI, SQL, triggers |
| `agent/` | triggers, loop-cap policy, persona, Gemini call | WhatsApp protocol |
| `storage/` | all SQL, **atomic loop-cap enforcement** | why the data exists |
| `config/` + `.env` | every tunable + every secret | nothing — it's data |

Tunables (trigger words, prefixes, delay ranges, context window) live in
`config/settings.json`; deployment values and secrets live in `.env`. Nothing
is hardcoded in logic.

## Quickstart

Prerequisites: Node ≥ 20.10, Python 3.10+, a running local Postgres
(`meal_agent` DB), and a spare WhatsApp number.

```bash
# 1. clone + install
git clone <repo-url> && cd whatsapp-agent
python3 -m venv .venv && .venv/bin/pip install -r agent/requirements.txt
cd gateway && npm install && cd ..

# 2. configure (never commit this file)
cp .env.example .env    # then fill: GEMINI_API_KEY, DATABASE_URL
# leave GROUP_JID=TODO — first run is discovery mode and tells you what to copy

# 3. run everything (Postgres check → schema+seed → agent → gateway)
bash scripts/run_all.sh

# 4. scan the QR with the spare phone, send a message in the target group,
#    copy the [discovery] JID into .env, restart — live. Details below.
```

## Setup (one-time)

### 1. Postgres + Node

Installed via Homebrew (Postgres 15.3) plus the official **Node 22 LTS**
tarball at `/usr/local/opt/node-lts` — brew's tap was too old to offer a Node
that supports Baileys' import-attribute syntax (`import ... with { type: 'json' }`
needs Node ≥ 20.10). PATH is wired in `~/.zshrc`. Postgres is **not** a login
service here — start it manually (or just use `scripts/run_all.sh`, which does):

```bash
LC_ALL="C" /usr/local/opt/postgresql@15/bin/postgres -D /usr/local/var/postgresql@15 &
export PATH="/usr/local/opt/postgresql@15/bin:$PATH"
createdb meal_agent   # once
```

### 2. Secrets + values

```bash
cp .env.example .env   # then edit
```

Fill in: `GEMINI_API_KEY` (Google AI Studio free tier), `DATABASE_URL`,
`PEER_BOT_SENDER` (the other bot's JID), `GROUP_JID`. `.env` is gitignored,
and opencode's global config (`~/.config/opencode/opencode.jsonc`) denies AI
read access to `.env` files and `gateway/auth/`. Never commit it.

### 3. Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r agent/requirements.txt
cd gateway && npm install && cd ..
```

### 4. Schema

```bash
.venv/bin/python scripts/init_db.py
```

## Finding GROUP_JID and PEER_BOT_SENDER

1. Leave `GROUP_JID=TODO` in `.env`, start the gateway (below), and send any
   message in the target group. The gateway runs in **discovery mode** and logs
   `[discovery] group seen: 120363...@g.us` — copy that JID into `.env`.
   Note: only *group* chats appear here — direct 1:1 messages are ignored by
   design, so make sure the bot number is a member of the group and test there.
2. Re-run. The agent now starts too (in DRY_RUN if `GEMINI_API_KEY` isn't
   added yet) — replying to humans already works here. When the *other* bot
   eventually posts, its JID appears in the `[recv] <sender_jid>: ...` line;
   copy it into `PEER_BOT_SENDER` to activate peer-bot engagement + the loop
   cap. Sender JIDs may look like `127...@lid` — WhatsApp's newer member-ID
   format; copy it exactly as shown.
3. Add `GEMINI_API_KEY` (or leave `AGENT_DRY_RUN=1`) — you're live.

## Run

One command (Postgres → agent → gateway, in the right order):

```bash
bash scripts/run_all.sh
```

Or manually, terminal order matters (Postgres → agent → gateway):

```bash
# 1. Postgres (if not running)
LC_ALL="C" /usr/local/opt/postgresql@15/bin/postgres -D /usr/local/var/postgresql@15 &

# 2. Agent — with the real Gemini key
.venv/bin/python -m uvicorn agent.app.main:app --host 127.0.0.1 --port 8100

# 2b. Agent — DRY RUN (no Gemini key needed; canned replies)
AGENT_DRY_RUN=1 .venv/bin/python -m uvicorn agent.app.main:app --host 127.0.0.1 --port 8100

# 3. Gateway (first run: scan the QR with the spare phone, or set
#    LINK_PHONE_NUMBER in .env to use a pairing code)
cd gateway && npm run dev
```

Session credentials persist in `gateway/auth/` (gitignored) — no re-scan on
restart. Logged out? Delete `gateway/auth/` and link again.

## Behavior rules (enforced, not prompted)

- **Allowlist:** the configured `GROUP_JID` is processed, plus **private 1:1
  chats** when `allow_private_chats` is true in `config/settings.json` (DM
  senders appear as `...@s.whatsapp.net` / `...@lid`). Other groups and chats
  are ignored; own outgoing messages via `fromMe`.
- **Consent-gated chats (operator console):** unknown chats never get replies
  automatically. First message from a chat that isn't the allowlisted group →
  the bot asks *you* in your own "Message yourself" chat (who asked, what they
  said) — once, not per message. Reply there: `YES` (agent handles that chat
  for `consent_ttl_hours`, default 24h) · `NO` (silent forever) · `LIST` ·
  `STOP` (end all) or `STOP 2` (end one, by LIST number). Multiple chats can
  be agent-handled at once — each keeps its own conversation context.
  Sending a manual message from the bot phone to a new chat triggers the same
  consent ask. Consent state lives in `chat_consents` and survives restarts.
  Allowlisted groups/private settings keep their static behavior.
- **Bot-initiated messages:** the gateway runs a tiny local relay
  (`127.0.0.1:GATEWAY_PORT`):
  `.venv/bin/python scripts/initiate.py` (group) or `... initiate.py dm`
  (uses `DM_JID` from `.env`) — the agent composes a proactive opener
  (e.g. asking about a pending suggestion) and the gateway sends it
  human-like. Operator-triggered only — no auto-spam.
- **Triggers** (from `config/settings.json`): by default `reply_to_everything: true`
  — every allowlisted-group message goes to the model, which decides relevance and
  may still decline pure noise (returns null → silence). Set it to `false` for the
  classic keyword/prefix filter + follow-up-window behavior.
- **Food KB (RAG-lite)**: `ingredient` → `dish` → `dish_ingredient` tables seeded
  with ~72 Indian ingredients (with Hindi aliases) and ~86 dishes. Retrieval: find
  ingredient mentions in the session text → their dishes → subtract recently-eaten
  (3 days) and already-suggested (2 days) → pass the pool to Gemini, which picks
  2–3 exact dish names. No embeddings needed at this scale.
- **Suggestion lifecycle**: every suggestion is stored; new rounds auto-demote old
  ones `suggested → pending`; user confirmation marks `selected`. The model sees
  this history, so it can follow up naturally ("Aloo paratha banaya tha parson?")
  and never repeats itself.
- **Session context**: the prompt includes the whole conversation from the last
  `session_hours` (6h, capped at `window` messages) — not just the last N lines.
- **Bot-to-bot loop cap** (`BOT_TO_BOT_MAX_TURNS`, default 20): consecutive
  turns with the peer bot are counted in `bot_loop_state` and enforced with a
  conditional SQL update — the increment is *refused in the database* once the
  cap is hit, so a prompt regression can't cause an infinite bot fight. Any
  human message resets the counter.
- **Human-like send:** randomized delay (base + per-character typing time +
  jitter, capped), WhatsApp "composing…" presence, exactly one message per
  trigger — never instant, never bursty.
- **Model fallback chain:** replies try `GEMINI_MODEL` first, then
  `gemini-3.6-flash`, then `gemini-3.5-flash-lite` (from
  `config/settings.json`, overridable via `GEMINI_FALLBACK_MODELS`). Capacity
  errors (429/5xx/404) move down the chain; auth errors abort; if every model
  fails the turn is silently skipped (no 500, no half-send). The newest flash
  model gets priority but also the worst launch-time 503s; the Flash-Lite end
  of the chain is deliberately the most available. Note: `gemini-2.5-flash-lite`
  is closed to new API users (Google says migrate to `gemini-3.5-flash-lite`).
  Avoid `-preview` models — Google expires them. See what your key can use:
  `.venv/bin/python scripts/list_models.py`.
- **Cross-provider last resort (OpenRouter):** when *every* Google model fails
  (whole-family 503/504 outages happen on the free tier), the chain hands off
  to OpenRouter's free models (same prompt, same structured output — the agent
  logic doesn't care which provider served the reply). Set `OPENROUTER_API_KEY`
  in `.env` (free account at openrouter.ai); optionally `OPENROUTER_MODELS=`
  comma list (current `:free` ids at openrouter.ai/models?max_price=0).
  Without a key the leg is disabled and Google-only behavior applies.
- **Meal memory:** one structured Gemini call returns the reply *and* any
  clearly-stated eaten foods, which land in `meal_log` for the day and feed
  future suggestions.

## Data model

- `messages(id, group_id, sender, text, direction 'in'|'out', created_at)` —
  rolling conversation window for context.
- `meal_log(id, day, item, meal_type, created_at)` — what was eaten, per day.
- `bot_loop_state(group_id, peer_sender, consecutive_turns, last_reset_at)` —
  loop-cap counter.
- `ingredient(id, name, category, common_names)` — vegetables, fruits, dals,
  grains/millets, sprouts, dairy (Hindi aliases for matching).
- `dish(id, name, meal_types)` + `dish_ingredient(dish_id, ingredient_id)` —
  the food KB the suggestions are grounded in.
- `suggestion(id, group_id, dish, status, created_at)` — what the bot
  suggested and whether it was confirmed: `suggested → pending → selected`.

## End-to-end test plan

### A. Pipeline without WhatsApp (done — all passing)

1. `scripts/init_db.py` creates the 3 tables.
2. Storage: message ordering, meal round-trip, **cap blocks at the limit**,
   human reset works.
3. Agent (DRY_RUN) over HTTP: prefix → `trigger_prefix` reply; non-trigger →
   `no_trigger`; peer turns count 1, 2 then `bot_loop_cap_reached`; human
   message resets and peer can talk again; wrong group → 403.

Re-run the HTTP scenario with curl (see `scripts/test_agent_flow.sh`).

### B. Full stack with WhatsApp

1. Start Postgres, agent (DRY_RUN), gateway; scan QR with the spare phone.
2. Send "ved: khana kya banau?" in the group → bot replies after a visible
   random delay with a dry-run reply. Confirm `[send] waiting XXXms` in logs.
3. Send "good morning" (no trigger) → silence, `[agent] no reply (no_trigger)`.
4. Send "had poha for breakfast" → reply acknowledges it; check
   `SELECT * FROM meal_log` shows poha/breakfast.
5. Let the other bot post 3 times → replies 1-2 fire, 3rd is silent with
   `bot_loop_cap_reached` in agent logs (set `BOT_TO_BOT_MAX_TURNS=2` for a
   quick test).
6. Post a human message → counter resets (verify next peer reply works).
7. Switch the agent to the real Gemini key and repeat 2-5 — replies should now
   sound like a group member, consider today's meal log, and occasionally
   politely disagree with the other bot's suggestion.

### C. Hygiene checks

- Restart the gateway → no QR re-scan (session persisted).
- `git status` → `.env`, `gateway/auth/` never staged.
- Kill the agent while the gateway runs → gateway logs the HTTP failure and
  stays connected; no crash, no send.

## Security notes

- **Secrets**: `.env` (Gemini/OpenRouter keys, DB URL) and `gateway/auth/`
  (WhatsApp session) are gitignored — verified never committed. If a key ever
  leaks: rotate it in Google AI Studio / openrouter.ai and re-link the phone
  (delete `gateway/auth/`).
- **Network surface**: both services bind `127.0.0.1` only (agent `:8100`,
  relay `:8090`). Nothing is reachable from the network. The local endpoints
  have no auth — acceptable for a personal machine, but anything running under
  your user can message as the bot; don't run untrusted local code.
- **SQL**: all queries are parameterized (psycopg placeholders); no string-built SQL.
- **Prompt injection**: chat members can try to influence the LLM — mitigations
  are structural (chat allowlist, bot-to-bot loop cap enforced in SQL, human-like
  rate limits, one-message-per-turn), not prompt promises. The persona can still
  be social-engineered by someone in the chat; treat it as a known limitation.
- **Dependencies**: pinned exact; `pip-audit` and `npm audit` clean at time of
  writing. Re-run both periodically.
- **Data**: the Postgres DB lives outside the repo; nothing family-related is
  committed. Logs (message text, JIDs) go to your terminal only — don't paste
  them into public issues.

## Learning notes (why it's built this way)

- **Gateway/brain split** lets you develop and test the AI against a log file
  instead of a live WhatsApp session, and swap WhatsApp out entirely.
- **Repository protocol** means Postgres could become SQLite with one new
  class; the agent can't tell the difference.
- **The cap lives in SQL**, because "the LLM promised not to loop" is not
  enforcement — a `WHERE consecutive_turns < cap` in the UPDATE is.
- **Structured output** (JSON schema) instead of free text means meal
  extraction is part of the same call: cheaper, and the reply and the log can't
  disagree.
