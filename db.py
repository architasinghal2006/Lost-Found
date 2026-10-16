import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'admin')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);

-- D1: what makes an item findable.
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    finder_id INTEGER NOT NULL REFERENCES users(id),
    photo_path TEXT NOT NULL,
    public_description TEXT NOT NULL,
    category TEXT NOT NULL,
    zone TEXT NOT NULL,
    date_found TEXT NOT NULL,
    embedding BLOB NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'resolved')),
    resolved_at TEXT,
    closure_reason TEXT CHECK (closure_reason IN ('HANDED_OVER', 'WITHDRAWN', 'EXPIRED')),
    created_at TEXT NOT NULL,
    -- INV-5: a resolved item always has a timestamp and a reason code.
    CHECK ((status = 'active' AND resolved_at IS NULL AND closure_reason IS NULL)
        OR (status = 'resolved' AND resolved_at IS NOT NULL AND closure_reason IS NOT NULL))
);

-- D2: what proves ownership. Only claims.py touches this table.
CREATE TABLE IF NOT EXISTS private_attributes (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    label TEXT NOT NULL,
    answer TEXT NOT NULL,
    weight INTEGER NOT NULL CHECK (weight BETWEEN 1 AND 5)
);

CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    claimant_id INTEGER NOT NULL REFERENCES users(id),
    state TEXT NOT NULL,
    closure_cause TEXT,
    finder_confirmed_by INTEGER REFERENCES users(id),
    claimant_confirmed INTEGER NOT NULL DEFAULT 0,
    decision_justification TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS challenges (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER NOT NULL REFERENCES claims(id),
    attribute_id INTEGER NOT NULL REFERENCES private_attributes(id),
    question_text TEXT NOT NULL,
    disclosure_cost INTEGER NOT NULL,
    issued_at TEXT NOT NULL,
    UNIQUE (claim_id, attribute_id)
);

CREATE TABLE IF NOT EXISTS challenge_responses (
    id INTEGER PRIMARY KEY,
    challenge_id INTEGER NOT NULL UNIQUE REFERENCES challenges(id),
    response_text TEXT NOT NULL,
    similarity REAL NOT NULL,
    matched INTEGER NOT NULL,
    answered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verification_records (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER NOT NULL UNIQUE REFERENCES claims(id),
    score REAL NOT NULL,
    threshold_applied REAL NOT NULL,
    outcome TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- D4: disclosure budgets, one row per item and one per claim.
CREATE TABLE IF NOT EXISTS budgets (
    id INTEGER PRIMARY KEY,
    item_id INTEGER UNIQUE REFERENCES items(id),
    claim_id INTEGER UNIQUE REFERENCES claims(id),
    allowance INTEGER NOT NULL,
    consumed_total INTEGER NOT NULL DEFAULT 0,
    frozen INTEGER NOT NULL DEFAULT 0,
    frozen_value INTEGER,
    CHECK ((item_id IS NULL) <> (claim_id IS NULL)),
    CHECK (consumed_total <= allowance)
);

-- D5: append-only audit log.
CREATE TABLE IF NOT EXISTS item_events (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    claim_id INTEGER REFERENCES claims(id),
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility IN ('public', 'claimant', 'staff')),
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS item_events_no_update BEFORE UPDATE ON item_events
BEGIN SELECT RAISE(ABORT, 'item_events is append-only'); END;

CREATE TRIGGER IF NOT EXISTS item_events_no_delete BEFORE DELETE ON item_events
BEGIN SELECT RAISE(ABORT, 'item_events is append-only'); END;

CREATE TRIGGER IF NOT EXISTS items_no_delete BEFORE DELETE ON items
BEGIN SELECT RAISE(ABORT, 'items are soft-deleted only'); END;

CREATE TRIGGER IF NOT EXISTS budgets_monotonic BEFORE UPDATE OF consumed_total ON budgets
WHEN NEW.consumed_total < OLD.consumed_total
BEGIN SELECT RAISE(ABORT, 'budgets never go down'); END;
"""


def connect(path=None):
    path = path or os.environ.get("LF_DB", "lostfound.db")
    # isolation_level=None: we write BEGIN/COMMIT ourselves, so transactions are visible in the code.
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn):
    # BEGIN IMMEDIATE takes the write lock at the start, so concurrent writers are serialised.
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
