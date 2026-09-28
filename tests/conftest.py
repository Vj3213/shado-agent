"""Shared test setup.

Two things this must guarantee (both are known traps from AGENTS.md):

1. Tests never touch the live `meal_agent` database. We derive a sibling
   `meal_agent_test` database from DATABASE_URL and create/point everything at it.
2. AGENT_DRY_RUN never leaks from the shell into tests. We delete it up front;
   modules that specifically want dry-run set it themselves, deliberately.

Env vars must be pinned before `agent.app.config` / `agent.app.main` get
imported (config loads once at import time), so this file does it eagerly.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv
from psycopg import connect
from psycopg import errors as psycopg_errors

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DB = "meal_agent_test"

load_dotenv(PROJECT_ROOT / ".env")  # reads secrets/tunables; existing env stays authoritative

# --- 1. Pin the test environment BEFORE anything imports agent.app.config ---
_prod_url = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)
_test_url = _prod_url.rsplit("/", 1)[0] + "/" + TEST_DB

os.environ["DATABASE_URL"] = _test_url
os.environ.setdefault("GROUP_JID", "120363test@g.us")
# Force (not setdefault): .env may carry PEER_BOT_SENDER=TODO — the flow test
# must exercise the peer-bot path deterministically, whatever prod uses.
os.environ["PEER_BOT_SENDER"] = "919999000111@s.whatsapp.net"
os.environ["BOT_TO_BOT_MAX_TURNS"] = "2"
# Console routing keys off OPERATOR_JID — pin a test value so the HTTP-flow
# tests are deterministic no matter what the real .env carries.
os.environ["OPERATOR_JID"] = "919999000112@s.whatsapp.net"
# The deliberate unset: no dry-run leakage. test GeminiClient uses its own flag;
# the HTTP-flow module sets =1 itself right before importing agent.app.main.
os.environ.pop("AGENT_DRY_RUN", None)

TEST_DATABASE_URL = _test_url
RUNNING_TEST_GROUP = os.environ["GROUP_JID"]
RUNNING_TEST_PEER = os.environ["PEER_BOT_SENDER"]


@pytest.fixture(scope="session", autouse=True)
def isolated_test_database():
    """Create a throwaway database next to the live one; wipe it afterwards."""
    _drop_test_database()
    with connect(_prod_url, autocommit=True) as admin:
        with admin.cursor() as cur:
            cur.execute('CREATE DATABASE "%s"' % TEST_DB)
    yield TEST_DATABASE_URL
    _drop_test_database()


def _drop_test_database() -> None:
    try:
        with connect(_prod_url, autocommit=True) as admin:
            with admin.cursor() as cur:
                cur.execute('DROP DATABASE "%s" WITH (FORCE)' % TEST_DB)
    except psycopg_errors.InvalidCatalogName:
        pass  # first run: nothing to drop yet


@pytest.fixture(autouse=True)
def clean_tables(isolated_test_database):
    """Fresh tables for every test (TRUNCATE is fast and resets all state)."""
    from storage.postgres_repo import PostgresRepository

    PostgresRepository(TEST_DATABASE_URL).init_schema()
    with connect(TEST_DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "TRUNCATE messages, meal_log, bot_loop_state, suggestion, "
            "chat_consents, learned_reactions"
        )
    yield


@pytest.fixture
def repo(clean_tables):
    from storage.postgres_repo import PostgresRepository

    return PostgresRepository(TEST_DATABASE_URL)
