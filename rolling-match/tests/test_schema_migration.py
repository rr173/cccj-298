from __future__ import annotations

import sqlite3

from app.db import connect, init_db


def test_init_db_upgrades_a_legacy_database_in_place(tmp_path):
    """A database created before mergers existed must gain the new columns,
    tables and indexes without losing its historical rows."""
    db_path = tmp_path / "legacy.db"

    # Build the legacy schema by stripping every merger-era object from the
    # current DDL script and creating the old-shaped tables directly.
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE campaigns (
                id TEXT PRIMARY KEY, name TEXT NOT NULL,
                total_pool_limit_cents INTEGER NOT NULL,
                donor_match_cap_cents INTEGER NOT NULL,
                created_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE TABLE projects (
                campaign_id TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
                is_ready INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                PRIMARY KEY (campaign_id, id)
            );
            CREATE TABLE donations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL,
                source_event_id TEXT NOT NULL, donor_id TEXT NOT NULL,
                project_id TEXT NOT NULL, amount_cents INTEGER NOT NULL,
                note TEXT, created_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                UNIQUE (campaign_id, source_event_id)
            );
            CREATE TABLE refunds (
                id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL,
                source_event_id TEXT NOT NULL UNIQUE,
                donation_id INTEGER NOT NULL, amount_cents INTEGER NOT NULL,
                reason TEXT, created_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE TABLE settlements (
                id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL,
                project_id TEXT NOT NULL, source_event_id TEXT NOT NULL UNIQUE,
                total_amount_cents INTEGER NOT NULL,
                donation_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'settled',
                created_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE TABLE grant_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                settlement_id INTEGER NOT NULL, campaign_id TEXT NOT NULL,
                project_id TEXT NOT NULL, donation_id INTEGER NOT NULL,
                amount_cents INTEGER NOT NULL, created_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE TABLE reversals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL,
                donation_id INTEGER NOT NULL, refund_id INTEGER NOT NULL,
                amount_cents INTEGER NOT NULL, reason TEXT,
                created_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                UNIQUE (refund_id, donation_id)
            );
            CREATE TABLE idempotency_records (
                campaign_id TEXT NOT NULL, operation TEXT NOT NULL,
                source_event_id TEXT NOT NULL, request_hash TEXT NOT NULL,
                status_code INTEGER NOT NULL, response_json TEXT NOT NULL,
                resource_type TEXT, resource_id TEXT,
                created_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                PRIMARY KEY (campaign_id, operation, source_event_id)
            );
            CREATE TABLE invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL,
                payee TEXT NOT NULL, vendor_invoice_number TEXT NOT NULL,
                total_amount_cents INTEGER NOT NULL,
                registration_hash TEXT NOT NULL, source_event_id TEXT NOT NULL,
                created_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                UNIQUE (campaign_id, payee, vendor_invoice_number)
            );
            CREATE TABLE invoice_shares (
                invoice_id INTEGER NOT NULL, campaign_id TEXT NOT NULL,
                project_id TEXT NOT NULL, share_cents INTEGER NOT NULL,
                created_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                PRIMARY KEY (invoice_id, project_id)
            );
            CREATE TABLE invoice_consents (
                invoice_id INTEGER NOT NULL, campaign_id TEXT NOT NULL,
                project_id TEXT NOT NULL, consented INTEGER NOT NULL DEFAULT 0,
                last_source_event_id TEXT, updated_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                PRIMARY KEY (invoice_id, project_id)
            );
            CREATE TABLE payment_instructions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL,
                invoice_id INTEGER NOT NULL,
                instruction_number TEXT NOT NULL UNIQUE,
                attempt_no INTEGER NOT NULL, payee TEXT NOT NULL,
                vendor_invoice_number TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                request_source_event_id TEXT NOT NULL,
                result_event_id TEXT, result_at TEXT,
                created_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                UNIQUE (invoice_id, attempt_no)
            );
            CREATE TABLE closings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL,
                period_no INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
                source_event_id TEXT NOT NULL, cutoff_at TEXT NOT NULL,
                content_json TEXT NOT NULL, content_hash TEXT NOT NULL,
                signable INTEGER NOT NULL, blockers_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                signed_at TEXT, discarded_at TEXT,
                UNIQUE (campaign_id, source_event_id)
            );
            INSERT INTO campaigns (id, name, total_pool_limit_cents,
                                   donor_match_cap_cents)
            VALUES ('c', 'legacy', 1000, 1000);
            INSERT INTO projects (campaign_id, id, name)
            VALUES ('c', 'a', 'A'), ('c', 'b', 'B');
            INSERT INTO invoice_shares (invoice_id, campaign_id, project_id,
                                        share_cents)
            VALUES (1, 'c', 'a', 500);
            """
        )
    finally:
        conn.close()

    # Running the current initializer migrates the legacy file in place.
    init_db(db_path)

    with connect(db_path) as conn:
        project_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(projects)")
        }
        assert {"status", "merged_into_project_id"} <= project_cols
        share_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(invoice_shares)")
        }
        assert "responsible_project_id" in share_cols
        grant_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(grant_items)")
        }
        assert "responsible_project_id" in grant_cols
        reversal_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(reversals)")
        }
        assert "responsible_project_id" in reversal_cols

        # New tables exist.
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"project_mergers", "internal_transfer_entries"} <= tables

        # Historical rows survive and backfill treats the original project as
        # responsible.
        row = conn.execute(
            "SELECT project_id, responsible_project_id FROM invoice_shares"
        ).fetchone()
        assert tuple(row) == ("a", "a")
        projects = conn.execute(
            "SELECT id, status FROM projects ORDER BY id"
        ).fetchall()
        assert [tuple(r) for r in projects] == [
            ("a", "active"),
            ("b", "active"),
        ]

    # A second init is an idempotent no-op (migration guards do not re-run).
    init_db(db_path)
    with connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM invoice_shares"
            ).fetchone()[0]
            == 1
        )


def test_fresh_database_contains_full_schema(tmp_path):
    db_path = tmp_path / "fresh.db"
    init_db(db_path)
    with connect(db_path) as conn:
        # The migration-created index exists on a fresh database too.
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_invoice_shares_responsible" in indexes
        # A grant item requires the responsible column and defaults work via
        # normal service flows (covered elsewhere); here just confirm NOT NULL
        # is enforced on a raw insert shape.
        cols = {
            r["name"]: r for r in conn.execute("PRAGMA table_info(grant_items)")
        }
        assert cols["responsible_project_id"]["notnull"] == 1
