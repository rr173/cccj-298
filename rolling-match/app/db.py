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
    -- 'active' projects may receive donations, invoice shares, settlements
    -- and consent changes; a 'merged' project has been merged out exactly
    -- once and may never receive new obligations.  The row is retained so all
    -- historical records stay traceable under the original project id.
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'merged')),
    merged_into_project_id TEXT,
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
    -- The project responsible at grant time.  A settlement on a successor
    -- grants against donations whose original project was merged into it, so
    -- project_id (the donation's original project) and responsible_project_id
    -- may differ.  Without any merger they are identical.
    responsible_project_id TEXT NOT NULL,
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
-- responsible_project_id is the project responsible when the refund happened
-- (a refund against an old donation of a merged project is borne by the
-- current successor).
CREATE TABLE IF NOT EXISTS reversals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    donation_id INTEGER NOT NULL REFERENCES donations(id),
    refund_id INTEGER NOT NULL REFERENCES refunds(id),
    responsible_project_id TEXT NOT NULL,
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

-- ------------------------------------------------------------------
-- Joint reimbursement of one vendor invoice by several projects.
-- Shares are the only source of the invoice total and are immutable
-- after registration.
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    payee TEXT NOT NULL,
    vendor_invoice_number TEXT NOT NULL,
    total_amount_cents INTEGER NOT NULL CHECK (total_amount_cents > 0),
    registration_hash TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (campaign_id, payee, vendor_invoice_number)
);

CREATE INDEX IF NOT EXISTS idx_invoices_campaign_order
    ON invoices(campaign_id, id);
CREATE INDEX IF NOT EXISTS idx_invoices_vendor_number
    ON invoices(campaign_id, vendor_invoice_number);

CREATE TABLE IF NOT EXISTS invoice_shares (
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    -- project_id is the immutable original shareholder recorded at
    -- registration; responsible_project_id is the project currently bearing
    -- the share and is re-pointed along a merger chain when that shareholder
    -- is merged out.  Both stay queryable, so history remains traceable.
    project_id TEXT NOT NULL,
    responsible_project_id TEXT NOT NULL,
    share_cents INTEGER NOT NULL CHECK (share_cents > 0),
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (invoice_id, project_id),
    FOREIGN KEY (campaign_id, project_id)
        REFERENCES projects(campaign_id, id)
);

CREATE INDEX IF NOT EXISTS idx_invoice_shares_project
    ON invoice_shares(campaign_id, project_id);
-- idx_invoice_shares_responsible is created in _run_migrations after the
-- responsible_project_id column exists on every database version.

-- Each original shareholder consents separately at registration, and consent
-- is keyed by the original (immutable) project id.  A missing row means "not
-- yet consented".  When responsibility moves through a merger, the prior
-- consent of a transferred share is invalidated (consented reset to 0); the
-- current responsible project must re-confirm before an unpaid invoice may
-- queue again.  Rows are otherwise only updated through an idempotent source
-- event.
CREATE TABLE IF NOT EXISTS invoice_consents (
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    project_id TEXT NOT NULL,
    consented INTEGER NOT NULL DEFAULT 0 CHECK (consented IN (0, 1)),
    last_source_event_id TEXT,
    updated_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (invoice_id, project_id),
    FOREIGN KEY (campaign_id, project_id)
        REFERENCES projects(campaign_id, id)
);

-- One row per submission attempt.  The bank de-duplicates by the immutable
-- instruction_number; an invoice can have many failed attempts but never more
-- than one pending or one succeeded instruction at a time.
CREATE TABLE IF NOT EXISTS payment_instructions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    instruction_number TEXT NOT NULL UNIQUE,
    attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
    payee TEXT NOT NULL,
    vendor_invoice_number TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'succeeded', 'failed')),
    request_source_event_id TEXT NOT NULL,
    result_event_id TEXT,
    result_at TEXT,
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (invoice_id, attempt_no)
);

CREATE INDEX IF NOT EXISTS idx_payment_instructions_invoice
    ON payment_instructions(invoice_id, id);
CREATE INDEX IF NOT EXISTS idx_payment_instructions_campaign
    ON payment_instructions(campaign_id, id);
CREATE INDEX IF NOT EXISTS idx_payment_instructions_status
    ON payment_instructions(campaign_id, status);

-- Structural guarantees: at most one in-flight and one successful payment per
-- invoice.  Failed attempts are historical and may accumulate.
CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_one_pending
    ON payment_instructions(invoice_id)
    WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_one_succeeded
    ON payment_instructions(invoice_id)
    WHERE status = 'succeeded';

