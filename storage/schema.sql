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

-- Consent-gated dynamic allowlist, controlled from the operator's self-chat:
-- pending (asked once) -> granted (TTL) / declined (silent forever)
--                      -> revoked (re-asks on next message)
CREATE TABLE IF NOT EXISTS chat_consents (
    chat_id    TEXT PRIMARY KEY,
    status     TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'granted', 'declined', 'revoked')),
    granted_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
