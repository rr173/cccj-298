"""SQLite connection and schema management."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = "rolling_match.db"

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    total_pool_limit_cents INTEGER NOT NULL
        CHECK (total_pool_limit_cents >= 0),
    donor_match_cap_cents INTEGER NOT NULL
        CHECK (donor_match_cap_cents >= 0),
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS projects (
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    is_ready INTEGER NOT NULL DEFAULT 1 CHECK (is_ready IN (0, 1)),
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (campaign_id, id)
);

CREATE TABLE IF NOT EXISTS donations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    source_event_id TEXT NOT NULL,
    donor_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    note TEXT,
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (campaign_id, project_id)
        REFERENCES projects(campaign_id, id),
    UNIQUE (campaign_id, source_event_id)
);

CREATE INDEX IF NOT EXISTS idx_donations_order
    ON donations(campaign_id, id);
CREATE INDEX IF NOT EXISTS idx_donations_donor
    ON donations(campaign_id, donor_id);
CREATE INDEX IF NOT EXISTS idx_donations_project
    ON donations(campaign_id, project_id);

CREATE TABLE IF NOT EXISTS refunds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    source_event_id TEXT NOT NULL UNIQUE,
    donation_id INTEGER NOT NULL REFERENCES donations(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    reason TEXT,
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_refunds_donation
    ON refunds(donation_id);

-- A settlement is one project-level realization event.  Grant items are the
-- immutable details against each original donation order.
CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    project_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL UNIQUE,
    total_amount_cents INTEGER NOT NULL
        CHECK (total_amount_cents >= 0),
    donation_count INTEGER NOT NULL DEFAULT 0
        CHECK (donation_count >= 0),
    status TEXT NOT NULL DEFAULT 'settled'
        CHECK (status IN ('settled', 'no_eligible_match')),
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (campaign_id, project_id)
        REFERENCES projects(campaign_id, id)
);

CREATE INDEX IF NOT EXISTS idx_settlements_project
    ON settlements(campaign_id, project_id, id);

CREATE TABLE IF NOT EXISTS grant_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id INTEGER NOT NULL REFERENCES settlements(id),
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    project_id TEXT NOT NULL,
    donation_id INTEGER NOT NULL REFERENCES donations(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_grant_items_settlement
    ON grant_items(settlement_id);
CREATE INDEX IF NOT EXISTS idx_grant_items_donation
    ON grant_items(donation_id);
CREATE INDEX IF NOT EXISTS idx_grant_items_project
    ON grant_items(campaign_id, project_id, id);

-- Actual correction/reversal records.  Old grant_items are never updated or
-- deleted; a reversal is a separate negative correction against an order.
CREATE TABLE IF NOT EXISTS reversals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    donation_id INTEGER NOT NULL REFERENCES donations(id),
    refund_id INTEGER NOT NULL REFERENCES refunds(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    reason TEXT,
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (refund_id, donation_id)
);

CREATE INDEX IF NOT EXISTS idx_reversals_donation
    ON reversals(donation_id);
CREATE INDEX IF NOT EXISTS idx_reversals_campaign
    ON reversals(campaign_id, id);

-- Operation-scoped idempotency.  request_hash covers every business parameter,
-- including campaign and the source id itself.
CREATE TABLE IF NOT EXISTS idempotency_records (
    campaign_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (campaign_id, operation, source_event_id)
);
"""


def resolve_db_path(db_path: str | Path | None = None) -> str:
    import os

    return str(db_path or os.environ.get("ROLLING_MATCH_DB") or DEFAULT_DB_PATH)


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = resolve_db_path(db_path)
    conn = sqlite3.connect(
        path,
        timeout=30.0,
        check_same_thread=False,
        isolation_level=None,  # explicit BEGIN IMMEDIATE transactions
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if path != ":memory:":
        # WAL improves concurrent reader/writer behavior for a file database.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(db_path: str | Path | None = None) -> None:
    path = resolve_db_path(db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def read_conn(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_transaction(
    db_path: str | Path | None = None,
) -> Iterator[sqlite3.Connection]:
    """Serialize writers with SQLite's reservation lock.

    BEGIN IMMEDIATE acquires the write lock before any read.  Consequently,
    idempotency checks and balance/grant calculations in the yielded block see
    a stable committed state, and concurrent writers wait rather than creating
    interleaved half-batches.
    """

    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
