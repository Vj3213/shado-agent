-- whatsapp-meal-agent schema (Postgres)
-- Rolling conversation window: old rows can be pruned; nothing else is kept forever.

CREATE TABLE IF NOT EXISTS messages (
    id         BIGSERIAL PRIMARY KEY,
    group_id   TEXT NOT NULL,
    sender     TEXT NOT NULL,
    text       TEXT NOT NULL,
    direction  TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    source     TEXT NOT NULL DEFAULT 'agent' CHECK (source IN ('operator', 'agent')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE messages ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'agent';

CREATE INDEX IF NOT EXISTS idx_messages_source
    ON messages (group_id, source, id DESC);

CREATE INDEX IF NOT EXISTS idx_messages_group_recent
    ON messages (group_id, created_at DESC);

CREATE TABLE IF NOT EXISTS meal_log (
    id         BIGSERIAL PRIMARY KEY,
    day        DATE NOT NULL,
    item       TEXT NOT NULL,
    meal_type  TEXT NOT NULL DEFAULT 'snack'
               CHECK (meal_type IN ('breakfast', 'lunch', 'dinner', 'snack')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_meal_log_day ON meal_log (day);

-- One row per (group, peer bot). The loop-cap counter lives here.
CREATE TABLE IF NOT EXISTS bot_loop_state (
    group_id          TEXT NOT NULL,
    peer_sender       TEXT NOT NULL,
    consecutive_turns INT  NOT NULL DEFAULT 0 CHECK (consecutive_turns >= 0),
    last_reset_at     TIMESTAMPTZ,
    PRIMARY KEY (group_id, peer_sender)
);

-- ===== Food knowledge base (RAG-lite: SQL retrieval, no embeddings needed) =====

CREATE TABLE IF NOT EXISTS ingredient (
    id           SERIAL PRIMARY KEY,
    name         TEXT UNIQUE NOT NULL,          -- canonical English name
    category     TEXT NOT NULL CHECK (category IN
                 ('vegetable', 'fruit', 'lentil', 'grain', 'sprout', 'dairy', 'protein')),
    common_names TEXT NOT NULL DEFAULT '',       -- Hindi/aliases, comma-separated
    season       TEXT NOT NULL DEFAULT 'all'     -- csv of months, or 'all'
);
ALTER TABLE ingredient ADD COLUMN IF NOT EXISTS season TEXT NOT NULL DEFAULT 'all';

CREATE TABLE IF NOT EXISTS dish (
    id         SERIAL PRIMARY KEY,
    name       TEXT UNIQUE NOT NULL,
    meal_types TEXT NOT NULL DEFAULT 'lunch,dinner'   -- csv: breakfast,lunch,dinner,snack
);

CREATE TABLE IF NOT EXISTS dish_ingredient (
    dish_id       INT NOT NULL REFERENCES dish(id) ON DELETE CASCADE,
    ingredient_id INT NOT NULL REFERENCES ingredient(id) ON DELETE CASCADE,
    PRIMARY KEY (dish_id, ingredient_id)
);
CREATE INDEX IF NOT EXISTS idx_dish_ingredient_ing ON dish_ingredient (ingredient_id);

-- Lifecycle: suggested -> pending (auto-demoted when a new round starts)
--           -> selected (user confirmed) / rejected
CREATE TABLE IF NOT EXISTS suggestion (
    id         SERIAL PRIMARY KEY,
    group_id   TEXT NOT NULL,
    dish       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'suggested'
               CHECK (status IN ('suggested', 'pending', 'selected', 'rejected')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_suggestion_group ON suggestion (group_id, created_at DESC);

-- Emojis the operator has personally used as reactions — Shado learns these.
CREATE TABLE IF NOT EXISTS learned_reactions (
    emoji      TEXT PRIMARY KEY,
    times      INT NOT NULL DEFAULT 1 CHECK (times >= 1),
    learned_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Same idea, per chat: how the operator reacts HERE (mirroring material).
CREATE TABLE IF NOT EXISTS chat_reactions (
    chat_id    TEXT NOT NULL,
    emoji      TEXT NOT NULL,
    times      INT NOT NULL DEFAULT 1 CHECK (times >= 1),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, emoji)
);

-- Sender display names (WhatsApp pushName), for readable consent prompts.
CREATE TABLE IF NOT EXISTS contacts (
    jid        TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Trusted entries the operator pre-declares in their DM — by NAME or NUMBER:
-- the first sender matching an unbound entry (within scope) is auto-approved
-- for 24h TTL — no ask. Numbers win over names (precise, unspoofable).
-- TOFU: the match binds to that jid; another jid with the same name is NOT
-- auto-approved (falls back to the normal ask). STOP deletes the watch row.
CREATE TABLE IF NOT EXISTS trusted_names (
    id         SERIAL PRIMARY KEY,
    name       TEXT,
    number     TEXT,
    scope      TEXT NOT NULL DEFAULT 'any',
    bound_jid  TEXT,
    bound_chat TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name, scope),
    UNIQUE (number, scope)
);
ALTER TABLE trusted_names ADD COLUMN IF NOT EXISTS number TEXT;
ALTER TABLE trusted_names ALTER COLUMN name DROP NOT NULL;
ALTER TABLE trusted_names ALTER COLUMN number DROP NOT NULL;

-- Consent-gated dynamic allowlist, controlled from the operator's self-chat:
-- pending (asked once) -> granted (TTL) / declined (silent forever)
--                      -> revoked (re-asks on next message)
-- `remembered`: the operator approved this chat at least once — expiry then
-- silently re-grants (no re-ask). STOP clears it so a stop really stops.
CREATE TABLE IF NOT EXISTS chat_consents (
    chat_id    TEXT PRIMARY KEY,
    status     TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'granted', 'declined', 'revoked')),
    granted_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    remembered BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE chat_consents ADD COLUMN IF NOT EXISTS remembered BOOLEAN NOT NULL DEFAULT FALSE;

-- Operator-curated per-chat context ("REMEMBER <chat> <note>"): injected into
-- that chat's prompt while fresh; expired notes are ignored, not deleted —
-- the retention sweep removes them.
CREATE TABLE IF NOT EXISTS chat_context (
    id         BIGSERIAL PRIMARY KEY,
    chat_id    TEXT NOT NULL,
    note       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '7 days'
);
CREATE INDEX IF NOT EXISTS idx_chat_context_chat ON chat_context (chat_id, id DESC);

-- Ask-for-context rate limiting (one ask per chat per cooldown window).
CREATE TABLE IF NOT EXISTS context_asks (
    chat_id     TEXT PRIMARY KEY,
    last_ask_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===== Mimicry Phase 2: the background distiller =====

-- Voice fingerprint: HOW the operator writes in this chat. One row per chat,
-- overwritten on every distill run — the most recent voice always wins.
CREATE TABLE IF NOT EXISTS style_profile (
    chat_id    TEXT PRIMARY KEY,
    profile    TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Durable facts extracted from the chat's life (TTL'd; re-mentioned facts
-- get their expiry refreshed on each distill run).
CREATE TABLE IF NOT EXISTS chat_facts (
    id         BIGSERIAL PRIMARY KEY,
    chat_id    TEXT NOT NULL,
    fact       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '30 days'
);
CREATE INDEX IF NOT EXISTS idx_chat_facts_chat ON chat_facts (chat_id, id DESC);

-- Distiller bookkeeping: operator-message counter since last run + run stamp.
CREATE TABLE IF NOT EXISTS distill_state (
    chat_id             TEXT PRIMARY KEY,
    operator_msgs_since INT NOT NULL DEFAULT 0,
    last_run_at         TIMESTAMPTZ
);
