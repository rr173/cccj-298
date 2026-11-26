"""Locally runnable simulated bank payment channel.

This module deliberately stands apart from the campaign ledger.  It persists
acceptance and results in its own SQLite database so it can be treated as an
external payment provider during local demos and tests.  No real bank or
network is involved.

Guarantees:

* an instruction number identifies exactly one payment (submission is
  de-duplicated by number);
* a payment can only move ``pending -> succeeded`` or ``pending -> failed``;
* delivering the same terminal result again is a harmless duplicate;
* a contradictory terminal result never overwrites the first confirmed one;
* results for one instruction can never affect another instruction.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import read_conn, resolve_db_path, write_transaction

BANK_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS bank_payments (
    instruction_number TEXT PRIMARY KEY,
    payee TEXT NOT NULL,
    vendor_invoice_number TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'succeeded', 'failed')),
    result_event_id TEXT,
    accepted_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    result_at TEXT
);

CREATE TABLE IF NOT EXISTS bank_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instruction_number TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_id TEXT,
    note TEXT,
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_bank_events_instruction
    ON bank_events(instruction_number, id);

-- A single-row fault-injection switch used by demos/tests to model "the bank
-- accepted the instruction but its response never reached the caller".
CREATE TABLE IF NOT EXISTS bank_simulation_flags (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    lose_next_submit_response INTEGER NOT NULL DEFAULT 0
        CHECK (lose_next_submit_response IN (0, 1))
);
INSERT OR IGNORE INTO bank_simulation_flags (id, lose_next_submit_response)
VALUES (1, 0);
"""

TERMINAL_STATUSES = ("succeeded", "failed")


