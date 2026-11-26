"""Joint reimbursement of one vendor invoice across several projects.

Money is still integer cents.  A project's reimbursement capacity is its
*realized* matching money -- actual grants minus actual reversals -- never its
provisional match, and never another project's money.

The accounting model is deliberately derived, not stored as mutable project
balances:

* invoice shares are immutable and always cover the whole invoice;
* a payment instruction is either ``pending`` (in flight), ``succeeded``
  (paid), or ``failed``;
* per-project paid / in-flight / temporarily reserved amounts are recomputed
  from immutable shares plus instruction status.

Because a single instruction-status change moves *all* shares of an invoice at
once inside one ``BEGIN IMMEDIATE`` transaction, a concurrent refund, consent
change, submission, or result can never debit only some projects of an
invoice.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from .bank import (
    BankError,
    BankResponseLost,
    SimulatedBank,
    resolve_bank_db_path,
)
from .db import read_conn, write_transaction
from .service import CommandResult, LedgerError, LedgerService, canonical_hash

UNPAID = "unpaid"
IN_FLIGHT = "in_flight"
PAID = "paid"
TERMINAL_PAYMENT_STATUSES = ("succeeded", "failed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ReimbursementService:
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
        self.bank = SimulatedBank(self.bank_db_path)

    # ------------------------------------------------------------------
    # Lookups and derived accounting
    # ------------------------------------------------------------------
    @staticmethod
    def _get_invoice(
        conn: sqlite3.Connection, campaign_id: str, invoice_id: int
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM invoices WHERE campaign_id = ? AND id = ?",
            (campaign_id, invoice_id),
        ).fetchone()
        if row is None:
            raise LedgerError(
                "invoice_not_found",
                f"Invoice {invoice_id} does not exist in campaign "
                f"{campaign_id!r}.",
                status_code=404,
            )
        return row

    @staticmethod
    def _get_instruction(
        conn: sqlite3.Connection,
        campaign_id: str,
        instruction_number: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT * FROM payment_instructions
            WHERE campaign_id = ? AND instruction_number = ?
            """,
            (campaign_id, instruction_number),
        ).fetchone()
        if row is None:
            raise LedgerError(
                "payment_instruction_not_found",
                f"Payment instruction {instruction_number!r} was not found.",
                status_code=404,
            )
        return row

    @staticmethod
    def _shares(
        conn: sqlite3.Connection, invoice_id: int
    ) -> list[sqlite3.Row]:
        return list(
            conn.execute(
                """
                SELECT project_id, responsible_project_id, share_cents
                FROM invoice_shares
                WHERE invoice_id = ?
                ORDER BY project_id ASC
                """,
                (invoice_id,),
            ).fetchall()
        )

    @staticmethod
    def _instructions(
        conn: sqlite3.Connection, invoice_id: int
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT * FROM payment_instructions
            WHERE invoice_id = ?
            ORDER BY attempt_no ASC
            """,
            (invoice_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _project_funds(
        self, conn: sqlite3.Connection, campaign_id: str
    ) -> dict[str, dict[str, Any]]:
        """Realized money minus paid, in-flight, and temporary holds.

        Every component is attributed to the project *currently responsible*.
        Realized money therefore includes internal transfers received through
        a merger, while a merged-out project keeps no spendable balance.
        In-flight and paid invoice shares are aggregated from all original
        shareholders whose responsibility now points at the same project, so
        two shares of one invoice borne by the same project are judged
        together against one balance, never each against its own copy.
        """
        rows = conn.execute(
            """
            SELECT
                p.id AS project_id,
                p.status AS status,
                COALESCE(g.gross_granted_cents, 0) AS gross_granted_cents,
                COALESCE(rv.reversed_cents, 0) AS reversed_cents,
                COALESCE(tin.transfer_in_cents, 0) AS transfer_in_cents,
                COALESCE(tout.transfer_out_cents, 0) AS transfer_out_cents
            FROM projects p
            LEFT JOIN (
                SELECT responsible_project_id AS project_id,
                       SUM(amount_cents) AS gross_granted_cents
                FROM grant_items
                WHERE campaign_id = ?
                GROUP BY responsible_project_id
            ) g ON g.project_id = p.id
            LEFT JOIN (
                SELECT responsible_project_id AS project_id,
                       SUM(amount_cents) AS reversed_cents
                FROM reversals
                WHERE campaign_id = ?
                GROUP BY responsible_project_id
            ) rv ON rv.project_id = p.id
            LEFT JOIN (
                SELECT project_id, SUM(amount_cents) AS transfer_in_cents
                FROM internal_transfer_entries
                WHERE campaign_id = ? AND direction = 'in'
                GROUP BY project_id
            ) tin ON tin.project_id = p.id
            LEFT JOIN (
                SELECT project_id, SUM(amount_cents) AS transfer_out_cents
                FROM internal_transfer_entries
                WHERE campaign_id = ? AND direction = 'out'
                GROUP BY project_id
            ) tout ON tout.project_id = p.id
            WHERE p.campaign_id = ?
            ORDER BY p.id ASC
            """,
            (campaign_id, campaign_id, campaign_id, campaign_id, campaign_id),
        ).fetchall()

        funds: dict[str, dict[str, Any]] = {}
        for row in rows:
            gross = int(row["gross_granted_cents"])
            reversed_cents = int(row["reversed_cents"])
            transfer_in = int(row["transfer_in_cents"])
            transfer_out = int(row["transfer_out_cents"])
            realized = gross - reversed_cents + transfer_in - transfer_out
            funds[row["project_id"]] = {
                "project_id": row["project_id"],
                "status": row["status"],
                "is_active": row["status"] == "active",
                "gross_granted_cents": gross,
                "reversed_cents": reversed_cents,
                "internal_transfer_in_cents": transfer_in,
                "internal_transfer_out_cents": transfer_out,
                "realized_net_grant_cents": realized,
                "paid_cents": 0,
                "in_flight_cents": 0,
                "reserved_cents": 0,
                "available_cents": 0,
                "gap_cents": 0,
            }

        # Invoice shares are summed by the project currently responsible, so
        # several original shareholders merged into one project are judged with
        # a single combined balance.
        for status, key in (
            ("succeeded", "paid_cents"),
            ("pending", "in_flight_cents"),
        ):
            for row in conn.execute(
                """
                SELECT s.responsible_project_id AS project_id,
                       SUM(s.share_cents) AS amount_cents
                FROM invoice_shares s
                JOIN payment_instructions pi
                  ON pi.invoice_id = s.invoice_id
                WHERE s.campaign_id = ? AND pi.status = ?
                GROUP BY s.responsible_project_id
                """,
                (campaign_id, status),
            ).fetchall():
                if row["project_id"] in funds:
                    funds[row["project_id"]][key] += int(row["amount_cents"])

        # Bank-confirmed successful spend attributed by the project
        # responsible at the bank's confirmation moment.  This mirrors the
        # merger/closing bank-first rule: a merged project keeps the spend
        # confirmed before it was merged out, while a success confirmed
        # afterwards is borne by the successor.
        bank_rows: dict[str, dict[str, Any]] | None
        try:
            with read_conn(self.bank_db_path) as bank_conn:
                bank_rows = {
                    row["instruction_number"]: dict(row)
                    for row in bank_conn.execute(
                        "SELECT * FROM bank_payments"
                    ).fetchall()
                }
        except (sqlite3.Error, OSError):
            bank_rows = None
        if bank_rows is not None:
            instructions = conn.execute(
                """
                SELECT pi.instruction_number
                FROM payment_instructions pi
                WHERE pi.campaign_id = ?
                """,
                (campaign_id,),
            ).fetchall()
            book_spend = {project_id: 0 for project_id in funds}
            from .merger import MergerService

            for instruction in instructions:
                number = instruction["instruction_number"]
                bank_row = bank_rows.get(number)
                if bank_row is None or bank_row["status"] != "succeeded":
                    continue
                result_at = bank_row.get("result_at")
                for share in conn.execute(
                    """
                    SELECT s.project_id, s.share_cents
                    FROM invoice_shares s
                    JOIN payment_instructions pi
                      ON pi.invoice_id = s.invoice_id
                    WHERE pi.instruction_number = ?
                    """,
                    (number,),
                ).fetchall():
                    responsible = MergerService.responsible_at(
                        conn,
                        campaign_id,
                        share["project_id"],
                        result_at,
                    )
                    if responsible in book_spend:
                        book_spend[responsible] += int(share["share_cents"])
            for project_id, spend in book_spend.items():
                funds[project_id]["book_balance_spend_cents"] = spend

        for fund in funds.values():
            book_spend = fund.get("book_balance_spend_cents", fund["paid_cents"])
            # The book balance is realized money (including merger transfers)
            # net of every bank-confirmed expense attributed to the project at
            # the bank confirmation time.  For a merged-out project it is
            # always zero: the remaining book left through the internal
            # transfer, while its historical grant/reversal/spend lines stay
            # queryable separately.
            fund["book_balance_cents"] = (
                fund["realized_net_grant_cents"] - book_spend
            )
            # Spendable headroom deducts every confirmed expense (including a
            # merged project's pre-merger spend already moved through the
            # transfer) and current in-flight holds.  When they exceed
            # realized money the shortfall is reported as a gap; a gap is
            # never spendable and later grants fill it before new headroom.
            net_headroom = (
                fund["realized_net_grant_cents"]
                - book_spend
                - fund["in_flight_cents"]
            )
            fund["available_cents"] = max(0, net_headroom)
            fund["gap_cents"] = max(0, -net_headroom)
        return funds

    def _snapshot(
        self, conn: sqlite3.Connection, campaign_id: str
    ) -> dict[str, Any]:
        """Current invoice payability and per-project reimbursement capacity."""
        funds = self._project_funds(conn, campaign_id)

        invoices: list[dict[str, Any]] = []
        invoice_rows = conn.execute(
            "SELECT * FROM invoices WHERE campaign_id = ? ORDER BY id ASC",
            (campaign_id,),
        ).fetchall()

        for registration_order, invoice in enumerate(invoice_rows, start=1):
            invoice_id = int(invoice["id"])
            share_rows = self._shares(conn, invoice_id)
            consent_rows = conn.execute(
                """
                SELECT project_id, consented
                FROM invoice_consents
                WHERE invoice_id = ?
                """,
                (invoice_id,),
            ).fetchall()
            consents = {
                row["project_id"]: bool(row["consented"]) for row in consent_rows
            }
            shares = [
                {
                    "project_id": row["project_id"],
                    "responsible_project_id": row["responsible_project_id"],
                    "share_cents": int(row["share_cents"]),
                    "consented": consents.get(row["project_id"], False),
                }
                for row in share_rows
            ]
            instructions = self._instructions(conn, invoice_id)
            pending = next(
                (i for i in instructions if i["status"] == "pending"), None
            )
            succeeded = next(
                (i for i in instructions if i["status"] == "succeeded"), None
            )
            if succeeded is not None:
                state = PAID
            elif pending is not None:
                state = IN_FLIGHT
            else:
                state = UNPAID

            fully_consented = bool(shares) and all(
                share["consented"] for share in shares
            )
            if state == PAID:
                reason: str | None = "invoice_already_paid"
            elif state == IN_FLIGHT:
                reason = "payment_in_flight"
            elif not fully_consented:
                reason = "consent_pending"
            else:
                reason = None

            invoices.append(
                {
                    "id": invoice_id,
                    "campaign_id": invoice["campaign_id"],
                    "registration_order": registration_order,
                    "payee": invoice["payee"],
                    "vendor_invoice_number": invoice[
                        "vendor_invoice_number"
                    ],
                    "total_amount_cents": int(invoice["total_amount_cents"]),
                    "created_at": invoice["created_at"],
                    "shares": shares,
                    "consents": consents,
                    "fully_consented": fully_consented,
                    "state": state,
                    "payable": False,
                    "is_queued": False,
                    "not_payable_reason": reason,
                    "missing_consent_projects": [
                        share["responsible_project_id"]
                        for share in shares
                        if not share["consented"]
                    ],
                    "shortfall_cents_by_project": None,
                    "reserved_cents_by_project": None,
                    "payment_instructions": instructions,
                    "latest_instruction": (
                        instructions[-1] if instructions else None
                    ),
                    "pending_instruction": pending,
                    "succeeded_instruction": succeeded,
                }
            )

        # Temporary reservations are recomputed in invoice registration order.
        # A whole invoice is reserved only if every currently responsible
        # project can bear the *combined* total of its shares at that point
        # (several original shareholders may now be the same project after a
        # merger); an invoice that does not fit is skipped without blocking
        # later invoices.
        # Queue headroom uses the same book-spend basis as the funds view so
        # pre-merger historical spend and transferred balances are judged with
        # exactly the money a project can actually spend.
        queue_available = {
            project_id: fund["available_cents"] + fund["reserved_cents"]
            for project_id, fund in funds.items()
        }
        reserved: dict[str, int] = {project_id: 0 for project_id in funds}

        for invoice in invoices:
            if invoice["state"] != UNPAID:
                continue
            if not invoice["fully_consented"]:
                continue

            # Combine shares by the project currently responsible, so the same
            # balance is never counted once per original shareholder.
            combined: dict[str, int] = {}
            for share in invoice["shares"]:
                responsible = share["responsible_project_id"]
                combined[responsible] = (
                    combined.get(responsible, 0) + share["share_cents"]
                )

            shortfalls: dict[str, int] = {}
            for responsible, total_share in combined.items():
                available = queue_available.get(responsible, 0)
                if total_share > available:
                    shortfalls[responsible] = total_share - available

            if shortfalls:
                invoice["not_payable_reason"] = "insufficient_project_funds"
                invoice["shortfall_cents_by_project"] = shortfalls
                continue

            reserved_by_project: dict[str, int] = {}
            for responsible, total_share in combined.items():
                queue_available[responsible] = (
                    queue_available.get(responsible, 0) - total_share
                )
                reserved[responsible] = (
                    reserved.get(responsible, 0) + total_share
                )
                reserved_by_project[responsible] = total_share
            invoice["payable"] = True
            invoice["is_queued"] = True
            invoice["not_payable_reason"] = None
            invoice["reserved_cents_by_project"] = reserved_by_project

        for project_id, fund in funds.items():
            fund["reserved_cents"] = reserved.get(project_id, 0)
            book_spend = fund.get(
                "book_balance_spend_cents", fund["paid_cents"]
            )
            fund["available_cents"] = max(
                0,
                fund["realized_net_grant_cents"]
                - book_spend
                - fund["in_flight_cents"]
                - fund["reserved_cents"],
            )
            fund["gap_cents"] = max(
                0,
                book_spend
                + fund["in_flight_cents"]
                - fund["realized_net_grant_cents"],
            )

        return {
            "campaign_id": campaign_id,
            "invoice_order": "registration_order_ascending",
            "funds_basis": (
                "realized_actual_grants_minus_reversals; provisional match "
                "is excluded and projects never borrow from each other"
            ),
            "projects": [funds[pid] for pid in sorted(funds)],
            "invoices": invoices,
        }

    @staticmethod
    def _invoice_from_snapshot(
        snapshot: dict[str, Any], invoice_id: int
    ) -> dict[str, Any] | None:
        return next(
            (i for i in snapshot["invoices"] if i["id"] == invoice_id), None
        )

    def _invoice_view(
        self, conn: sqlite3.Connection, campaign_id: str, invoice_id: int
    ) -> dict[str, Any]:
        snapshot = self._snapshot(conn, campaign_id)
        view = self._invoice_from_snapshot(snapshot, invoice_id)
        if view is None:
            raise LedgerError(
                "invoice_not_found",
                f"Invoice {invoice_id} does not exist.",
                status_code=404,
            )
        return view

    @staticmethod
    def _instruction_base(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        body = dict(row)
        body["amount_cents"] = int(body["amount_cents"])
        body["attempt_no"] = int(body["attempt_no"])
        return body

    def _instruction_view(
        self, conn: sqlite3.Connection, instruction: sqlite3.Row
    ) -> dict[str, Any]:
        body = self._instruction_base(instruction)
        instruction_number = body["instruction_number"]
        invoice_id = int(body["invoice_id"])
        share_rows = self._shares(conn, invoice_id)
        body["allocations"] = [
            {
                "project_id": row["project_id"],
                "responsible_project_id": row["responsible_project_id"],
                "share_cents": int(row["share_cents"]),
            }
            for row in share_rows
        ]
        bank_payment = self.bank.get(instruction_number)
        body["bank"] = bank_payment
        body["bank_events"] = self.bank.list_events(instruction_number)
        body["needs_result_sync"] = bool(
            bank_payment is not None
            and bank_payment["status"] in TERMINAL_PAYMENT_STATUSES
            and body["status"] == "pending"
        )
        body["ledger_bank_conflict"] = bool(
            bank_payment is not None
            and bank_payment["status"] in TERMINAL_PAYMENT_STATUSES
            and body["status"] in TERMINAL_PAYMENT_STATUSES
            and bank_payment["status"] != body["status"]
        )
        return body

    # ------------------------------------------------------------------
    # Invariants
    # ------------------------------------------------------------------
    def _assert_reimbursement_invariants(
        self, conn: sqlite3.Connection, campaign_id: str
    ) -> None:
        snapshot = self._snapshot(conn, campaign_id)
        for invoice in snapshot["invoices"]:
            share_total = sum(
                share["share_cents"] for share in invoice["shares"]
            )
            if share_total != invoice["total_amount_cents"]:
                raise LedgerError(
                    "invariant_invoice_share_total",
                    "Invoice shares do not add up to the invoice total.",
                    status_code=500,
                    details={"invoice_id": invoice["id"]},
                )
            statuses = [
                i["status"] for i in invoice["payment_instructions"]
            ]
            if statuses.count("pending") > 1:
                raise LedgerError(
                    "invariant_multiple_pending_payments",
                    "An invoice has more than one pending payment.",
                    status_code=500,
                    details={"invoice_id": invoice["id"]},
                )
            if statuses.count("succeeded") > 1:
                raise LedgerError(
                    "invariant_multiple_successful_payments",
                    "An invoice has more than one successful payment.",
                    status_code=500,
                    details={"invoice_id": invoice["id"]},
                )
            if "pending" in statuses and "succeeded" in statuses:
                raise LedgerError(
                    "invariant_invoice_paid_and_pending",
                    "An invoice is simultaneously paid and in flight.",
                    status_code=500,
                    details={"invoice_id": invoice["id"]},
                )
            for instruction in invoice["payment_instructions"]:
                if (
                    int(instruction["amount_cents"])
                    != invoice["total_amount_cents"]
                ):
                    raise LedgerError(
                        "invariant_instruction_amount",
                        "A payment instruction amount differs from its "
                        "invoice total.",
                        status_code=500,
                        details={
                            "invoice_id": invoice["id"],
                            "instruction_number": instruction[
                                "instruction_number"
                            ],
                        },
                    )

        for fund in snapshot["projects"]:
            book_spend = fund.get(
                "book_balance_spend_cents", fund["paid_cents"]
            )
            capacity = max(
                0,
                fund["realized_net_grant_cents"]
                - book_spend
                - fund["in_flight_cents"],
            )
            if fund["reserved_cents"] > capacity:
                raise LedgerError(
                    "invariant_reserved_over_capacity",
                    "Temporary invoice reservations exceed a project's "
                    "available realized funds.",
                    status_code=500,
                    details={"project_id": fund["project_id"]},
                )

        # Every invoice share must point at the project currently responsible
        # for its original shareholder.
        from .merger import MergerService

        for invoice in snapshot["invoices"]:
            for share in invoice["shares"]:
                expected = MergerService.current_responsible(
                    conn,
                    campaign_id,
                    share["project_id"],
                )
                if share["responsible_project_id"] != expected:
                    raise LedgerError(
                        "invariant_share_responsibility",
                        "An invoice share is not pointed at its current "
                        "responsible project.",
                        status_code=500,
                        details={
                            "invoice_id": invoice["id"],
                            "original_project_id": share["project_id"],
                            "stored_responsible_project_id": share[
                                "responsible_project_id"
                            ],
                            "expected_responsible_project_id": expected,
                        },
                    )

    # ------------------------------------------------------------------
    # Invoice registration
    # ------------------------------------------------------------------
    def register_invoice(
        self,
        campaign_id: str,
        *,
        source_event_id: str,
        payee: str,
        vendor_invoice_number: str,
        shares: list[dict[str, Any]],
    ) -> CommandResult:
        normalized_shares = sorted(
            (
                {
                    "project_id": str(share["project_id"]),
                    "share_cents": int(share["share_cents"]),
                }
                for share in shares
            ),
            key=lambda s: s["project_id"],
        )
        if not normalized_shares:
            raise LedgerError(
                "invoice_shares_required",
                "At least one project share is required.",
                status_code=422,
            )
        if any(share["share_cents"] <= 0 for share in normalized_shares):
            raise LedgerError(
                "invoice_share_not_positive",
                "Every project share must be a positive integer number of "
                "cents.",
                status_code=422,
            )
        project_ids = [s["project_id"] for s in normalized_shares]
        if len(set(project_ids)) != len(project_ids):
            raise LedgerError(
                "invoice_duplicate_project",
                "Each project may appear at most once in an invoice.",
                status_code=422,
            )

        total_amount_cents = sum(
            share["share_cents"] for share in normalized_shares
        )
        registration_hash = canonical_hash(
            {
                "campaign_id": campaign_id,
                "payee": payee,
                "vendor_invoice_number": vendor_invoice_number,
                "shares": normalized_shares,
            }
        )
        payload = {
            "operation": "invoice_register",
            "campaign_id": campaign_id,
            "source_event_id": source_event_id,
            "payee": payee,
            "vendor_invoice_number": vendor_invoice_number,
            "shares": normalized_shares,
        }

        with write_transaction(self.db_path) as conn:
            replay = LedgerService._idempotent_replay(
                conn, campaign_id, "invoice_register", source_event_id, payload
            )
            if replay is not None:
                return replay

            LedgerService._get_campaign(conn, campaign_id)
            for share in normalized_shares:
                project = LedgerService._get_project(
                    conn, campaign_id, share["project_id"]
                )
                if project["status"] != "active":
                    raise LedgerError(
                        "project_merged_no_new_invoice_share",
                        "A merged-out project can no longer take a share of a "
                        "new invoice; register the share under its current "
                        "responsible project instead.",
                        status_code=409,
                        details={
                            "project_id": share["project_id"],
                            "merged_into_project_id": project[
                                "merged_into_project_id"
                            ],
                        },
                    )

            existing = conn.execute(
                """
                SELECT * FROM invoices
                WHERE campaign_id = ? AND payee = ?
                  AND vendor_invoice_number = ?
                """,
                (campaign_id, payee, vendor_invoice_number),
            ).fetchone()
            if existing is not None:
                if existing["registration_hash"] != registration_hash:
                    raise LedgerError(
                        "invoice_registration_conflict",
                        "This campaign, payee, and vendor invoice number were "
                        "already registered with different shares.",
                        status_code=409,
                        details={
                            "invoice_id": int(existing["id"]),
                            "registered_total_amount_cents": int(
                                existing["total_amount_cents"]
                            ),
                        },
                    )
                body = self._invoice_view(
                    conn, campaign_id, int(existing["id"])
                )
                LedgerService._save_idempotency(
                    conn,
                    campaign_id=campaign_id,
                    operation="invoice_register",
                    source_event_id=source_event_id,
                    payload=payload,
                    status_code=200,
                    body=body,
                    resource_type="invoice",
                    resource_id=int(existing["id"]),
                )
                return CommandResult(200, body, True)

            cursor = conn.execute(
                """
                INSERT INTO invoices (
                    campaign_id, payee, vendor_invoice_number,
                    total_amount_cents, registration_hash, source_event_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign_id,
                    payee,
                    vendor_invoice_number,
                    total_amount_cents,
                    registration_hash,
                    source_event_id,
                ),
            )
            invoice_id = int(cursor.lastrowid)
            for share in normalized_shares:
                conn.execute(
                    """
                    INSERT INTO invoice_shares (
                        invoice_id, campaign_id, project_id,
                        responsible_project_id, share_cents
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        invoice_id,
                        campaign_id,
                        share["project_id"],
                        share["project_id"],
                        share["share_cents"],
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO invoice_consents (
                        invoice_id, campaign_id, project_id, consented,
                        last_source_event_id
                    ) VALUES (?, ?, ?, 0, NULL)
                    """,
                    (invoice_id, campaign_id, share["project_id"]),
                )

            self._assert_reimbursement_invariants(conn, campaign_id)
            body = self._invoice_view(conn, campaign_id, invoice_id)
            LedgerService._save_idempotency(
                conn,
                campaign_id=campaign_id,
                operation="invoice_register",
                source_event_id=source_event_id,
                payload=payload,
                status_code=201,
                body=body,
                resource_type="invoice",
                resource_id=invoice_id,
            )
            return CommandResult(201, body, False)

    # ------------------------------------------------------------------
    # Per-project consent
    # ------------------------------------------------------------------
    def set_invoice_consent(
        self,
        campaign_id: str,
        invoice_id: int,
        *,
        source_event_id: str,
        project_id: str,
        consented: bool,
    ) -> CommandResult:
        payload = {
            "operation": "invoice_consent",
            "campaign_id": campaign_id,
            "invoice_id": invoice_id,
            "source_event_id": source_event_id,
            "project_id": project_id,
            "consented": bool(consented),
        }
        with write_transaction(self.db_path) as conn:
            replay = LedgerService._idempotent_replay(
                conn, campaign_id, "invoice_consent", source_event_id, payload
            )
            if replay is not None:
                return replay

            LedgerService._get_campaign(conn, campaign_id)
            self._get_invoice(conn, campaign_id, invoice_id)

            share = conn.execute(
                """
                SELECT * FROM invoice_shares
                WHERE invoice_id = ? AND project_id = ?
                """,
                (invoice_id, project_id),
            ).fetchone()
            if share is None:
                # A successor re-confirms transferred shares under its own
                # project id; consent rows stay keyed by the immutable original
                # shareholder, so resolve them here.
                share = conn.execute(
                    """
                    SELECT * FROM invoice_shares
                    WHERE invoice_id = ? AND responsible_project_id = ?
                    """,
                    (invoice_id, project_id),
                ).fetchall()
            else:
                share = [share]
            if not share:
                raise LedgerError(
                    "invoice_project_not_shareholder",
                    f"Project {project_id!r} does not carry a share of "
                    f"invoice {invoice_id}.",
                    status_code=404,
                )
            share_rows = list(share)
            original_project_ids = {row["project_id"] for row in share_rows}
            responsible_ids = {
                row["responsible_project_id"] for row in share_rows
            }
            # Only the project currently responsible may (re-)confirm.  A
            # merged original shareholder can no longer act on its old share.
            if project_id not in responsible_ids:
                raise LedgerError(
                    "invoice_consent_project_not_responsible",
                    "The original shareholder has been merged out; the current "
                    "responsible project must confirm this share.",
                    status_code=409,
                    details={
                        "original_project_ids": sorted(original_project_ids),
                        "responsible_project_ids": sorted(responsible_ids),
                    },
                )
            # A direct original-shareholder call must also be the current
            # responsible project (handled above); here every row the caller
            # bears is updated together, so one successor confirmation covers
            # all transferred shares on this invoice.

            locked = conn.execute(
                """
                SELECT instruction_number, status
                FROM payment_instructions
                WHERE invoice_id = ? AND status IN ('pending', 'succeeded')
                ORDER BY attempt_no DESC
                LIMIT 1
                """,
                (invoice_id,),
            ).fetchone()
            if locked is not None:
                raise LedgerError(
                    "invoice_consent_locked",
                    "Consent cannot be changed while the invoice has an "
                    "in-flight or successful payment.",
                    status_code=409,
                    details={
                        "instruction_number": locked["instruction_number"],
                        "payment_status": locked["status"],
                    },
                )

            for row in share_rows:
                conn.execute(
                    """
                    INSERT INTO invoice_consents (
                        invoice_id, campaign_id, project_id, consented,
                        last_source_event_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(invoice_id, project_id) DO UPDATE SET
                        consented = excluded.consented,
                        last_source_event_id = excluded.last_source_event_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        invoice_id,
                        campaign_id,
                        row["project_id"],
                        1 if consented else 0,
                        source_event_id,
                        _now(),
                    ),
                )

            self._assert_reimbursement_invariants(conn, campaign_id)
            body = self._invoice_view(conn, campaign_id, invoice_id)
            LedgerService._save_idempotency(
                conn,
                campaign_id=campaign_id,
                operation="invoice_consent",
                source_event_id=source_event_id,
                payload=payload,
                status_code=200,
                body=body,
                resource_type="invoice",
                resource_id=invoice_id,
            )
            return CommandResult(200, body, False)

    # ------------------------------------------------------------------
    # Submission to the simulated bank
    # ------------------------------------------------------------------
    def submit_payment(
        self,
        campaign_id: str,
        invoice_id: int,
        *,
        source_event_id: str,
    ) -> CommandResult:
        payload = {
            "operation": "payment_submit",
            "campaign_id": campaign_id,
            "invoice_id": invoice_id,
            "source_event_id": source_event_id,
        }

        with write_transaction(self.db_path) as conn:
            replay = LedgerService._idempotent_replay(
                conn, campaign_id, "payment_submit", source_event_id, payload
            )
            if replay is not None:
                # A retried submission always refers to the same instruction.
                # Never allocate a fresh attempt for an already-recorded one.
                original_number = str(replay.body["instruction_number"])
                row = self._get_instruction(
                    conn, campaign_id, original_number
                )
                body = self._instruction_view(conn, row)
                result = CommandResult(replay.status_code, body, True)
                instruction_for_bank = (
                    body if body["status"] == "pending" else None
                )
            else:
                LedgerService._get_campaign(conn, campaign_id)
                invoice = self._get_invoice(
                    conn, campaign_id, invoice_id
                )

                in_flight = conn.execute(
                    """
                    SELECT * FROM payment_instructions
                    WHERE invoice_id = ? AND status = 'pending'
                    """,
                    (invoice_id,),
                ).fetchone()
                if in_flight is not None:
                    raise LedgerError(
                        "invoice_payment_in_flight",
                        "This invoice already has an in-flight payment; query "
                        "or confirm that instruction instead of paying again.",
                        status_code=409,
                        details={
                            "instruction_number": in_flight[
                                "instruction_number"
                            ],
                            "payment_status": "pending",
                        },
                    )
                paid = conn.execute(
                    """
                    SELECT * FROM payment_instructions
                    WHERE invoice_id = ? AND status = 'succeeded'
                    """,
                    (invoice_id,),
                ).fetchone()
                if paid is not None:
                    raise LedgerError(
                        "invoice_already_paid",
                        "This invoice has already been paid successfully.",
                        status_code=409,
                        details={
                            "instruction_number": paid["instruction_number"]
                        },
                    )

                snapshot = self._snapshot(conn, campaign_id)
                view = self._invoice_from_snapshot(snapshot, invoice_id)
                if view is None:
                    raise LedgerError(
                        "invoice_not_found",
                        f"Invoice {invoice_id} does not exist.",
                        status_code=404,
                    )
                if not view["payable"]:
                    raise LedgerError(
                        "invoice_not_payable",
                        "The whole invoice is not payable at this moment.",
                        status_code=409,
                        details={
                            "reason": view["not_payable_reason"],
                            "missing_consent_projects": view[
                                "missing_consent_projects"
                            ],
                            "shortfall_cents_by_project": view[
                                "shortfall_cents_by_project"
                            ],
                        },
                    )

                attempt_row = conn.execute(
                    """
                    SELECT COALESCE(MAX(attempt_no), 0) AS max_attempt
                    FROM payment_instructions
                    WHERE invoice_id = ?
                    """,
                    (invoice_id,),
                ).fetchone()
                attempt_no = int(attempt_row["max_attempt"]) + 1
                instruction_number = f"PAY-{invoice_id}-{attempt_no}"

                cursor = conn.execute(
                    """
                    INSERT INTO payment_instructions (
                        campaign_id, invoice_id, instruction_number,
                        attempt_no, payee, vendor_invoice_number,
                        amount_cents, status, request_source_event_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        campaign_id,
                        invoice_id,
                        instruction_number,
                        attempt_no,
                        invoice["payee"],
                        invoice["vendor_invoice_number"],
                        int(invoice["total_amount_cents"]),
                        source_event_id,
                    ),
                )
                instruction_pk = int(cursor.lastrowid)
                row = conn.execute(
                    "SELECT * FROM payment_instructions WHERE id = ?",
                    (instruction_pk,),
                ).fetchone()
                body = self._instruction_base(row)

                self._assert_reimbursement_invariants(conn, campaign_id)
                LedgerService._save_idempotency(
                    conn,
                    campaign_id=campaign_id,
                    operation="payment_submit",
                    source_event_id=source_event_id,
                    payload=payload,
                    status_code=201,
                    body=body,
                    resource_type="payment_instruction",
                    resource_id=instruction_number,
                )
                result = CommandResult(201, body, False)
                instruction_for_bank = body

        # The ledger instruction is durably pending before the bank is called.
        # If the bank accepted but its response was lost, a retry still returns
        # this same instruction; the bank de-duplicates by instruction number.
        if (
            instruction_for_bank is not None
            and instruction_for_bank.get("status") == "pending"
        ):
            self._ensure_bank_submitted(instruction_for_bank)
        return result

    def _ensure_bank_submitted(self, instruction: dict[str, Any]) -> None:
        instruction_number = str(instruction["instruction_number"])
        if self.bank.get(instruction_number) is not None:
            return
        try:
            self.bank.submit(
                instruction_number=instruction_number,
                payee=str(instruction["payee"]),
                vendor_invoice_number=str(
                    instruction["vendor_invoice_number"]
                ),
                amount_cents=int(instruction["amount_cents"]),
            )
        except BankResponseLost:
            # Accepted by the bank but the response never reached the ledger.
            # The pending instruction remains recoverable via GET/sync.
            pass
        except BankError:
            # The ledger instruction is already durable.  A hard bank conflict
            # is surfaced through the instruction view rather than rolling
            # back the accepted reservation.
            pass

    # ------------------------------------------------------------------
    # Bank results
    # ------------------------------------------------------------------
    def deliver_payment_result(
        self,
        campaign_id: str,
        instruction_number: str,
        *,
        outcome: str,
        result_event_id: str,
    ) -> CommandResult:
        if outcome not in TERMINAL_PAYMENT_STATUSES:
            raise LedgerError(
                "invalid_payment_outcome",
                "Outcome must be 'succeeded' or 'failed'.",
                status_code=422,
            )

        # Verify the instruction belongs to this campaign *before* touching the
        # bank, so a wrong-campaign call cannot mutate provider state.  A
        # ledger-confirmed contradictory terminal result is rejected up front
        # so the bank is never told to contradict an already-confirmed result.
        with read_conn(self.db_path) as conn:
            LedgerService._get_campaign(conn, campaign_id)
            existing = self._get_instruction(
                conn, campaign_id, instruction_number
            )
            if (
                existing["status"] in TERMINAL_PAYMENT_STATUSES
                and existing["status"] != outcome
            ):
                raise LedgerError(
                    "payment_result_conflict",
                    "A different terminal result is already confirmed for "
                    "this instruction.",
                    status_code=409,
                    details={
                        "instruction_number": instruction_number,
                        "confirmed_status": existing["status"],
                        "rejected_status": outcome,
                    },
                )

        try:
            bank_result = self.bank.confirm(
                instruction_number,
                outcome=outcome,
                result_event_id=result_event_id,
            )
        except BankError as exc:
            raise LedgerError(
                exc.code,
                exc.message,
                status_code=exc.status_code,
                details=exc.details,
            ) from exc

        replayed = False
        with write_transaction(self.db_path) as conn:
            LedgerService._get_campaign(conn, campaign_id)
            instruction = self._get_instruction(
                conn, campaign_id, instruction_number
            )

            if instruction["status"] == outcome:
                replayed = True
            elif instruction["status"] == "pending":
                changed_at = _now()
                conn.execute(
                    """
                    UPDATE payment_instructions
                    SET status = ?, result_event_id = ?, result_at = ?,
                        updated_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (
                        outcome,
                        result_event_id,
                        bank_result["payment"].get("result_at")
                        or changed_at,
                        changed_at,
                        int(instruction["id"]),
                    ),
                )
            else:
                raise LedgerError(
                    "payment_result_conflict",
                    "A different terminal result is already confirmed for "
                    "this instruction.",
                    status_code=409,
                    details={
                        "instruction_number": instruction_number,
                        "confirmed_status": instruction["status"],
                        "rejected_status": outcome,
                    },
                )

            self._assert_reimbursement_invariants(conn, campaign_id)
            refreshed = self._get_instruction(
                conn, campaign_id, instruction_number
            )
            body = self._instruction_view(conn, refreshed)

        return CommandResult(200, body, replayed)

    def sync_payment_result(
        self, campaign_id: str, instruction_number: str
    ) -> dict[str, Any]:
        """Recover/reconcile one instruction with the simulated bank.

        Safe after a restart or a lost submit response.  If the ledger is
        ``pending`` but the bank never received the instruction (the process
        died before submission), the same instruction number is re-submitted --
        never a new payment.  If the bank already has an explicit result, it is
        pulled into the ledger.  A ``pending`` instruction with no terminal
        result stays pending: an unclear outcome is not treated as failed.
        """
        with write_transaction(self.db_path) as conn:
            LedgerService._get_campaign(conn, campaign_id)
            instruction = self._get_instruction(
                conn, campaign_id, instruction_number
            )
            # Re-read inside the serialized ledger transaction so a result
            # delivered between the initial read and this point is not missed.
            bank_payment = self.bank.get(instruction_number)

            if bank_payment is not None and (
                bank_payment["status"] in TERMINAL_PAYMENT_STATUSES
                and instruction["status"] == "pending"
            ):
                changed_at = _now()
                conn.execute(
                    """
                    UPDATE payment_instructions
                    SET status = ?, result_event_id = ?, result_at = ?,
                        updated_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (
                        bank_payment["status"],
                        bank_payment.get("result_event_id")
                        or f"bank-sync-{instruction_number}",
                        bank_payment.get("result_at") or changed_at,
                        changed_at,
                        int(instruction["id"]),
                    ),
                )
                self._assert_reimbursement_invariants(conn, campaign_id)

            refreshed = self._get_instruction(
                conn, campaign_id, instruction_number
            )
            view = self._instruction_view(conn, refreshed)
            resubmit = bool(
                view["status"] == "pending" and view["bank"] is None
            )

        if resubmit:
            # Only the very same instruction number is replayed; the bank
            # de-duplicates.  This can never be a second payment.
            self._ensure_bank_submitted(view)
            with read_conn(self.db_path) as conn:
                refreshed = self._get_instruction(
                    conn, campaign_id, instruction_number
                )
                view = self._instruction_view(conn, refreshed)
        return view

    # ------------------------------------------------------------------
    # Read models
    # ------------------------------------------------------------------
    def invoice_overview(self, campaign_id: str) -> dict[str, Any]:
        with read_conn(self.db_path) as conn:
            LedgerService._get_campaign(conn, campaign_id)
            return self._snapshot(conn, campaign_id)

    def project_funds(self, campaign_id: str) -> dict[str, Any]:
        with read_conn(self.db_path) as conn:
            LedgerService._get_campaign(conn, campaign_id)
            snapshot = self._snapshot(conn, campaign_id)
            return {
                "campaign_id": campaign_id,
                "funds_basis": snapshot["funds_basis"],
                "projects": snapshot["projects"],
            }

    def get_invoice(
        self, campaign_id: str, invoice_id: int
    ) -> dict[str, Any]:
        with read_conn(self.db_path) as conn:
            LedgerService._get_campaign(conn, campaign_id)
            return self._invoice_view(conn, campaign_id, invoice_id)

    def list_invoice_payments(
        self, campaign_id: str, invoice_id: int
    ) -> dict[str, Any]:
        with read_conn(self.db_path) as conn:
            LedgerService._get_campaign(conn, campaign_id)
            self._get_invoice(conn, campaign_id, invoice_id)
            instructions = self._instructions(conn, invoice_id)
            views = []
            for instruction in instructions:
                row = conn.execute(
                    "SELECT * FROM payment_instructions WHERE id = ?",
                    (instruction["id"],),
                ).fetchone()
                views.append(self._instruction_view(conn, row))
            return {
                "campaign_id": campaign_id,
                "invoice_id": invoice_id,
                "payment_count": len(views),
                "payment_instructions": views,
            }

    def get_payment_instruction(
        self, campaign_id: str, instruction_number: str
    ) -> dict[str, Any]:
        with read_conn(self.db_path) as conn:
            LedgerService._get_campaign(conn, campaign_id)
            instruction = self._get_instruction(
                conn, campaign_id, instruction_number
            )
            return self._instruction_view(conn, instruction)

    def list_payment_instructions(
        self, campaign_id: str
    ) -> dict[str, Any]:
        with read_conn(self.db_path) as conn:
            LedgerService._get_campaign(conn, campaign_id)
            rows = conn.execute(
                """
                SELECT * FROM payment_instructions
                WHERE campaign_id = ?
                ORDER BY id ASC
                """,
                (campaign_id,),
            ).fetchall()
            views = [self._instruction_view(conn, row) for row in rows]
            return {
                "campaign_id": campaign_id,
                "payment_count": len(views),
                "payment_instructions": views,
            }