-- ------------------------------------------------------------------
-- Periodic sign-off statements ("closings").
--
-- A closing is a frozen reconciliation statement at a fixed cutoff.
-- Drafts can be signed or discarded; signed statements are immutable and
-- become the opening basis of the next period.  Only one draft may exist
-- per campaign, and only one signed statement may cover a period.
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS closings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    period_no INTEGER NOT NULL CHECK (period_no > 0),
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'signed', 'discarded')),
    source_event_id TEXT NOT NULL,
    cutoff_at TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    signable INTEGER NOT NULL CHECK (signable IN (0, 1)),
    blockers_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    signed_at TEXT,
    discarded_at TEXT,
    UNIQUE (campaign_id, source_event_id)
);

CREATE INDEX IF NOT EXISTS idx_closings_campaign_period
    ON closings(campaign_id, period_no, id);

-- At most one un-signed draft per campaign.  This is the structural part of
-- "one campaign has exactly one pending draft"; the service also checks it
-- inside BEGIN IMMEDIATE for a friendly error.
CREATE UNIQUE INDEX IF NOT EXISTS idx_closing_one_draft
    ON closings(campaign_id)
    WHERE status = 'draft';

-- A period can be signed exactly once, so no two statements ever cover the
-- same interval, even across restarts or concurrent sign calls.
CREATE UNIQUE INDEX IF NOT EXISTS idx_closing_one_signed_period
    ON closings(campaign_id, period_no)
    WHERE status = 'signed';

-- ------------------------------------------------------------------
-- Project handovers ("mergers").
--
-- A merger moves every remaining obligation and the book balance of one
-- active project (source) to another distinct, still-active project
-- (successor) in the same campaign.  It is effective immediately and
-- irreversible: a merged project can never be the source or target of
-- another merger directly, but a successor may itself be merged out later
-- (forming a responsibility chain).
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS project_mergers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    source_project_id TEXT NOT NULL,
    successor_project_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    -- Book balance transferred at the effective moment: actual spend already
    -- deducted, in-flight holds NOT deducted; may be negative.  The same
    -- amount is booked out of the source and into the successor.
    transferred_book_balance_cents INTEGER NOT NULL,
    effective_at TEXT NOT NULL,
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (campaign_id, source_event_id),
    -- A project can be merged out at most once.
    UNIQUE (campaign_id, source_project_id)
);

CREATE INDEX IF NOT EXISTS idx_project_mergers_campaign
    ON project_mergers(campaign_id, id);
CREATE INDEX IF NOT EXISTS idx_project_mergers_successor
    ON project_mergers(campaign_id, successor_project_id);

-- Internal transfer entries are the ledger lines produced by a merger, one
-- signed line per affected project in the period the merger takes effect.
-- Negative amounts leave a project, positive amounts enter one.  Each pair
-- references the same merger id so a statement can show the transfer-out,
-- the transfer-in and the merger record they belong to.
CREATE TABLE IF NOT EXISTS internal_transfer_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    merger_id INTEGER NOT NULL REFERENCES project_mergers(id),
    project_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('out', 'in')),
    amount_cents INTEGER NOT NULL,
    effective_at TEXT NOT NULL,
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (merger_id, project_id, direction)
);

CREATE INDEX IF NOT EXISTS idx_internal_transfer_project
    ON internal_transfer_entries(campaign_id, project_id, id);
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


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Upgrade databases created by earlier versions in place.

    New columns are added with a safe default and then backfilled from the
    historical rows so responsibility is traceable after a merger while
    pre-merger data keeps its original project as the responsible one.
    """
    project_cols = _column_names(conn, "projects")
    if "status" not in project_cols:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN status TEXT NOT NULL "
            "DEFAULT 'active' CHECK (status IN ('active', 'merged'))"
        )
    if "merged_into_project_id" not in project_cols:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN merged_into_project_id TEXT"
        )

    grant_cols = _column_names(conn, "grant_items")
    if "responsible_project_id" not in grant_cols:
        conn.execute(
            "ALTER TABLE grant_items ADD COLUMN responsible_project_id TEXT"
        )
        conn.execute(
            "UPDATE grant_items SET responsible_project_id = project_id "
            "WHERE responsible_project_id IS NULL"
        )

    reversal_cols = _column_names(conn, "reversals")
    if "responsible_project_id" not in reversal_cols:
        conn.execute(
            "ALTER TABLE reversals ADD COLUMN responsible_project_id TEXT"
        )
        conn.execute(
            """
            UPDATE reversals
            SET responsible_project_id = (
                SELECT d.project_id FROM donations d
                WHERE d.id = reversals.donation_id
            )
            WHERE responsible_project_id IS NULL
            """
        )

    share_cols = _column_names(conn, "invoice_shares")
    if "responsible_project_id" not in share_cols:
        conn.execute(
            "ALTER TABLE invoice_shares ADD COLUMN responsible_project_id TEXT"
        )
        conn.execute(
            "UPDATE invoice_shares SET responsible_project_id = project_id "
            "WHERE responsible_project_id IS NULL"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_invoice_shares_responsible "
        "ON invoice_shares(campaign_id, responsible_project_id)"
    )


def init_db(db_path: str | Path | None = None) -> None:
    path = resolve_db_path(db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        _run_migrations(conn)
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