class BankError(Exception):
    """Deterministic simulated-bank rejection."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 409,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class BankResponseLost(BankError):
    """The bank accepted the instruction but the response never arrived."""

    def __init__(self, instruction_number: str) -> None:
        super().__init__(
            "bank_response_lost",
            "The bank accepted the instruction but the response was lost.",
            status_code=503,
            details={"instruction_number": instruction_number},
        )


def resolve_bank_db_path(ledger_db_path: str | Path) -> str:
    """Derive a separate default bank database next to the ledger database.

    The simulated channel always persists on disk, independently of the
    campaign ledger, so it survives application restarts.
    """
    return f"{ledger_db_path}.bank.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def init_bank_db(db_path: str | Path | None = None) -> None:
    path = resolve_db_path(db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(BANK_SCHEMA)
    finally:
        conn.close()


class SimulatedBank:
    """An idempotent, persistent, local stand-in for a payment provider."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        init_bank_db(self.db_path)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def get(self, instruction_number: str) -> dict[str, Any] | None:
        with read_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM bank_payments WHERE instruction_number = ?",
                (instruction_number,),
            ).fetchone()
            return dict(row) if row is not None else None

    def list_events(self, instruction_number: str) -> list[dict[str, Any]]:
        with read_conn(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM bank_events
                WHERE instruction_number = ?
                ORDER BY id ASC
                """,
                (instruction_number,),
            ).fetchall()
            return [dict(r) for r in rows]

    def set_lose_next_submit_response(self, enabled: bool) -> None:
        """Arm/disarm the 'accepted but response lost' simulation."""
        with write_transaction(self.db_path) as conn:
            conn.execute(
                """
                UPDATE bank_simulation_flags
                SET lose_next_submit_response = ?
                WHERE id = 1
                """,
                (1 if enabled else 0,),
            )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    def submit(
        self,
        *,
        instruction_number: str,
        payee: str,
        vendor_invoice_number: str,
        amount_cents: int,
    ) -> dict[str, Any]:
        """Accept a payment instruction exactly once.

        The instruction number is the provider-side idempotency key.  Repeating
        a submission with the same details returns the stored payment and never
        creates a second one.  Reusing a number with different details is a
        hard conflict.
        """
        with write_transaction(self.db_path) as conn:
            existing = conn.execute(
                "SELECT * FROM bank_payments WHERE instruction_number = ?",
                (instruction_number,),
            ).fetchone()
            if existing is not None:
                same = (
                    existing["payee"] == payee
                    and existing["vendor_invoice_number"]
                    == vendor_invoice_number
                    and int(existing["amount_cents"]) == int(amount_cents)
                )
                if not same:
                    raise BankError(
                        "bank_instruction_conflict",
                        "The instruction number was already used with "
                        "different payment details.",
                        status_code=409,
                        details={"instruction_number": instruction_number},
                    )
                conn.execute(
                    """
                    INSERT INTO bank_events (
                        instruction_number, event_type, note
                    ) VALUES (?, 'duplicate_submission',
                              'same instruction re-delivered')
                    """,
                    (instruction_number,),
                )
                return {"payment": dict(existing), "duplicate": True}

            conn.execute(
                """
                INSERT INTO bank_payments (
                    instruction_number, payee, vendor_invoice_number,
                    amount_cents, status
                ) VALUES (?, ?, ?, ?, 'pending')
                """,
                (
                    instruction_number,
                    payee,
                    vendor_invoice_number,
                    int(amount_cents),
                ),
            )
            conn.execute(
                """
                INSERT INTO bank_events (
                    instruction_number, event_type, note
                ) VALUES (?, 'accepted', 'instruction accepted')
                """,
                (instruction_number,),
            )
            row = conn.execute(
                "SELECT * FROM bank_payments WHERE instruction_number = ?",
                (instruction_number,),
            ).fetchone()
            flag = conn.execute(
                """
                SELECT lose_next_submit_response
                FROM bank_simulation_flags WHERE id = 1
                """
            ).fetchone()
            lose_response = bool(
                flag is not None and flag["lose_next_submit_response"]
            )
            if lose_response:
                conn.execute(
                    """
                    UPDATE bank_simulation_flags
                    SET lose_next_submit_response = 0
                    WHERE id = 1
                    """
                )
            result = {"payment": dict(row), "duplicate": False}

        # The payment above is durably accepted.  The simulated "lost response"
        # only hides the acknowledgement from the caller; it must not undo the
        # accepted instruction.
        if lose_response:
            raise BankResponseLost(instruction_number)
        return result

    def confirm(
        self,
        instruction_number: str,
        *,
        outcome: str,
        result_event_id: str,
    ) -> dict[str, Any]:
        """Deliver an explicit terminal result (``succeeded``/``failed``).

        Duplicate delivery of the same outcome is a no-op.  A result that
        contradicts an already confirmed terminal status is rejected and the
        stored result is left untouched.
        """
        if outcome not in TERMINAL_STATUSES:
            raise BankError(
                "bank_invalid_outcome",
                "Outcome must be 'succeeded' or 'failed'.",
                status_code=422,
            )
        conflict_details: dict[str, Any] | None = None
        with write_transaction(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM bank_payments WHERE instruction_number = ?",
                (instruction_number,),
            ).fetchone()
            if row is None:
                raise BankError(
                    "bank_instruction_not_found",
                    "The bank has no such instruction number.",
                    status_code=404,
                    details={"instruction_number": instruction_number},
                )

            if row["status"] == outcome:
                conn.execute(
                    """
                    INSERT INTO bank_events (
                        instruction_number, event_type, event_id, note
                    ) VALUES (?, 'duplicate_result', ?,
                              'identical terminal result re-delivered')
                    """,
                    (instruction_number, result_event_id),
                )
                return {"payment": dict(row), "duplicate": True}

            if row["status"] != "pending":
                # Record the rejected delivery for audit, but keep the
                # confirmed terminal status untouched.
                conn.execute(
                    """
                    INSERT INTO bank_events (
                        instruction_number, event_type, event_id, note
                    ) VALUES (?, 'rejected_conflict', ?,
                              'contradicts confirmed terminal result')
                    """,
                    (instruction_number, result_event_id),
                )
                conflict_details = {
                    "instruction_number": instruction_number,
                    "confirmed_status": row["status"],
                    "rejected_status": outcome,
                }
                updated = dict(row)
            else:
                changed_at = _now()
                conn.execute(
                    """
                    UPDATE bank_payments
                    SET status = ?, result_event_id = ?, result_at = ?
                    WHERE instruction_number = ? AND status = 'pending'
                    """,
                    (outcome, result_event_id, changed_at, instruction_number),
                )
                conn.execute(
                    """
                    INSERT INTO bank_events (
                        instruction_number, event_type, event_id, note
                    ) VALUES (?, ?, ?, 'terminal result confirmed')
                    """,
                    (instruction_number, outcome, result_event_id),
                )
                updated = dict(
                    conn.execute(
                        "SELECT * FROM bank_payments "
                        "WHERE instruction_number = ?",
                        (instruction_number,),
                    ).fetchone()
                )

        if conflict_details is not None:
            # Raised after commit so the audit event survives the rejection.
            raise BankError(
                "bank_result_conflict",
                "A different terminal result was already confirmed and "
                "cannot be overwritten.",
                status_code=409,
                details=conflict_details,
            )
        return {"payment": updated, "duplicate": False}
