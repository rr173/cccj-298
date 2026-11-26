"""Project handovers (mergers) for a campaign.

When an open-source project shuts down, finance hands its remaining money and
all outstanding obligations to another project in the same campaign.  A merger:

* names two distinct, still-active projects in one campaign;
* is effective immediately, takes effect exactly once and is irreversible;
* books the source's *book balance* at the effective moment as an internal
  transfer (actual spend already deducted, in-flight holds NOT deducted; the
  amount may be negative) -- the same amount leaves the source and enters the
  successor;
* re-points every invoice share the source still carries to the successor and
  invalidates the source's prior consent on not-yet-paid shares, so the
  successor re-confirms transferred shares;
* never changes a payment instruction: an in-flight payment keeps its
  instruction, amount and ordering, and the successor simply assumes the
  whole occupied hold;
* never submits or synchronizes a payment.

Responsibility over historical money is tracked explicitly instead of being
mutated in place:

* ``grant_items`` / ``reversals`` keep the donation's original project id and
  additionally record the project responsible when the event happened;
* ``invoice_shares`` keep the immutable original shareholder and a current
  responsible project;
* the ``project_mergers`` chain answers "which project is responsible now" and
  "which project was responsible at a given moment".

All writes run inside a ``BEGIN IMMEDIATE`` transaction, so a merger
interleaved with a payment, refund, bank confirmation or closing cutoff always
has one clear business ordering: the same money can never be spendable on both
sides, an in-flight hold is never lost, and a pre-merger payment can never be
attributed after the merger.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from .bank import SimulatedBank, resolve_bank_db_path
from .db import read_conn, write_transaction
from .service import CommandResult, LedgerError, LedgerService, canonical_hash

ACTIVE = "active"
MERGED = "merged"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def at_or_before(value: str | None, limit: str | None) -> bool:
    if value is None:
        return False
    moment = parse_time(value)
    boundary = parse_time(limit)
    if moment is None or boundary is None:
        return False
    return moment <= boundary


class MergerService:
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
        # Ensure the independent bank schema exists without ever submitting or
        # confirming a payment.  The merger only reads bank confirmation times.
        try:
            SimulatedBank(self.bank_db_path)
        except (sqlite3.Error, OSError):
            pass

    # ------------------------------------------------------------------
    # Responsibility-chain helpers (shared with other services)
    # ------------------------------------------------------------------
    @staticmethod
    def merger_rows(
        conn: sqlite3.Connection, campaign_id: str
    ) -> list[sqlite3.Row]:
        return list(
            conn.execute(
                """
                SELECT * FROM project_mergers
                WHERE campaign_id = ?
                ORDER BY id ASC
                """,
                (campaign_id,),
            ).fetchall()
        )

    @staticmethod
    def current_responsible(
        conn: sqlite3.Connection, campaign_id: str, project_id: str
    ) -> str:
        """Follow the merger chain to the project responsible now."""
        current = project_id
        for _ in range(1000):  # defensive bound on chain length
            row = conn.execute(
                """
                SELECT successor_project_id FROM project_mergers
                WHERE campaign_id = ? AND source_project_id = ?
                """,
                (campaign_id, current),
            ).fetchone()
            if row is None:
                return current
            current = row["successor_project_id"]
        raise LedgerError(
            "merger_chain_cycle",
            "The merger chain is unexpectedly long or cyclic.",
            status_code=500,
            details={"campaign_id": campaign_id, "project_id": project_id},
        )

    @staticmethod
    def responsible_at(
        conn: sqlite3.Connection,
        campaign_id: str,
        project_id: str,
        moment: str | None,
    ) -> str:
        """Resolve the responsible project for ``project_id`` at ``moment``.

        A merger only changes responsibility from its ``effective_at`` on, so
        an event (e.g. a bank confirmation) is attributed to the project that
        was responsible when the event happened, never retroactively.
        """
        if moment is None:
            return MergerService.current_responsible(
                conn, campaign_id, project_id
            )
        current = project_id
        for _ in range(1000):
            row = conn.execute(
                """
                SELECT successor_project_id, effective_at
                FROM project_mergers
                WHERE campaign_id = ? AND source_project_id = ?
                """,
                (campaign_id, current),
            ).fetchone()
            if row is None:
                return current
            if at_or_before(str(row["effective_at"]), moment):
                # The merger was already effective at the event moment: the
                # successor (and possibly its own successors) was responsible.
                current = row["successor_project_id"]
            else:
                # The merger happened after the event: responsibility stays
                # with this project at the event moment.
                return current
        raise LedgerError(
            "merger_chain_cycle",
            "The merger chain is unexpectedly long or cyclic.",
            status_code=500,
            details={"campaign_id": campaign_id, "project_id": project_id},
        )

    @staticmethod
    def _book_components(
        conn: sqlite3.Connection,
        campaign_id: str,
        project_id: str,
        bank_rows: dict[str, dict[str, Any]] | None,
    ) -> dict[str, int]:
        """Reconstruct a project's book balance from immutable events.

        ``book = realized grants - reversals - bank-confirmed successful
        reimbursement shares + internal transfer-ins - transfer-outs``.

        Expense attribution follows the *bank's* first confirmation time and
        the responsibility chain at that moment (bank-first).  A success the
        bank has confirmed but the ledger has not synced yet is therefore
        already deducted, which keeps the transferred balance consistent with
        the closing rules; the merger itself never syncs the instruction.
        """
        grants = int(
            conn.execute(
                """
                SELECT COALESCE(SUM(amount_cents), 0) AS total
                FROM grant_items
                WHERE campaign_id = ? AND responsible_project_id = ?
                """,
                (campaign_id, project_id),
            ).fetchone()["total"]
        )
        reversals = int(
            conn.execute(
                """
                SELECT COALESCE(SUM(amount_cents), 0) AS total
                FROM reversals
                WHERE campaign_id = ? AND responsible_project_id = ?
                """,
                (campaign_id, project_id),
            ).fetchone()["total"]
        )
        transfers_in = int(
            conn.execute(
                """
                SELECT COALESCE(SUM(amount_cents), 0) AS total
                FROM internal_transfer_entries
                WHERE campaign_id = ? AND project_id = ? AND direction = 'in'
                """,
                (campaign_id, project_id),
            ).fetchone()["total"]
        )
        transfers_out = int(
            conn.execute(
                """
                SELECT COALESCE(SUM(amount_cents), 0) AS total
                FROM internal_transfer_entries
                WHERE campaign_id = ? AND project_id = ? AND direction = 'out'
                """,
                (campaign_id, project_id),
            ).fetchone()["total"]
        )

        bank_confirmed_spend = 0
        if bank_rows is not None:
            instructions = conn.execute(
                """
                SELECT pi.instruction_number
                FROM payment_instructions pi
                WHERE pi.campaign_id = ?
                """,
                (campaign_id,),
            ).fetchall()
            for instruction in instructions:
                number = instruction["instruction_number"]
                bank_row = bank_rows.get(number)
                if bank_row is None or bank_row["status"] != "succeeded":
                    continue
                result_at = bank_row.get("result_at")
                share_rows = conn.execute(
                    """
                    SELECT s.project_id, s.share_cents
                    FROM invoice_shares s
                    JOIN payment_instructions pi
                      ON pi.invoice_id = s.invoice_id
                    WHERE pi.instruction_number = ?
                    """,
                    (number,),
                ).fetchall()
                for share in share_rows:
                    responsible = MergerService.responsible_at(
                        conn,
                        campaign_id,
                        share["project_id"],
                        result_at,
                    )
                    if responsible == project_id:
                        bank_confirmed_spend += int(share["share_cents"])

        book = (
            grants
            - reversals
            + transfers_in
            - transfers_out
            - bank_confirmed_spend
        )
        return {
            "grants_cents": grants,
            "reversals_cents": reversals,
            "transfers_in_cents": transfers_in,
            "transfers_out_cents": transfers_out,
            "bank_confirmed_spend_cents": bank_confirmed_spend,
            "book_balance_cents": book,
        }

    def _read_bank_rows(
        self,
    ) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
        try:
            with read_conn(self.bank_db_path) as bank_conn:
                return {
                    row["instruction_number"]: dict(row)
                    for row in bank_conn.execute(
                        "SELECT * FROM bank_payments"
                    ).fetchall()
                }, None
        except (sqlite3.Error, OSError) as exc:
            return None, str(exc)

    # ------------------------------------------------------------------
    # Command: merge one project into another
    # ------------------------------------------------------------------
    def merge_projects(
        self,
        campaign_id: str,
        *,
        source_event_id: str,
        source_project_id: str,
        successor_project_id: str,
    ) -> CommandResult:
        if source_project_id == successor_project_id:
            raise LedgerError(
                "merger_same_project",
                "A project cannot be merged into itself.",
                status_code=422,
                details={
                    "source_project_id": source_project_id,
                    "successor_project_id": successor_project_id,
                },
            )
        payload = {
            "operation": "project_merger",
            "campaign_id": campaign_id,
            "source_event_id": source_event_id,
            "source_project_id": source_project_id,
            "successor_project_id": successor_project_id,
        }
        with write_transaction(self.db_path) as conn:
            replay = LedgerService._idempotent_replay(
                conn, campaign_id, "project_merger", source_event_id, payload
            )
            if replay is not None:
                return replay

            LedgerService._get_campaign(conn, campaign_id)
            source = LedgerService._get_project(
                conn, campaign_id, source_project_id
            )
            successor = LedgerService._get_project(
                conn, campaign_id, successor_project_id
            )
            if source["status"] != ACTIVE:
                raise LedgerError(
                    "merger_source_not_active",
                    "The source project has already been merged out and can no "
                    "longer be merged.",
                    status_code=409,
                    details={
                        "source_project_id": source_project_id,
                        "status": source["status"],
                        "merged_into_project_id": source[
                            "merged_into_project_id"
                        ],
                    },
                )
            if successor["status"] != ACTIVE:
                raise LedgerError(
                    "merger_successor_not_active",
                    "The successor project has already been merged out and "
                    "cannot take over another project.",
                    status_code=409,
                    details={
                        "successor_project_id": successor_project_id,
                        "status": successor["status"],
                    },
                )

            effective_at = _now()
            # Bank-first book balance: bank-confirmed successes (even ones the
            # ledger has not synced) are already deducted.  The bank record is
            # authoritative, so a merger refuses to proceed when it cannot be
            # read: proceeding on ledger-only numbers could transfer a balance
            # that already left the bank and let both sides spend the same
            # money.  The merger itself never submits or syncs a payment.
            bank_rows, bank_error = self._read_bank_rows()
            if bank_rows is None:
                raise LedgerError(
                    "merger_bank_unavailable",
                    "The bank record could not be read, so the book balance to "
                    "transfer cannot be determined safely; restore the bank "
                    "database and retry the same source event.",
                    status_code=503,
                    details={"error": bank_error},
                )
            components = self._book_components(
                conn,
                campaign_id,
                source_project_id,
                bank_rows,
            )
            transferred = int(components["book_balance_cents"])

            cursor = conn.execute(
                """
                INSERT INTO project_mergers (
                    campaign_id, source_project_id, successor_project_id,
                    source_event_id, transferred_book_balance_cents,
                    effective_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign_id,
                    source_project_id,
                    successor_project_id,
                    source_event_id,
                    transferred,
                    effective_at,
                ),
            )
            merger_id = int(cursor.lastrowid)

            # Internal transfer pair: source out (signed negative stored as a
            # positive amount with direction), successor in with the same sum.
            conn.execute(
                """
                INSERT INTO internal_transfer_entries (
                    campaign_id, merger_id, project_id, direction,
                    amount_cents, effective_at
                ) VALUES (?, ?, ?, 'out', ?, ?)
                """,
                (
                    campaign_id,
                    merger_id,
                    source_project_id,
                    transferred,
                    effective_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO internal_transfer_entries (
                    campaign_id, merger_id, project_id, direction,
                    amount_cents, effective_at
                ) VALUES (?, ?, ?, 'in', ?, ?)
                """,
                (
                    campaign_id,
                    merger_id,
                    successor_project_id,
                    transferred,
                    effective_at,
                ),
            )

            # The source is now merged out, irreversibly.
            conn.execute(
                """
                UPDATE projects
                SET status = 'merged', merged_into_project_id = ?
                WHERE campaign_id = ? AND id = ?
                """,
                (successor_project_id, campaign_id, source_project_id),
            )

            # Re-point every share the source still carries.  Shares whose
            # original shareholder is some other project are untouched, so a
            # later merger of that project still follows its own chain.
            transferred_share_rows = conn.execute(
                """
                SELECT invoice_id, project_id, share_cents
                FROM invoice_shares
                WHERE campaign_id = ? AND responsible_project_id = ?
                """,
                (campaign_id, source_project_id),
            ).fetchall()
            conn.execute(
                """
                UPDATE invoice_shares
                SET responsible_project_id = ?
                WHERE campaign_id = ? AND responsible_project_id = ?
                """,
                (successor_project_id, campaign_id, source_project_id),
            )

            # Invalidate the source's prior consent on every transferred
            # share of an invoice that is not already successfully paid.  An
            # in-flight instruction keeps its hold and cannot be changed, but
            # if it later fails the stale consent is already gone, so the
            # successor must re-confirm before the invoice can queue again.
            # Shares of an already-succeeded invoice are historical and their
            # consent stays locked as the final record of who authorized it.
            for share in transferred_share_rows:
                succeeded = conn.execute(
                    """
                    SELECT 1 FROM payment_instructions
                    WHERE invoice_id = ? AND status = 'succeeded'
                    LIMIT 1
                    """,
                    (share["invoice_id"],),
                ).fetchone()
                if succeeded is not None:
                    continue
                conn.execute(
                    """
                    UPDATE invoice_consents
                    SET consented = 0, last_source_event_id = NULL,
                        updated_at = ?
                    WHERE invoice_id = ? AND project_id = ?
                    """,
                    (
                        effective_at,
                        share["invoice_id"],
                        share["project_id"],
                    ),
                )

            self._assert_merger_invariants(conn, campaign_id)
            body = self._merger_view(conn, merger_id)
            LedgerService._save_idempotency(
                conn,
                campaign_id=campaign_id,
                operation="project_merger",
                source_event_id=source_event_id,
                payload=payload,
                status_code=201,
                body=body,
                resource_type="project_merger",
                resource_id=merger_id,
            )
            return CommandResult(201, body, False)

    # ------------------------------------------------------------------
    # Invariants
    # ------------------------------------------------------------------
    @classmethod
    def _assert_merger_invariants(
        cls, conn: sqlite3.Connection, campaign_id: str
    ) -> None:
        # Every invoice share's responsible project must be an existing
        # project reachable by following merger records from the original
        # shareholder.
        for share in conn.execute(
            """
            SELECT invoice_id, project_id, responsible_project_id
            FROM invoice_shares WHERE campaign_id = ?
            """,
            (campaign_id,),
        ).fetchall():
            current = cls.current_responsible(
                conn, campaign_id, share["project_id"]
            )
            if current != share["responsible_project_id"]:
                raise LedgerError(
                    "invariant_share_responsibility",
                    "An invoice share is not pointed at its current "
                    "responsible project.",
                    status_code=500,
                    details={
                        "invoice_id": share["invoice_id"],
                        "original_project_id": share["project_id"],
                        "stored_responsible_project_id": share[
                            "responsible_project_id"
                        ],
                        "expected_responsible_project_id": current,
                    },
                )

        # Each transfer pair must net to zero within one merger, so the
        # campaign's total book balance can never be created or destroyed by a
        # handover.
        for merger in conn.execute(
            "SELECT id FROM project_mergers WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchall():
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN direction='in' THEN amount_cents
                                      ELSE -amount_cents END), 0) AS net
                FROM internal_transfer_entries WHERE merger_id = ?
                """,
                (merger["id"],),
            ).fetchone()
            if int(row["net"]) != 0:
                raise LedgerError(
                    "invariant_transfer_not_balanced",
                    "A merger's transfer-in and transfer-out do not net to zero.",
                    status_code=500,
                    details={"merger_id": int(merger["id"])},
                )

    # ------------------------------------------------------------------
    # Read models
    # ------------------------------------------------------------------
    def _merger_view(
        self,
        conn: sqlite3.Connection,
        merger_id: int,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM project_mergers WHERE id = ?", (merger_id,)
        ).fetchone()
        if row is None:
            raise LedgerError(
                "merger_not_found",
                f"Merger {merger_id} does not exist.",
                status_code=404,
            )
        entries = [
            dict(r)
            for r in conn.execute(
                """
                SELECT * FROM internal_transfer_entries
                WHERE merger_id = ? ORDER BY id ASC
                """,
                (merger_id,),
            ).fetchall()
        ]
        for entry in entries:
            entry["amount_cents"] = int(entry["amount_cents"])
        transferred_shares = [
            dict(r)
            for r in conn.execute(
                """
                SELECT invoice_id, project_id AS original_project_id,
                       responsible_project_id, share_cents
                FROM invoice_shares
                WHERE campaign_id = ? AND responsible_project_id = ?
                  AND project_id IN (
                      SELECT source_project_id FROM project_mergers WHERE id = ?
                  )
                ORDER BY invoice_id ASC, project_id ASC
                """,
                (
                    row["campaign_id"],
                    row["successor_project_id"],
                    merger_id,
                ),
            ).fetchall()
        ]
        for share in transferred_shares:
            share["share_cents"] = int(share["share_cents"])
            share["invoice_id"] = int(share["invoice_id"])
        return {
            "id": int(row["id"]),
            "campaign_id": row["campaign_id"],
            "source_event_id": row["source_event_id"],
            "source_project_id": row["source_project_id"],
            "successor_project_id": row["successor_project_id"],
            "transferred_book_balance_cents": int(
                row["transferred_book_balance_cents"]
            ),
            "effective_at": row["effective_at"],
            "created_at": row["created_at"],
            "irreversible": True,
            "internal_transfer_entries": entries,
            "transferred_invoice_shares": transferred_shares,
            "bank_balance_basis": "bank_first_confirmed_spend_deducted",
        }

    def get_merger(self, campaign_id: str, merger_id: int) -> dict[str, Any]:
        with read_conn(self.db_path) as conn:
            LedgerService._get_campaign(conn, campaign_id)
            return self._merger_view(conn, merger_id)

    def list_mergers(self, campaign_id: str) -> dict[str, Any]:
        with read_conn(self.db_path) as conn:
            LedgerService._get_campaign(conn, campaign_id)
            rows = conn.execute(
                """
                SELECT * FROM project_mergers
                WHERE campaign_id = ? ORDER BY id ASC
                """,
                (campaign_id,),
            ).fetchall()
            mergers = [self._merger_view(conn, int(r["id"])) for r in rows]
            return {
                "campaign_id": campaign_id,
                "merger_count": len(mergers),
                "mergers": mergers,
            }

    def project_responsibility(self, campaign_id: str) -> dict[str, Any]:
        """Which project currently answers for every registered project."""
        with read_conn(self.db_path) as conn:
            LedgerService._get_campaign(conn, campaign_id)
            projects = conn.execute(
                """
                SELECT * FROM projects
                WHERE campaign_id = ? ORDER BY id ASC
                """,
                (campaign_id,),
            ).fetchall()
            result = []
            for project in projects:
                chain: list[str] = []
                current = project["id"]
                seen = set()
                while current not in seen:
                    seen.add(current)
                    row = conn.execute(
                        """
                        SELECT id, successor_project_id, effective_at
                        FROM project_mergers
                        WHERE campaign_id = ? AND source_project_id = ?
                        """,
                        (campaign_id, current),
                    ).fetchone()
                    if row is None:
                        break
                    chain.append(
                        {
                            "merger_id": int(row["id"]),
                            "from_project_id": current,
                            "to_project_id": row["successor_project_id"],
                            "effective_at": row["effective_at"],
                        }
                    )
                    current = row["successor_project_id"]
                result.append(
                    {
                        "project_id": project["id"],
                        "name": project["name"],
                        "status": project["status"],
                        "is_active": project["status"] == ACTIVE,
                        "merged_into_project_id": project[
                            "merged_into_project_id"
                        ],
                        "current_responsible_project_id": (
                            chain[-1]["to_project_id"] if chain else project["id"]
                        ),
                        "merger_chain": chain,
                    }
                )
            return {
                "campaign_id": campaign_id,
                "projects": result,
            }
