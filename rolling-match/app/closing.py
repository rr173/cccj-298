"""Periodic sign-off statements (closings) for a campaign.

A closing freezes a reconciliation statement at a fixed cutoff.  Drafts may be
signed or discarded; signing never recomputes balances, so the signed statement
is exactly the content finance reviewed.  Signed statements are immutable and
are the opening basis of the next period.

Money is still integer cents.  There are two deliberately separate views:

* the *book* balance (opening + actual grants - actual reversals - bank
  confirmed reimbursement shares);
* the *in-flight* holds at the cutoff (submitted payment instructions whose
  bank outcome is not yet an explicit terminal result at the cutoff), split
  into "bank not accepted" and "accepted by the bank, still pending".

Temporary provisional matches and reservations of not-yet-submitted invoices
never enter a closing.

Bank results are authoritative.  A payment confirmed by the bank before the
cutoff is expensed in that period even if the business ledger has not synced
it; a cross-period instruction is expensed only in the period of the bank's
first successful confirmation, and a later ledger sync never counts again.
The closing itself is read-only: it never submits, syncs, grants, reverses,
or changes a reimbursement status.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .bank import SimulatedBank, resolve_bank_db_path
from .db import read_conn, write_transaction
from .service import CommandResult, LedgerError, LedgerService, canonical_hash

DRAFT = "draft"
SIGNED = "signed"
DISCARDED = "discarded"

BANK_SUCCEEDED = "succeeded"
BANK_FAILED = "failed"

HOLD_ACCEPTED_PENDING = "bank_accepted_pending"
HOLD_NOT_ACCEPTED = "bank_not_accepted"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _at_or_before(value: str | None, cutoff: str | None) -> bool:
    if value is None:
        return False
    moment = _parse_time(value)
    limit = _parse_time(cutoff)
    if moment is None or limit is None:
        return False
    return moment <= limit


def _ledger_status_at(instruction: sqlite3.Row, cutoff: str) -> str:
    """Reconstruct the ledger's instruction status as of the cutoff.

    The stored ``status`` reflects the latest committed state, which may be
    later than the cutoff (e.g. a result synced after the cutoff).  The ledger
    stamps ``updated_at`` when it changes the row, so that is the moment the
    ledger itself became terminal -- unlike ``result_at``, which mirrors the
    *bank's* confirmation time.  An instruction is treated as still ``pending``
    at the cutoff when its ledger change happened after the cutoff.
    """
    status = str(instruction["status"])
    if status not in {BANK_SUCCEEDED, BANK_FAILED}:
        return "pending"
    changed_at = instruction["updated_at"] or instruction["result_at"]
    if _at_or_before(changed_at, cutoff):
        return status
    return "pending"


class ClosingService:
    def __init__(
        self,
        db_path: str,
        bank_db_path: str | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self.bank_db_path = (
            str(bank_db_path)
            if bank_db_path is not None
            else resolve_bank_db_path(self.db_path)
        )
        # Ensure the independent bank schema exists.  This only creates the
        # provider-side schema; it never submits, confirms, or changes a
        # payment.  If the bank record cannot be opened the draft builder will
        # surface an explicit ``bank_unavailable`` blocker rather than fail
        # construction.
        try:
            SimulatedBank(self.bank_db_path)
        except (sqlite3.Error, OSError):
            pass

    # ------------------------------------------------------------------
    # Idempotent replay helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _replay_closing(
        conn: sqlite3.Connection,
        campaign_id: str,
        operation: str,
        source_event_id: str,
        payload: dict[str, Any],
    ) -> CommandResult | None:
        request_hash = canonical_hash(payload)
        row = conn.execute(
            """
            SELECT request_hash, resource_type, resource_id
            FROM idempotency_records
            WHERE campaign_id = ? AND operation = ? AND source_event_id = ?
            """,
            (campaign_id, operation, source_event_id),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise LedgerError(
                "idempotency_parameter_mismatch",
                "This source_event_id was already used with different parameters.",
                status_code=409,
                details={
                    "operation": operation,
                    "source_event_id": source_event_id,
                },
            )
        if row["resource_type"] != "closing":
            raise LedgerError(
                "idempotency_parameter_mismatch",
                "This source_event_id belongs to a different resource type.",
                status_code=409,
            )
        closing = conn.execute(
            "SELECT * FROM closings WHERE id = ?", (int(row["resource_id"]),)
        ).fetchone()
        if closing is None:
            raise LedgerError(
                "closing_not_found",
                "The replayed closing no longer exists.",
                status_code=404,
            )
        # Return the current metadata (a retry still points to the exact same
        # draft/statement) while its frozen content stays untouched.
        return CommandResult(200, ClosingService._row_view(closing), True)

    # ------------------------------------------------------------------
    # Command: create a draft at a fixed cutoff
    # ------------------------------------------------------------------
    def create_draft(
        self,
        campaign_id: str,
        *,
        source_event_id: str,
        cutoff_at: str | None = None,
    ) -> CommandResult:
        payload = {
            "operation": "closing_draft",
            "campaign_id": campaign_id,
            "source_event_id": source_event_id,
            "cutoff_at": cutoff_at,
        }
        with write_transaction(self.db_path) as conn:
            replay = self._replay_closing(
                conn,
                campaign_id,
                "closing_draft",
                source_event_id,
                payload,
            )
            if replay is not None:
                return replay

            LedgerService._get_campaign(conn, campaign_id)

            existing_draft = conn.execute(
                """
                SELECT * FROM closings
                WHERE campaign_id = ? AND status = 'draft'
                """,
                (campaign_id,),
            ).fetchone()
            if existing_draft is not None:
                raise LedgerError(
                    "closing_draft_exists",
                    "This campaign already has one pending closing draft. "
                    "Sign or discard it before taking a new cutoff.",
                    status_code=409,
                    details={
                        "closing_id": int(existing_draft["id"]),
                        "cutoff_at": existing_draft["cutoff_at"],
                    },
                )

            db_now = conn.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now') AS now_value"
            ).fetchone()["now_value"]
            if cutoff_at is None:
                cutoff = str(db_now)
            else:
                try:
                    parsed = _parse_time(cutoff_at)
                except ValueError as exc:
                    raise LedgerError(
                        "closing_invalid_cutoff",
                        "cutoff_at must be an ISO-8601 timestamp.",
                        status_code=422,
                    ) from exc
                if parsed is None:
                    raise LedgerError(
                        "closing_invalid_cutoff",
                        "cutoff_at must be an ISO-8601 timestamp.",
                        status_code=422,
                    )
                cutoff = parsed.astimezone(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                )
                if _parse_time(cutoff) > _parse_time(str(db_now)):
                    raise LedgerError(
                        "closing_cutoff_in_future",
                        "A closing cutoff cannot be later than now.",
                        status_code=422,
                        details={"cutoff_at": cutoff, "now": db_now},
                    )

            prior_signed = conn.execute(
                """
                SELECT * FROM closings
                WHERE campaign_id = ? AND status = 'signed'
                ORDER BY period_no DESC, id DESC
                LIMIT 1
                """,
                (campaign_id,),
            ).fetchone()
            if prior_signed is not None and not (
                _parse_time(cutoff) > _parse_time(str(prior_signed["cutoff_at"]))
            ):
                raise LedgerError(
                    "closing_cutoff_not_after_previous",
                    "The new cutoff must be strictly later than the last "
                    "signed statement's cutoff.",
                    status_code=409,
                    details={
                        "previous_closing_id": int(prior_signed["id"]),
                        "previous_cutoff_at": prior_signed["cutoff_at"],
                        "requested_cutoff_at": cutoff,
                    },
                )

            signed_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM closings
                    WHERE campaign_id = ? AND status = 'signed'
                    """,
                    (campaign_id,),
                ).fetchone()["n"]
            )
            period_no = signed_count + 1

            statement, blockers, signable, content_hash = self._build_statement(
                conn,
                campaign_id,
                cutoff=cutoff,
                period_no=period_no,
                prior_signed=prior_signed,
            )

            cursor = conn.execute(
                """
                INSERT INTO closings (
                    campaign_id, period_no, status, source_event_id,
                    cutoff_at, content_json, content_hash, signable,
                    blockers_json
                ) VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign_id,
                    period_no,
                    source_event_id,
                    cutoff,
                    json.dumps(
                        statement, ensure_ascii=False, sort_keys=True
                    ),
                    content_hash,
                    1 if signable else 0,
                    json.dumps(blockers, ensure_ascii=False, sort_keys=True),
                ),
            )
            closing_id = int(cursor.lastrowid)
            row = conn.execute(
                "SELECT * FROM closings WHERE id = ?", (closing_id,)
            ).fetchone()
            body = self._row_view(row)

            LedgerService._save_idempotency(
                conn,
                campaign_id=campaign_id,
                operation="closing_draft",
                source_event_id=source_event_id,
                payload=payload,
                status_code=201,
                body=body,
                resource_type="closing",
                resource_id=closing_id,
            )
            return CommandResult(201, body, False)

    # ------------------------------------------------------------------
    # Command: sign the exact reviewed draft
    # ------------------------------------------------------------------
    def sign_closing(
        self,
        campaign_id: str,
        closing_id: int,
        *,
        source_event_id: str,
    ) -> CommandResult:
        payload = {
            "operation": "closing_sign",
            "campaign_id": campaign_id,
            "closing_id": closing_id,
            "source_event_id": source_event_id,
        }
        with write_transaction(self.db_path) as conn:
            replay = self._replay_closing(
                conn,
                campaign_id,
                "closing_sign",
                source_event_id,
                payload,
            )
            if replay is not None:
                return replay

            LedgerService._get_campaign(conn, campaign_id)
            row = self._get_closing(conn, campaign_id, closing_id)

            if row["status"] == SIGNED:
                raise LedgerError(
                    "closing_already_signed",
                    "This closing draft has already been signed.",
                    status_code=409,
                    details={"closing_id": closing_id},
                )
            if row["status"] == DISCARDED:
                raise LedgerError(
                    "closing_discarded",
                    "This closing draft was discarded and can no longer be "
                    "signed.",
                    status_code=409,
                    details={"closing_id": closing_id},
                )
            if not bool(row["signable"]):
                raise LedgerError(
                    "closing_not_signable",
                    "The draft cannot be signed because reconciliation found "
                    "issues. Inspect blockers to locate each cause.",
                    status_code=409,
                    details={
                        "closing_id": closing_id,
                        "blockers": json.loads(row["blockers_json"]),
                    },
                )

            statement = json.loads(row["content_json"])
            if canonical_hash(statement) != row["content_hash"]:
                raise LedgerError(
                    "closing_content_corrupted",
                    "The frozen statement no longer matches its content hash; "
                    "it cannot be signed.",
                    status_code=409,
                    details={"closing_id": closing_id},
                )

            signed_at = _now()
            conn.execute(
                """
                UPDATE closings
                SET status = 'signed', signed_at = ?
                WHERE id = ? AND status = 'draft'
                """,
                (signed_at, closing_id),
            )
            signed_row = self._get_closing(conn, campaign_id, closing_id)
            body = self._row_view(signed_row)
            LedgerService._save_idempotency(
                conn,
                campaign_id=campaign_id,
                operation="closing_sign",
                source_event_id=source_event_id,
                payload=payload,
                status_code=200,
                body=body,
                resource_type="closing",
                resource_id=closing_id,
            )
            return CommandResult(200, body, False)

    # ------------------------------------------------------------------
    # Command: discard a draft
    # ------------------------------------------------------------------
    def discard_closing(
        self,
        campaign_id: str,
        closing_id: int,
        *,
        source_event_id: str,
    ) -> CommandResult:
        payload = {
            "operation": "closing_discard",
            "campaign_id": campaign_id,
            "closing_id": closing_id,
            "source_event_id": source_event_id,
        }
        with write_transaction(self.db_path) as conn:
            replay = self._replay_closing(
                conn,
                campaign_id,
                "closing_discard",
                source_event_id,
                payload,
            )
            if replay is not None:
                return replay

            LedgerService._get_campaign(conn, campaign_id)
            row = self._get_closing(conn, campaign_id, closing_id)
            if row["status"] == SIGNED:
                raise LedgerError(
                    "closing_already_signed",
                    "A signed statement cannot be discarded.",
                    status_code=409,
                    details={"closing_id": closing_id},
                )
            if row["status"] == DISCARDED:
                raise LedgerError(
                    "closing_already_discarded",
                    "This closing draft was already discarded.",
                    status_code=409,
                    details={"closing_id": closing_id},
                )

            discarded_at = _now()
            conn.execute(
                """
                UPDATE closings
                SET status = 'discarded', discarded_at = ?
                WHERE id = ? AND status = 'draft'
                """,
                (discarded_at, closing_id),
            )
            discarded_row = self._get_closing(
                conn, campaign_id, closing_id
            )
            body = self._row_view(discarded_row)
            LedgerService._save_idempotency(
                conn,
                campaign_id=campaign_id,
                operation="closing_discard",
                source_event_id=source_event_id,
                payload=payload,
                status_code=200,
                body=body,
                resource_type="closing",
                resource_id=closing_id,
            )
            return CommandResult(200, body, False)

    # ------------------------------------------------------------------
    # Read models
    # ------------------------------------------------------------------
    @staticmethod
    def _get_closing(
        conn: sqlite3.Connection, campaign_id: str, closing_id: int
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT * FROM closings
            WHERE campaign_id = ? AND id = ?
            """,
            (campaign_id, closing_id),
        ).fetchone()
        if row is None:
            raise LedgerError(
                "closing_not_found",
                f"Closing {closing_id} does not exist in campaign "
                f"{campaign_id!r}.",
                status_code=404,
            )
        return row

    @staticmethod
    def _row_view(row: sqlite3.Row) -> dict[str, Any]:
        content = json.loads(row["content_json"])
        view: dict[str, Any] = {
            "id": int(row["id"]),
            "campaign_id": row["campaign_id"],
            "period_no": int(row["period_no"]),
            "status": row["status"],
            "source_event_id": row["source_event_id"],
            "cutoff_at": row["cutoff_at"],
            "created_at": row["created_at"],
            "signed_at": row["signed_at"],
            "discarded_at": row["discarded_at"],
            "signable": bool(row["signable"]),
            "blockers": json.loads(row["blockers_json"]),
            "content_hash": row["content_hash"],
        }
        view.update(content)
        # The stored columns are authoritative for signability; keep the
        # statement's own reconciliation block aligned with them.
        view["reconciliation"] = {
            **content.get("reconciliation", {}),
            "signable": bool(row["signable"]),
            "blockers": json.loads(row["blockers_json"]),
        }
        return view

    def get_closing(
        self, campaign_id: str, closing_id: int
    ) -> dict[str, Any]:
        with read_conn(self.db_path) as conn:
            LedgerService._get_campaign(conn, campaign_id)
            return self._row_view(self._get_closing(conn, campaign_id, closing_id))

    def list_closings(self, campaign_id: str) -> dict[str, Any]:
        with read_conn(self.db_path) as conn:
            LedgerService._get_campaign(conn, campaign_id)
            rows = conn.execute(
                """
                SELECT * FROM closings
                WHERE campaign_id = ?
                ORDER BY period_no ASC, id ASC
                """,
                (campaign_id,),
            ).fetchall()
            views = [self._row_view(row) for row in rows]
            latest_signed = next(
                (
                    v
                    for v in reversed(views)
                    if v["status"] == SIGNED
                ),
                None,
            )
            return {
                "campaign_id": campaign_id,
                "closing_count": len(views),
                "signed_count": sum(
                    1 for v in views if v["status"] == SIGNED
                ),
                "draft_count": sum(
                    1 for v in views if v["status"] == DRAFT
                ),
                "latest_signed_closing_id": (
                    latest_signed["id"] if latest_signed else None
                ),
                "closings": views,
            }

    # ------------------------------------------------------------------
    # Statement construction
    # ------------------------------------------------------------------
    def _build_statement(
        self,
        conn: sqlite3.Connection,
        campaign_id: str,
        *,
        cutoff: str,
        period_no: int,
        prior_signed: sqlite3.Row | None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], bool, str]:
        """Compute the frozen statement content and blockers.

        Everything is read inside the caller's ``BEGIN IMMEDIATE`` transaction,
        so all business-ledger events before the cutoff form one complete,
        non-interleaved snapshot.  The bank is an independent database, filtered
        strictly by its own result/accepted timestamps against the cutoff.
        """
        prior_content: dict[str, Any] | None = (
            json.loads(prior_signed["content_json"])
            if prior_signed is not None
            else None
        )
        prior_snapshot = (
            prior_content.get("cutoff", {}).get("snapshot")
            if prior_content is not None
            else None
        )
        prior_cutoff = (
            str(prior_signed["cutoff_at"])
            if prior_signed is not None
            else None
        )
        prior_period_no = (
            int(prior_signed["period_no"]) if prior_signed else None
        )
        prior_closing_id = int(prior_signed["id"]) if prior_signed else None

        prior_grant_max = int(
            (prior_snapshot or {}).get("max_grant_item_id", 0)
        )
        prior_reversal_max = int(
            (prior_snapshot or {}).get("max_reversal_id", 0)
        )
        prior_instruction_max = int(
            (prior_snapshot or {}).get("max_payment_instruction_id", 0)
        )

        # Timestamps are compared in Python, not by SQL text ordering: the
        # ledger writes ISO-8601 with microseconds while SQLite defaults use
        # milliseconds, and a lexicographic comparison can misorder events in
        # the same second.  This also keeps the cutoff boundary and the
        # period-id boundary exactly consistent.
        def _all_at_or_before(rows, column, limit):
            return [row for row in rows if _at_or_before(row[column], limit)]

        grant_candidates = conn.execute(
            """
            SELECT id, created_at FROM grant_items
            WHERE campaign_id = ? AND id > ?
            ORDER BY id ASC
            """,
            (campaign_id, prior_grant_max),
        ).fetchall()
        grant_period_rows = _all_at_or_before(
            grant_candidates, "created_at", cutoff
        )
        grant_max = (
            int(grant_period_rows[-1]["id"])
            if grant_period_rows
            else prior_grant_max
        )

        reversal_candidates = conn.execute(
            """
            SELECT id, created_at FROM reversals
            WHERE campaign_id = ? AND id > ?
            ORDER BY id ASC
            """,
            (campaign_id, prior_reversal_max),
        ).fetchall()
        reversal_period_rows = _all_at_or_before(
            reversal_candidates, "created_at", cutoff
        )
        reversal_max = (
            int(reversal_period_rows[-1]["id"])
            if reversal_period_rows
            else prior_reversal_max
        )

        instruction_candidates = conn.execute(
            """
            SELECT id, created_at FROM payment_instructions
            WHERE campaign_id = ? AND id > ?
            ORDER BY id ASC
            """,
            (campaign_id, prior_instruction_max),
        ).fetchall()
        instruction_period_rows = _all_at_or_before(
            instruction_candidates, "created_at", cutoff
        )
        instruction_max = (
            int(instruction_period_rows[-1]["id"])
            if instruction_period_rows
            else prior_instruction_max
        )

        blockers: list[dict[str, Any]] = []
        reconciliation = {
            "bank_query": "ok",
            "cutoff_at": cutoff,
            "instruction_count_checked": 0,
            "instruction_checks": [],
        }

        # ---- Bank snapshot (independent database, filtered by timestamp) ----
        bank_rows: dict[str, dict[str, Any]] = {}
        bank_query_ok = True
        bank_error: str | None = None
        bank_rows, bank_query_ok, bank_error = self._read_bank_rows()
        if bank_query_ok:
            # The bank is a separate database, so its rows are not covered by
            # the ledger's write lock.  Re-read until two consecutive reads
            # agree; this closes the window where a result committed just
            # after our first read but was already timestamped at/before the
            # cutoff.  The statement is then built from one coherent snapshot.
            signature = self._bank_signature(bank_rows)
            for _ in range(3):
                fresh, ok, err = self._read_bank_rows()
                if not ok:
                    bank_rows, bank_query_ok, bank_error = {}, False, err
                    break
                fresh_signature = self._bank_signature(fresh)
                bank_rows = fresh
                if fresh_signature == signature:
                    break
                signature = fresh_signature
        if not bank_query_ok:
            reconciliation["bank_query"] = "unavailable"
            blockers.append(
                {
                    "code": "bank_unavailable",
                    "message": (
                        "The bank record could not be read, so the cutoff "
                        "cannot be reconciled or signed."
                    ),
                    "details": {"error": bank_error},
                }
            )

        # ---- Projects and per-project opening balances ----
        project_rows = conn.execute(
            """
            SELECT id FROM projects
            WHERE campaign_id = ?
            ORDER BY id ASC
            """,
            (campaign_id,),
        ).fetchall()
        project_ids = [row["id"] for row in project_rows]
        prior_projects = {
            p["project_id"]: int(p["closing_book_balance_cents"])
            for p in (prior_content or {}).get("projects", [])
        }

        projects: dict[str, dict[str, Any]] = {}
        for project_id in project_ids:
            projects[project_id] = {
                "project_id": project_id,
                "opening_book_balance_cents": prior_projects.get(
                    project_id, 0
                ),
                "period_grants_cents": 0,
                "period_reversals_cents": 0,
                "bank_confirmed_reimbursement_cents": 0,
                "closing_book_balance_cents": 0,
                "outstanding_in_flight_cents": 0,
                "available_after_in_flight_cents": 0,
                "gap_after_in_flight_cents": 0,
                "basis": {
                    "grants": [],
                    "reversals": [],
                    "bank_confirmed_reimbursements": [],
                    "outstanding_in_flight": [],
                },
            }

        # ---- Period actual grants (business ledger, id boundary) ----
        grant_rows = conn.execute(
            """
            SELECT
                gi.id,
                gi.settlement_id,
                gi.project_id,
                gi.donation_id,
                gi.amount_cents,
                gi.created_at,
                d.source_event_id AS donation_source_event_id,
                d.donor_id,
                s.source_event_id AS settlement_source_event_id
            FROM grant_items gi
            JOIN donations d ON d.id = gi.donation_id
            LEFT JOIN settlements s ON s.id = gi.settlement_id
            WHERE gi.campaign_id = ?
              AND gi.id > ?
              AND gi.id <= ?
            ORDER BY gi.id ASC
            """,
            (campaign_id, prior_grant_max, grant_max),
        ).fetchall()
        for row in grant_rows:
            project = projects.get(row["project_id"])
            if project is None:
                continue
            amount = int(row["amount_cents"])
            project["period_grants_cents"] += amount
            project["basis"]["grants"].append(
                {
                    "grant_item_id": int(row["id"]),
                    "settlement_id": int(row["settlement_id"]),
                    "settlement_source_event_id": row[
                        "settlement_source_event_id"
                    ],
                    "donation_source_event_id": row[
                        "donation_source_event_id"
                    ],
                    "donor_id": row["donor_id"],
                    "amount_cents": amount,
                    "created_at": row["created_at"],
                }
            )

        # ---- Period actual reversals ----
        reversal_rows = conn.execute(
            """
            SELECT
                rv.id,
                rv.amount_cents,
                rv.created_at,
                rv.refund_id,
                d.project_id,
                d.source_event_id AS donation_source_event_id,
                rf.source_event_id AS refund_source_event_id
            FROM reversals rv
            JOIN donations d ON d.id = rv.donation_id
            JOIN refunds rf ON rf.id = rv.refund_id
            WHERE rv.campaign_id = ?
              AND rv.id > ?
              AND rv.id <= ?
            ORDER BY rv.id ASC
            """,
            (campaign_id, prior_reversal_max, reversal_max),
        ).fetchall()
        for row in reversal_rows:
            project = projects.get(row["project_id"])
            if project is None:
                continue
            amount = int(row["amount_cents"])
            project["period_reversals_cents"] += amount
            project["basis"]["reversals"].append(
                {
                    "reversal_id": int(row["id"]),
                    "refund_id": int(row["refund_id"]),
                    "refund_source_event_id": row[
                        "refund_source_event_id"
                    ],
                    "donation_source_event_id": row[
                        "donation_source_event_id"
                    ],
                    "amount_cents": amount,
                    "created_at": row["created_at"],
                }
            )

        # ---- Instruction snapshot ----
        instructions = conn.execute(
            """
            SELECT * FROM payment_instructions
            WHERE campaign_id = ? AND id <= ?
            ORDER BY id ASC
            """,
            (campaign_id, instruction_max),
        ).fetchall()
        invoice_shares: dict[int, list[dict[str, Any]]] = {}
        share_rows = conn.execute(
            """
            SELECT invoice_id, project_id, share_cents
            FROM invoice_shares
            WHERE campaign_id = ?
            ORDER BY invoice_id ASC, project_id ASC
            """,
            (campaign_id,),
        ).fetchall()
        for row in share_rows:
            invoice_shares.setdefault(int(row["invoice_id"]), []).append(
                {
                    "project_id": row["project_id"],
                    "share_cents": int(row["share_cents"]),
                }
            )

        # Instructions already counted in any signed statement must never be
        # counted again, even if the business ledger syncs them later.
        counted_instructions: dict[str, int] = {}
        prior_in_flight_refs: dict[str, list[dict[str, Any]]] = {}
        signed_rows = conn.execute(
            """
            SELECT * FROM closings
            WHERE campaign_id = ? AND status = 'signed'
            ORDER BY period_no ASC, id ASC
            """,
            (campaign_id,),
        ).fetchall()
        for signed_row in signed_rows:
            signed_content = json.loads(signed_row["content_json"])
            for payment in signed_content.get("payments", {}).get(
                "bank_confirmed_in_period", []
            ):
                counted_instructions[payment["instruction_number"]] = int(
                    signed_row["period_no"]
                )
            for hold in signed_content.get("payments", {}).get(
                "outstanding_in_flight", []
            ):
                prior_in_flight_refs.setdefault(
                    hold["instruction_number"], []
                ).append(
                    {
                        "closing_id": int(signed_row["id"]),
                        "period_no": int(signed_row["period_no"]),
                        "closing_cutoff_at": signed_row["cutoff_at"],
                    }
                )

        bank_confirmed_in_period: list[dict[str, Any]] = []
        bank_explicit_failures: list[dict[str, Any]] = []
        outstanding_in_flight: list[dict[str, Any]] = []
        instruction_checks: list[dict[str, Any]] = []

        for instruction in instructions:
            number = instruction["instruction_number"]
            # Two views of the same instruction:
            # * ``ledger_status`` is the ledger's own state *at the cutoff*; a
            #   result synced after the cutoff must not retcon the period, so
            #   it still counts as a hold there.
            # * ``current_status`` is the ledger's latest committed state and
            #   is what integrity reconciliation compares against the bank:
            #   the two systems genuinely disagree if either says terminal and
            #   the other does not, regardless of when that happened.
            ledger_status = _ledger_status_at(instruction, cutoff)
            current_status = str(instruction["status"])
            invoice_id = int(instruction["invoice_id"])
            shares = invoice_shares.get(invoice_id, [])
            bank_row = bank_rows.get(number)
            bank_status = bank_row["status"] if bank_row else "absent"

            check: dict[str, Any] = {
                "instruction_number": number,
                "invoice_id": invoice_id,
                "vendor_invoice_number": instruction[
                    "vendor_invoice_number"
                ],
                "payee": instruction["payee"],
                "total_amount_cents": int(instruction["amount_cents"]),
                "ledger_status_at_cutoff": ledger_status,
                "ledger_status_current": current_status,
                "bank_record": "found" if bank_row else "absent",
                "bank_status": bank_status,
                "bank_accepted_at": (
                    bank_row.get("accepted_at") if bank_row else None
                ),
                "bank_result_at": (
                    bank_row.get("result_at") if bank_row else None
                ),
                "matches_bank_fields": None,
                "issues": [],
            }

            # ---- Reconciliation of the instruction against the bank record ----
            if bank_query_ok and bank_row is not None:
                mismatched: dict[str, Any] = {}
                if instruction["payee"] != bank_row["payee"]:
                    mismatched["payee"] = {
                        "ledger": instruction["payee"],
                        "bank": bank_row["payee"],
                    }
                if (
                    instruction["vendor_invoice_number"]
                    != bank_row["vendor_invoice_number"]
                ):
                    mismatched["vendor_invoice_number"] = {
                        "ledger": instruction["vendor_invoice_number"],
                        "bank": bank_row["vendor_invoice_number"],
                    }
                if int(instruction["amount_cents"]) != int(
                    bank_row["amount_cents"]
                ):
                    mismatched["amount_cents"] = {
                        "ledger": int(instruction["amount_cents"]),
                        "bank": int(bank_row["amount_cents"]),
                    }
                check["matches_bank_fields"] = not mismatched
                if mismatched:
                    issue = {
                        "code": "bank_instruction_mismatch",
                        "message": (
                            "The instruction's payee, invoice number, or "
                            "total amount does not match the bank's accepted "
                            "record."
                        ),
                        "details": {
                            "instruction_number": number,
                            "mismatched_fields": mismatched,
                        },
                    }
                    check["issues"].append(issue["code"])
                    blockers.append(issue)
                if (
                    bank_row["status"] in {BANK_SUCCEEDED, BANK_FAILED}
                    and bank_row.get("result_at") is None
                ):
                    issue = {
                        "code": "bank_result_missing_timestamp",
                        "message": (
                            "The bank shows a terminal result without a "
                            "confirmation timestamp, so the period cannot be "
                            "determined."
                        ),
                        "details": {"instruction_number": number},
                    }
                    check["issues"].append(issue["code"])
                    blockers.append(issue)
            elif bank_query_ok and bank_row is None:
                check["matches_bank_fields"] = None

            if bank_query_ok and current_status in {
                BANK_SUCCEEDED,
                BANK_FAILED,
            }:
                if bank_row is None:
                    issue = {
                        "code": "ledger_terminal_without_bank_terminal",
                        "message": (
                            "The business ledger marks the instruction "
                            "terminal but the bank has no record and therefore "
                            "no terminal result."
                        ),
                        "details": {
                            "instruction_number": number,
                            "ledger_status": current_status,
                            "bank_status": "absent",
                        },
                    }
                    check["issues"].append(issue["code"])
                    blockers.append(issue)
                elif bank_row["status"] not in {
                    BANK_SUCCEEDED,
                    BANK_FAILED,
                }:
                    issue = {
                        "code": "ledger_terminal_without_bank_terminal",
                        "message": (
                            "The business ledger marks the instruction "
                            "terminal but the bank still shows it as pending."
                        ),
                        "details": {
                            "instruction_number": number,
                            "ledger_status": current_status,
                            "bank_status": bank_row["status"],
                        },
                    }
                    check["issues"].append(issue["code"])
                    blockers.append(issue)
                elif bank_row["status"] != current_status:
                    issue = {
                        "code": "bank_terminal_conflict",
                        "message": (
                            "The ledger and bank terminal results conflict for "
                            "the same instruction."
                        ),
                        "details": {
                            "instruction_number": number,
                            "ledger_status": current_status,
                            "bank_status": bank_row["status"],
                        },
                    }
                    check["issues"].append(issue["code"])
                    blockers.append(issue)

            if not bank_query_ok:
                # Without a readable bank snapshot no instruction can be
                # confirmed or held; the draft is created as non-signable.
                instruction_checks.append(check)
                continue

            instruction_checks.append(check)
            if bank_row is None:
                # A pending instruction the reachable bank has not accepted yet
                # is normal in transit, not a failure.
                if ledger_status not in {BANK_SUCCEEDED, BANK_FAILED}:
                    outstanding_in_flight.append(
                        self._hold_entry(
                            instruction,
                            shares,
                            state=HOLD_NOT_ACCEPTED,
                            bank_row=None,
                            prior_in_flight_refs=prior_in_flight_refs,
                            cutoff=cutoff,
                            ledger_status=ledger_status,
                        )
                    )
                continue

            result_at = bank_row.get("result_at")
            accepted_at = bank_row.get("accepted_at")
            bank_terminal_at_cutoff = bank_row["status"] in {
                BANK_SUCCEEDED,
                BANK_FAILED,
            } and _at_or_before(result_at, cutoff)
            bank_accepted_at_cutoff = _at_or_before(accepted_at, cutoff)

            if bank_terminal_at_cutoff:
                if bank_row["status"] == BANK_SUCCEEDED:
                    if number not in counted_instructions:
                        # Bank-first: the period is the bank's first confirmation.
                        if prior_cutoff is None or not _at_or_before(
                            result_at, prior_cutoff
                        ):
                            payment = self._paid_entry(
                                instruction,
                                shares,
                                bank_row,
                                prior_in_flight_refs,
                                ledger_status,
                            )
                            bank_confirmed_in_period.append(payment)
                            for share in shares:
                                project = projects.get(share["project_id"])
                                if project is None:
                                    continue
                                project[
                                    "bank_confirmed_reimbursement_cents"
                                ] += share["share_cents"]
                                project["basis"][
                                    "bank_confirmed_reimbursements"
                                ].append(
                                    {
                                        **payment,
                                        "share_cents": share["share_cents"],
                                    }
                                )
                        else:
                            # A signed prior statement should have counted a
                            # success at or before its cutoff.  If it did not,
                            # the books cannot be signed again; surface it.
                            blockers.append(
                                {
                                    "code": "prior_period_bank_success_not_counted",
                                    "message": (
                                        "The bank confirmed success at or "
                                        "before the prior signed cutoff but "
                                        "that instruction is not in any signed "
                                        "statement."
                                    ),
                                    "details": {
                                        "instruction_number": number,
                                        "bank_result_at": result_at,
                                        "prior_cutoff_at": prior_cutoff,
                                    },
                                }
                            )
                    # Already counted in a signed statement: no expense and
                    # no hold in this period.
                    continue
                # Explicit failure ends the hold and creates no expense.
                if prior_cutoff is None or _at_or_before(result_at, cutoff):
                    if prior_cutoff is None or not _at_or_before(
                        result_at, prior_cutoff
                    ):
                        bank_explicit_failures.append(
                            {
                                "instruction_number": number,
                                "invoice_id": invoice_id,
                                "vendor_invoice_number": instruction[
                                    "vendor_invoice_number"
                                ],
                                "payee": instruction["payee"],
                                "total_amount_cents": int(
                                    instruction["amount_cents"]
                                ),
                                "bank_status": BANK_FAILED,
                                "bank_result_event_id": bank_row.get(
                                    "result_event_id"
                                ),
                                "bank_result_at": result_at,
                                "ledger_status_at_cutoff": ledger_status,
                                "shares": shares,
                                "released_hold": True,
                                "expense_cents": 0,
                            }
                        )
                continue

            # Not terminal at the cutoff.  At the cutoff the instruction was
            # accepted and still pending if the bank accepted it before the
            # cutoff; otherwise the bank had not accepted it yet.
            state = (
                HOLD_ACCEPTED_PENDING
                if bank_accepted_at_cutoff
                else HOLD_NOT_ACCEPTED
            )
            outstanding_in_flight.append(
                self._hold_entry(
                    instruction,
                    shares,
                    state=state,
                    bank_row=bank_row,
                    prior_in_flight_refs=prior_in_flight_refs,
                    cutoff=cutoff,
                    ledger_status=ledger_status,
                )
            )

        # ---- Attach holds to per-project basis and compute balances ----
        for hold in outstanding_in_flight:
            for share in hold["shares"]:
                project = projects.get(share["project_id"])
                if project is None:
                    continue
                project["outstanding_in_flight_cents"] += share[
                    "share_cents"
                ]
                project["basis"]["outstanding_in_flight"].append(
                    {
                        **hold,
                        "project_share_cents": share["share_cents"],
                    }
                )

        for project in projects.values():
            project["closing_book_balance_cents"] = (
                project["opening_book_balance_cents"]
                + project["period_grants_cents"]
                - project["period_reversals_cents"]
                - project["bank_confirmed_reimbursement_cents"]
            )
            project["available_after_in_flight_cents"] = max(
                0,
                project["closing_book_balance_cents"]
                - project["outstanding_in_flight_cents"],
            )
            project["gap_after_in_flight_cents"] = max(
                0,
                project["outstanding_in_flight_cents"]
                - project["closing_book_balance_cents"],
            )

        reconciliation["instruction_count_checked"] = len(
            instruction_checks
        )
        reconciliation["instruction_checks"] = instruction_checks

        totals = {
            "opening_book_balance_cents": sum(
                p["opening_book_balance_cents"] for p in projects.values()
            ),
            "period_grants_cents": sum(
                p["period_grants_cents"] for p in projects.values()
            ),
            "period_reversals_cents": sum(
                p["period_reversals_cents"] for p in projects.values()
            ),
            "bank_confirmed_reimbursement_cents": sum(
                p["bank_confirmed_reimbursement_cents"]
                for p in projects.values()
            ),
            "closing_book_balance_cents": sum(
                p["closing_book_balance_cents"] for p in projects.values()
            ),
            "outstanding_in_flight_cents": sum(
                p["outstanding_in_flight_cents"]
                for p in projects.values()
            ),
            "available_after_in_flight_cents": sum(
                p["available_after_in_flight_cents"]
                for p in projects.values()
            ),
            "gap_after_in_flight_cents": sum(
                p["gap_after_in_flight_cents"]
                for p in projects.values()
            ),
            "bank_confirmed_payment_count": len(bank_confirmed_in_period),
            "bank_explicit_failure_count": len(bank_explicit_failures),
            "outstanding_in_flight_instruction_count": len(
                outstanding_in_flight
            ),
        }

        content: dict[str, Any] = {
            "opening_basis_closing_id": prior_closing_id,
            "opening_basis_period_no": prior_period_no,
            "opening_basis_cutoff_at": prior_cutoff,
            "cutoff": {
                "cutoff_at": cutoff,
                "period_no": period_no,
                "basis": (
                    "business ledger granted/reversed events after the prior "
                    "signed snapshot up to this snapshot; bank-confirmed "
                    "successes by the bank's first confirmation time"
                ),
                "snapshot": {
                    "max_grant_item_id": grant_max,
                    "max_reversal_id": reversal_max,
                    "max_payment_instruction_id": instruction_max,
                },
                "prior_snapshot": prior_snapshot,
                "excludes": (
                    "provisional match and reservations of not-yet-submitted "
                    "invoices are excluded"
                ),
            },
            "projects": [projects[pid] for pid in sorted(projects)],
            "payments": {
                "bank_confirmed_in_period": bank_confirmed_in_period,
                "bank_explicit_failures": bank_explicit_failures,
                "outstanding_in_flight": outstanding_in_flight,
            },
            "totals": totals,
            "reconciliation": {
                **reconciliation,
                "signable": False,  # replaced by authoritative column below
                "blockers": blockers,
            },
        }
        signable = bank_query_ok and not blockers
        content["reconciliation"]["signable"] = signable
        # The hash fingerprints exactly the persisted statement content.
        content_hash = canonical_hash(content)
        return content, blockers, signable, content_hash

    # ------------------------------------------------------------------
    # Bank snapshot helpers
    # ------------------------------------------------------------------
    def _read_bank_rows(
        self,
    ) -> tuple[dict[str, dict[str, Any]], bool, str | None]:
        """Read every provider-side payment row into memory.

        Returns ``(rows_by_instruction, ok, error)``.  A missing or unreadable
        bank record is reported as ``ok=False``; the caller turns that into a
        non-signable ``bank_unavailable`` blocker rather than guessing.
        """
        rows: dict[str, dict[str, Any]] = {}
        try:
            with read_conn(self.bank_db_path) as bank_conn:
                for row in bank_conn.execute(
                    "SELECT * FROM bank_payments"
                ).fetchall():
                    rows[row["instruction_number"]] = dict(row)
        except (sqlite3.Error, OSError) as exc:
            return {}, False, str(exc)
        return rows, True, None

    @staticmethod
    def _bank_signature(rows: dict[str, dict[str, Any]]) -> tuple:
        """A comparable fingerprint of the provider snapshot.

        Two consecutive identical signatures mean no result was committed
        between the reads, so the statement is built from a coherent snapshot.
        """
        return tuple(
            sorted(
                (
                    number,
                    row["status"],
                    row.get("result_event_id"),
                    row.get("result_at"),
                    row.get("accepted_at"),
                )
                for number, row in rows.items()
            )
        )

    @staticmethod
    def _hold_entry(
        instruction: sqlite3.Row,
        shares: list[dict[str, Any]],
        *,
        state: str,
        bank_row: dict[str, Any] | None,
        prior_in_flight_refs: dict[str, list[dict[str, Any]]],
        cutoff: str,
        ledger_status: str,
    ) -> dict[str, Any]:
        number = instruction["instruction_number"]
        return {
            "instruction_number": number,
            "invoice_id": int(instruction["invoice_id"]),
            "vendor_invoice_number": instruction["vendor_invoice_number"],
            "payee": instruction["payee"],
            "total_amount_cents": int(instruction["amount_cents"]),
            "hold_state": state,
            "description": (
                "accepted by the bank, no explicit terminal result at the "
                "cutoff"
                if state == HOLD_ACCEPTED_PENDING
                else "submitted but the bank has not accepted it at the cutoff"
            ),
            "ledger_status_at_cutoff": ledger_status,
            "bank_record": "found" if bank_row else "absent",
            "bank_status": bank_row["status"] if bank_row else "absent",
            "bank_accepted_at": (
                bank_row.get("accepted_at") if bank_row else None
            ),
            "bank_result_at": (
                bank_row.get("result_at") if bank_row else None
            ),
            "shares": shares,
            "prior_in_flight": prior_in_flight_refs.get(number, []),
        }

    @staticmethod
    def _paid_entry(
        instruction: sqlite3.Row,
        shares: list[dict[str, Any]],
        bank_row: dict[str, Any],
        prior_in_flight_refs: dict[str, list[dict[str, Any]]],
        ledger_status: str,
    ) -> dict[str, Any]:
        number = instruction["instruction_number"]
        return {
            "instruction_number": number,
            "invoice_id": int(instruction["invoice_id"]),
            "vendor_invoice_number": instruction["vendor_invoice_number"],
            "payee": instruction["payee"],
            "total_amount_cents": int(instruction["amount_cents"]),
            "bank_status": BANK_SUCCEEDED,
            "bank_result_event_id": bank_row.get("result_event_id"),
            "bank_result_at": bank_row.get("result_at"),
            "ledger_status_at_cutoff": ledger_status,
            "ledger_synced_at_cutoff": ledger_status == BANK_SUCCEEDED,
            "expensed_by_bank_first": ledger_status != BANK_SUCCEEDED,
            "shares": shares,
            "prior_in_flight": prior_in_flight_refs.get(number, []),
        }
