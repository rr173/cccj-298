"""Core rolling matching ledger.

All money values are integer cents.  Write operations are atomic and
idempotent.  Actual grants and reversals are append-only records; the
"provisional" view is recomputed from those records in donation acceptance
order.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .db import read_conn, write_transaction


class LedgerError(Exception):
    """A deterministic business-rule or idempotency error."""

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


@dataclass(frozen=True)
class CommandResult:
    status_code: int
    body: dict[str, Any]
    replayed: bool = False


def canonical_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now() -> str:
    # Python-side timestamp is only used where SQLite defaults are unavailable;
    # ISO UTC is readable in JSON.
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class LedgerService:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _idempotent_replay(
        conn: sqlite3.Connection,
        campaign_id: str,
        operation: str,
        source_event_id: str,
        payload: dict[str, Any],
    ) -> CommandResult | None:
        request_hash = canonical_hash(payload)
        row = conn.execute(
            """
            SELECT request_hash, status_code, response_json
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
        return CommandResult(
            status_code=200,
            body=json.loads(row["response_json"]),
            replayed=True,
        )

    @staticmethod
    def _save_idempotency(
        conn: sqlite3.Connection,
        *,
        campaign_id: str,
        operation: str,
        source_event_id: str,
        payload: dict[str, Any],
        status_code: int,
        body: dict[str, Any],
        resource_type: str,
        resource_id: str | int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO idempotency_records (
                campaign_id, operation, source_event_id, request_hash,
                status_code, response_json, resource_type, resource_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                campaign_id,
                operation,
                source_event_id,
                canonical_hash(payload),
                status_code,
                json.dumps(body, ensure_ascii=False, sort_keys=True),
                resource_type,
                str(resource_id),
            ),
        )

    @staticmethod
    def _get_campaign(
        conn: sqlite3.Connection, campaign_id: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
        ).fetchone()
        if row is None:
            raise LedgerError(
                "campaign_not_found",
                f"Campaign {campaign_id!r} does not exist.",
                status_code=404,
            )
        return row

    @staticmethod
    def _get_project(
        conn: sqlite3.Connection, campaign_id: str, project_id: str
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT * FROM projects
            WHERE campaign_id = ? AND id = ?
            """,
            (campaign_id, project_id),
        ).fetchone()
        if row is None:
            raise LedgerError(
                "project_not_found",
                f"Project {project_id!r} does not exist in campaign "
                f"{campaign_id!r}.",
                status_code=404,
            )
        return row

    @staticmethod
    def _get_donation_by_source(
        conn: sqlite3.Connection,
        campaign_id: str,
        source_event_id: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT * FROM donations
            WHERE campaign_id = ? AND source_event_id = ?
            """,
            (campaign_id, source_event_id),
        ).fetchone()
        if row is None:
            raise LedgerError(
                "donation_not_found",
                f"Donation source_event_id={source_event_id!r} was not found.",
                status_code=404,
            )
        return row

    @staticmethod
    def _donation_rows(
        conn: sqlite3.Connection, campaign_id: str
    ) -> list[sqlite3.Row]:
        return list(
            conn.execute(
                """
                SELECT
                    d.id,
                    d.campaign_id,
                    d.source_event_id,
                    d.donor_id,
                    d.project_id,
                    d.amount_cents,
                    d.note,
                    d.created_at,
                    COALESCE(rf.refunded_cents, 0) AS refunded_cents,
                    COALESCE(g.granted_cents, 0) AS gross_granted_cents,
                    COALESCE(rv.reversed_cents, 0) AS reversed_cents
                FROM donations d
                LEFT JOIN (
                    SELECT donation_id, SUM(amount_cents) AS refunded_cents
                    FROM refunds
                    GROUP BY donation_id
                ) rf ON rf.donation_id = d.id
                LEFT JOIN (
                    SELECT donation_id, SUM(amount_cents) AS granted_cents
                    FROM grant_items
                    GROUP BY donation_id
                ) g ON g.donation_id = d.id
                LEFT JOIN (
                    SELECT donation_id, SUM(amount_cents) AS reversed_cents
                    FROM reversals
                    GROUP BY donation_id
                ) rv ON rv.donation_id = d.id
                WHERE d.campaign_id = ?
                ORDER BY d.id ASC
                """,
                (campaign_id,),
            ).fetchall()
        )

    @classmethod
    def _provisional_view(
        cls, conn: sqlite3.Connection, campaign_id: str
    ) -> dict[str, Any]:
        campaign = cls._get_campaign(conn, campaign_id)
        rows = cls._donation_rows(conn, campaign_id)

        donor_cap = int(campaign["donor_match_cap_cents"])
        pool_limit = int(campaign["total_pool_limit_cents"])

        # Effective actual allocations = immutable gross grants minus separate
        # actual reversals.  These are deducted before any provisional order.
        donor_net_used: dict[str, int] = {}
        pool_net_granted = 0
        prepared: list[dict[str, Any]] = []

        for row in rows:
            amount = int(row["amount_cents"])
            refunded = int(row["refunded_cents"])
            gross_granted = int(row["gross_granted_cents"])
            reversed_amount = int(row["reversed_cents"])
            remaining = amount - refunded
            net_granted = gross_granted - reversed_amount
            ungranted_balance = max(0, remaining - net_granted)

            pool_net_granted += net_granted
            donor_net_used[row["donor_id"]] = (
                donor_net_used.get(row["donor_id"], 0) + net_granted
            )
            prepared.append(
                {
                    "donation_pk": row["id"],
                    "campaign_id": row["campaign_id"],
                    "source_event_id": row["source_event_id"],
                    "donor_id": row["donor_id"],
                    "project_id": row["project_id"],
                    "donation_amount_cents": amount,
                    "refunded_cents": refunded,
                    "remaining_donation_cents": remaining,
                    "gross_granted_cents": gross_granted,
                    "reversed_cents": reversed_amount,
                    "net_granted_cents": net_granted,
                    "ungranted_balance_cents": ungranted_balance,
                    "note": row["note"],
                    "created_at": row["created_at"],
                }
            )

        pool_actual_remaining = pool_limit - pool_net_granted
        donor_provisional: dict[str, int] = {
            donor: 0 for donor in donor_net_used
        }
        pool_provisional = 0
        items: list[dict[str, Any]] = []

        for accepted_order, item in enumerate(prepared, start=1):
            donor_id = item["donor_id"]
            donor_actual_remaining = donor_cap - donor_net_used.get(donor_id, 0)
            donor_available = (
                donor_actual_remaining - donor_provisional.get(donor_id, 0)
            )
            pool_available = pool_actual_remaining - pool_provisional

            provisional = max(
                0,
                min(
                    item["ungranted_balance_cents"],
                    donor_available,
                    pool_available,
                ),
            )

            donor_provisional[donor_id] = (
                donor_provisional.get(donor_id, 0) + provisional
            )
            pool_provisional += provisional

            waitlisted = item["ungranted_balance_cents"] - provisional
            items.append(
                {
                    "accepted_order": accepted_order,
                    **item,
                    "provisional_match_cents": provisional,
                    "waitlisted_cents": waitlisted,
                    "donor_cap_available_before_order_cents": max(
                        0, donor_available
                    ),
                    "pool_available_before_order_cents": max(
                        0, pool_available
                    ),
                }
            )

        project_map: dict[str, dict[str, Any]] = {}
        for item in items:
            pid = item["project_id"]
            agg = project_map.setdefault(
                pid,
                {
                    "project_id": pid,
                    "donation_order_count": 0,
                    "donation_amount_cents": 0,
                    "remaining_donation_cents": 0,
                    "gross_granted_cents": 0,
                    "reversed_cents": 0,
                    "net_granted_cents": 0,
                    "ungranted_balance_cents": 0,
                    "provisional_match_cents": 0,
                    "waitlisted_cents": 0,
                },
            )
            agg["donation_order_count"] += 1
            for key in (
                "donation_amount_cents",
                "remaining_donation_cents",
                "gross_granted_cents",
                "reversed_cents",
                "net_granted_cents",
                "ungranted_balance_cents",
                "provisional_match_cents",
                "waitlisted_cents",
            ):
                agg[key] += item[key]

        donor_ids = {row["donor_id"] for row in rows}
        donors = []
        for donor_id in sorted(donor_ids):
            net_used = donor_net_used.get(donor_id, 0)
            provisional = donor_provisional.get(donor_id, 0)
            donors.append(
                {
                    "donor_id": donor_id,
                    "donor_match_cap_cents": donor_cap,
                    "net_granted_cents": net_used,
                    "actual_cap_remaining_cents": donor_cap - net_used,
                    "provisional_match_cents": provisional,
                    "cap_remaining_after_provisional_cents": donor_cap
                    - net_used
                    - provisional,
                }
            )

        return {
            "campaign_id": campaign_id,
            "currency": "CNY_CENTS",
            "allocation_order": "donation_acceptance_order_ascending",
            "pool": {
                "total_pool_limit_cents": pool_limit,
                "gross_granted_cents": sum(
                    i["gross_granted_cents"] for i in items
                ),
                "reversed_cents": sum(i["reversed_cents"] for i in items),
                "net_granted_cents": pool_net_granted,
                "actual_pool_remaining_cents": pool_actual_remaining,
                "provisional_match_cents": pool_provisional,
                "unallocated_after_provisional_cents": pool_actual_remaining
                - pool_provisional,
            },
            "donors": donors,
            "projects": sorted(
                project_map.values(), key=lambda x: x["project_id"]
            ),
            "items": items,
        }

    @staticmethod
    def _assert_invariants(
        conn: sqlite3.Connection, campaign_id: str
    ) -> None:
        """Defense-in-depth check executed before every write commit."""
        campaign = conn.execute(
            "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
        ).fetchone()
        if campaign is None:
            return

        bad_refund = conn.execute(
            """
            SELECT d.id, d.amount_cents,
                   COALESCE(SUM(r.amount_cents), 0) AS refunded
            FROM donations d
            LEFT JOIN refunds r ON r.donation_id = d.id
            WHERE d.campaign_id = ?
            GROUP BY d.id
            HAVING refunded > d.amount_cents
            """,
            (campaign_id,),
        ).fetchone()
        if bad_refund is not None:
            raise LedgerError(
                "invariant_refund_over_donation",
                "Refund total exceeds the original donation.",
                status_code=500,
                details=dict(bad_refund),
            )

        view = LedgerService._provisional_view(conn, campaign_id)
        if view["pool"]["net_granted_cents"] > view["pool"]["total_pool_limit_cents"]:
            raise LedgerError(
                "invariant_pool_overdraw",
                "Effective grants exceed the campaign pool.",
                status_code=500,
            )
        for donor in view["donors"]:
            if donor["net_granted_cents"] > donor["donor_match_cap_cents"]:
                raise LedgerError(
                    "invariant_donor_cap_overdraw",
                    "Effective grants exceed a donor cap.",
                    status_code=500,
                    details={"donor_id": donor["donor_id"]},
                )
        for item in view["items"]:
            if item["net_granted_cents"] > item["remaining_donation_cents"]:
                raise LedgerError(
                    "invariant_grant_exceeds_remaining_donation",
                    "Net actual grant exceeds the donation balance after refund.",
                    status_code=500,
                    details={
                        "donation": item["source_event_id"],
                        "net_granted": item["net_granted_cents"],
                        "remaining": item["remaining_donation_cents"],
                    },
                )

    # ------------------------------------------------------------------
    # Campaign/project setup
    # ------------------------------------------------------------------
    def create_campaign(
        self,
        *,
        campaign_id: str,
        name: str,
        total_pool_limit_cents: int,
        donor_match_cap_cents: int,
    ) -> CommandResult:
        with write_transaction(self.db_path) as conn:
            existing = conn.execute(
                "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
            ).fetchone()
            if existing is not None:
                same = (
                    existing["name"] == name
                    and int(existing["total_pool_limit_cents"])
                    == total_pool_limit_cents
                    and int(existing["donor_match_cap_cents"])
                    == donor_match_cap_cents
                )
                if not same:
                    raise LedgerError(
                        "campaign_id_conflict",
                        "campaign_id already exists with different parameters.",
                    )
                body = dict(existing)
                return CommandResult(200, body, True)

            conn.execute(
                """
                INSERT INTO campaigns (
                    id, name, total_pool_limit_cents, donor_match_cap_cents
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    campaign_id,
                    name,
                    total_pool_limit_cents,
                    donor_match_cap_cents,
                ),
            )
            row = self._get_campaign(conn, campaign_id)
            return CommandResult(201, dict(row), False)

    def create_project(
        self,
        campaign_id: str,
        *,
        project_id: str,
        name: str,
        is_ready: bool = True,
    ) -> CommandResult:
        with write_transaction(self.db_path) as conn:
            self._get_campaign(conn, campaign_id)
            existing = conn.execute(
                """
                SELECT * FROM projects
                WHERE campaign_id = ? AND id = ?
                """,
                (campaign_id, project_id),
            ).fetchone()
            if existing is not None:
                if existing["name"] != name or bool(existing["is_ready"]) != is_ready:
                    raise LedgerError(
                        "project_id_conflict",
                        "project_id already exists with different parameters.",
                    )
                return CommandResult(200, dict(existing), True)

            conn.execute(
                """
                INSERT INTO projects (campaign_id, id, name, is_ready)
                VALUES (?, ?, ?, ?)
                """,
                (campaign_id, project_id, name, 1 if is_ready else 0),
            )
            row = self._get_project(conn, campaign_id, project_id)
            body = dict(row)
            body["is_ready"] = bool(body["is_ready"])
            return CommandResult(201, body, False)

    def set_project_ready(
        self, campaign_id: str, project_id: str, is_ready: bool
    ) -> dict[str, Any]:
        with write_transaction(self.db_path) as conn:
            self._get_project(conn, campaign_id, project_id)
            conn.execute(
                "UPDATE projects SET is_ready = ? "
                "WHERE campaign_id = ? AND id = ?",
                (1 if is_ready else 0, campaign_id, project_id),
            )
            row = self._get_project(conn, campaign_id, project_id)
            body = dict(row)
            body["is_ready"] = bool(body["is_ready"])
            return body

    # ------------------------------------------------------------------
    # Donations
    # ------------------------------------------------------------------
    def create_donation(
        self,
        campaign_id: str,
        *,
        source_event_id: str,
        donor_id: str,
        project_id: str,
        amount_cents: int,
        note: str | None = None,
    ) -> CommandResult:
        payload = {
            "operation": "donation",
            "campaign_id": campaign_id,
            "source_event_id": source_event_id,
            "donor_id": donor_id,
            "project_id": project_id,
            "amount_cents": amount_cents,
            "note": note,
        }
        with write_transaction(self.db_path) as conn:
            replay = self._idempotent_replay(
                conn, campaign_id, "donation", source_event_id, payload
            )
            if replay is not None:
                return replay

            self._get_campaign(conn, campaign_id)
            self._get_project(conn, campaign_id, project_id)

            cursor = conn.execute(
                """
                INSERT INTO donations (
                    campaign_id, source_event_id, donor_id, project_id,
                    amount_cents, note
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign_id,
                    source_event_id,
                    donor_id,
                    project_id,
                    amount_cents,
                    note,
                ),
            )
            donation_pk = cursor.lastrowid
            row = conn.execute(
                "SELECT * FROM donations WHERE id = ?", (donation_pk,)
            ).fetchone()
            body = dict(row)
            body.update(
                {
                    "refunded_cents": 0,
                    "remaining_donation_cents": amount_cents,
                    "gross_granted_cents": 0,
                    "reversed_cents": 0,
                    "net_granted_cents": 0,
                }
            )
            self._save_idempotency(
                conn,
                campaign_id=campaign_id,
                operation="donation",
                source_event_id=source_event_id,
                payload=payload,
                status_code=201,
                body=body,
                resource_type="donation",
                resource_id=donation_pk,
            )
            return CommandResult(201, body, False)

    # ------------------------------------------------------------------
    # Refunds and order-level corrections
    # ------------------------------------------------------------------
    def create_refund(
        self,
        campaign_id: str,
        *,
        source_event_id: str,
        donation_source_event_id: str,
        amount_cents: int,
        reason: str | None = None,
    ) -> CommandResult:
        payload = {
            "operation": "refund",
            "campaign_id": campaign_id,
            "source_event_id": source_event_id,
            "donation_source_event_id": donation_source_event_id,
            "amount_cents": amount_cents,
            "reason": reason,
        }
        with write_transaction(self.db_path) as conn:
            replay = self._idempotent_replay(
                conn, campaign_id, "refund", source_event_id, payload
            )
            if replay is not None:
                return replay

            self._get_campaign(conn, campaign_id)
            donation = self._get_donation_by_source(
                conn, campaign_id, donation_source_event_id
            )
            donation_pk = int(donation["id"])
            original_amount = int(donation["amount_cents"])

            refunded_before = int(
                conn.execute(
                    """
                    SELECT COALESCE(SUM(amount_cents), 0) AS total
                    FROM refunds WHERE donation_id = ?
                    """,
                    (donation_pk,),
                ).fetchone()["total"]
            )
            if refunded_before + amount_cents > original_amount:
                raise LedgerError(
                    "refund_exceeds_donation",
                    "Cumulative refunds would exceed the original donation.",
                    status_code=409,
                    details={
                        "donation_source_event_id": donation_source_event_id,
                        "original_amount_cents": original_amount,
                        "already_refunded_cents": refunded_before,
                        "requested_refund_cents": amount_cents,
                        "maximum_refund_cents": original_amount
                        - refunded_before,
                    },
                )

            cursor = conn.execute(
                """
                INSERT INTO refunds (
                    campaign_id, source_event_id, donation_id,
                    amount_cents, reason
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    campaign_id,
                    source_event_id,
                    donation_pk,
                    amount_cents,
                    reason,
                ),
            )
            refund_pk = cursor.lastrowid

            granted_before = int(
                conn.execute(
                    """
                    SELECT COALESCE(SUM(amount_cents), 0) AS total
                    FROM grant_items WHERE donation_id = ?
                    """,
                    (donation_pk,),
                ).fetchone()["total"]
            )
            reversed_before = int(
                conn.execute(
                    """
                    SELECT COALESCE(SUM(amount_cents), 0) AS total
                    FROM reversals WHERE donation_id = ?
                    """,
                    (donation_pk,),
                ).fetchone()["total"]
            )
            net_granted_before = granted_before - reversed_before
            remaining_after_refund = (
                original_amount - refunded_before - amount_cents
            )
            reversal_amount = max(
                0, net_granted_before - remaining_after_refund
            )

            reversal_body: dict[str, Any] | None = None
            if reversal_amount > 0:
                rev_cursor = conn.execute(
                    """
                    INSERT INTO reversals (
                        campaign_id, donation_id, refund_id,
                        amount_cents, reason
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        campaign_id,
                        donation_pk,
                        refund_pk,
                        reversal_amount,
                        (
                            "Automatic order-level correction because "
                            "remaining donation no longer supports net grants"
                            + (f": {reason}" if reason else "")
                        ),
                    ),
                )
                reversal_pk = rev_cursor.lastrowid
                rev_row = conn.execute(
                    "SELECT * FROM reversals WHERE id = ?", (reversal_pk,)
                ).fetchone()
                reversal_body = dict(rev_row)

            self._assert_invariants(conn, campaign_id)

            body = {
                "id": refund_pk,
                "campaign_id": campaign_id,
                "source_event_id": source_event_id,
                "donation_id": donation_pk,
                "donation_source_event_id": donation_source_event_id,
                "amount_cents": amount_cents,
                "reason": reason,
                "cumulative_refunded_cents": refunded_before + amount_cents,
                "remaining_donation_cents": remaining_after_refund,
                "net_granted_before_refund_cents": net_granted_before,
                "actual_reversal": reversal_body,
                "created_at": _now(),
            }
            self._save_idempotency(
                conn,
                campaign_id=campaign_id,
                operation="refund",
                source_event_id=source_event_id,
                payload=payload,
                status_code=201,
                body=body,
                resource_type="refund",
                resource_id=refund_pk,
            )
            return CommandResult(201, body, False)

    # ------------------------------------------------------------------
    # Project settlement / realization
    # ------------------------------------------------------------------
    def settle_project(
        self,
        campaign_id: str,
        project_id: str,
        *,
        source_event_id: str,
    ) -> CommandResult:
        payload = {
            "operation": "settlement",
            "campaign_id": campaign_id,
            "project_id": project_id,
            "source_event_id": source_event_id,
        }
        with write_transaction(self.db_path) as conn:
            replay = self._idempotent_replay(
                conn, campaign_id, "settlement", source_event_id, payload
            )
            if replay is not None:
                return replay

            self._get_campaign(conn, campaign_id)
            project = self._get_project(conn, campaign_id, project_id)
            if not project["is_ready"]:
                raise LedgerError(
                    "project_not_ready",
                    f"Project {project_id!r} is not marked ready for settlement.",
                    status_code=409,
                )

            view = self._provisional_view(conn, campaign_id)
            selected = [
                item
                for item in view["items"]
                if item["project_id"] == project_id
                and item["provisional_match_cents"] > 0
            ]
            total = sum(i["provisional_match_cents"] for i in selected)

            cursor = conn.execute(
                """
                INSERT INTO settlements (
                    campaign_id, project_id, source_event_id,
                    total_amount_cents, donation_count, status
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign_id,
                    project_id,
                    source_event_id,
                    total,
                    len(selected),
                    "settled" if total > 0 else "no_eligible_match",
                ),
            )
            settlement_pk = cursor.lastrowid

            items: list[dict[str, Any]] = []
            for item in selected:
                amount = int(item["provisional_match_cents"])
                grant_cursor = conn.execute(
                    """
                    INSERT INTO grant_items (
                        settlement_id, campaign_id, project_id,
                        donation_id, amount_cents
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        settlement_pk,
                        campaign_id,
                        project_id,
                        item["donation_pk"],
                        amount,
                    ),
                )
                grant_pk = grant_cursor.lastrowid
                items.append(
                    {
                        "id": grant_pk,
                        "settlement_id": settlement_pk,
                        "donation_id": item["donation_pk"],
                        "donation_source_event_id": item["source_event_id"],
                        "accepted_order": item["accepted_order"],
                        "amount_cents": amount,
                    }
                )

            self._assert_invariants(conn, campaign_id)
            row = conn.execute(
                "SELECT * FROM settlements WHERE id = ?", (settlement_pk,)
            ).fetchone()
            body = dict(row)
            body["items"] = items
            if total == 0:
                body["message"] = (
                    "No positive provisional match was eligible at settlement time."
                )

            self._save_idempotency(
                conn,
                campaign_id=campaign_id,
                operation="settlement",
                source_event_id=source_event_id,
                payload=payload,
                status_code=201 if total > 0 else 200,
                body=body,
                resource_type="settlement",
                resource_id=settlement_pk,
            )
            return CommandResult(
                201 if total > 0 else 200, body, False
            )

    # ------------------------------------------------------------------
    # Read models
    # ------------------------------------------------------------------
    def get_campaign(self, campaign_id: str) -> dict[str, Any]:
        with read_conn(self.db_path) as conn:
            row = self._get_campaign(conn, campaign_id)
            return dict(row)

    def list_projects(self, campaign_id: str) -> list[dict[str, Any]]:
        with read_conn(self.db_path) as conn:
            self._get_campaign(conn, campaign_id)
            rows = conn.execute(
                """
                SELECT * FROM projects
                WHERE campaign_id = ?
                ORDER BY id ASC
                """,
                (campaign_id,),
            ).fetchall()
            result = [dict(r) for r in rows]
            for r in result:
                r["is_ready"] = bool(r["is_ready"])
            return result

    def provisional(self, campaign_id: str) -> dict[str, Any]:
        with read_conn(self.db_path) as conn:
            return self._provisional_view(conn, campaign_id)

    def list_donations(self, campaign_id: str) -> list[dict[str, Any]]:
        with read_conn(self.db_path) as conn:
            self._get_campaign(conn, campaign_id)
            rows = self._donation_rows(conn, campaign_id)
            result = []
            for order, row in enumerate(rows, start=1):
                item = dict(row)
                item["accepted_order"] = order
                item["remaining_donation_cents"] = (
                    item["amount_cents"] - item["refunded_cents"]
                )
                item["net_granted_cents"] = (
                    item["gross_granted_cents"] - item["reversed_cents"]
                )
                result.append(item)
            return result

    def list_project_grants(
        self, campaign_id: str, project_id: str
    ) -> dict[str, Any]:
        with read_conn(self.db_path) as conn:
            self._get_campaign(conn, campaign_id)
            self._get_project(conn, campaign_id, project_id)
            settlements = conn.execute(
                """
                SELECT * FROM settlements
                WHERE campaign_id = ? AND project_id = ?
                  AND status = 'settled' AND total_amount_cents > 0
                ORDER BY id ASC
                """,
                (campaign_id, project_id),
            ).fetchall()

            result_settlements = []
            for s in settlements:
                items = conn.execute(
                    """
                    SELECT
                        gi.id,
                        gi.settlement_id,
                        gi.donation_id,
                        gi.amount_cents,
                        gi.created_at,
                        d.source_event_id AS donation_source_event_id,
                        d.donor_id
                    FROM grant_items gi
                    JOIN donations d ON d.id = gi.donation_id
                    WHERE gi.settlement_id = ?
                    ORDER BY gi.id ASC
                    """,
                    (s["id"],),
                ).fetchall()
                batch = dict(s)
                batch["items"] = [dict(i) for i in items]
                result_settlements.append(batch)

            total_granted = sum(
                int(s["total_amount_cents"]) for s in result_settlements
            )
            return {
                "campaign_id": campaign_id,
                "project_id": project_id,
                "grant_batch_count": len(result_settlements),
                "gross_granted_cents": total_granted,
                "settlements": result_settlements,
            }

    def list_reversals(
        self,
        campaign_id: str,
        donation_source_event_id: str | None = None,
    ) -> dict[str, Any]:
        with read_conn(self.db_path) as conn:
            self._get_campaign(conn, campaign_id)
            params: list[Any] = [campaign_id]
            where_extra = ""
            donation_summary = None
            if donation_source_event_id is not None:
                donation = self._get_donation_by_source(
                    conn, campaign_id, donation_source_event_id
                )
                where_extra = "AND rv.donation_id = ?"
                params.append(donation["id"])
                donation_summary = self._donation_summary(
                    conn, int(donation["id"])
                )

            rows = conn.execute(
                f"""
                SELECT
                    rv.id,
                    rv.campaign_id,
                    rv.donation_id,
                    rv.refund_id,
                    rv.amount_cents,
                    rv.reason,
                    rv.created_at,
                    d.source_event_id AS donation_source_event_id,
                    d.project_id,
                    d.donor_id,
                    rf.source_event_id AS refund_source_event_id
                FROM reversals rv
                JOIN donations d ON d.id = rv.donation_id
                JOIN refunds rf ON rf.id = rv.refund_id
                WHERE rv.campaign_id = ?
                {where_extra}
                ORDER BY rv.id ASC
                """,
                params,
            ).fetchall()
            reversals = [dict(r) for r in rows]
            return {
                "campaign_id": campaign_id,
                "donation": donation_summary,
                "reversal_count": len(reversals),
                "total_reversed_cents": sum(
                    r["amount_cents"] for r in reversals
                ),
                "reversals": reversals,
            }

    @staticmethod
    def _donation_summary(
        conn: sqlite3.Connection, donation_pk: int
    ) -> dict[str, Any]:
        row = conn.execute(
            """
            SELECT
                d.*,
                COALESCE(rf.refunded_cents, 0) AS refunded_cents,
                COALESCE(g.granted_cents, 0) AS gross_granted_cents,
                COALESCE(rv.reversed_cents, 0) AS reversed_cents
            FROM donations d
            LEFT JOIN (
                SELECT donation_id, SUM(amount_cents) AS refunded_cents
                FROM refunds WHERE donation_id = ?
            ) rf ON rf.donation_id = d.id
            LEFT JOIN (
                SELECT donation_id, SUM(amount_cents) AS granted_cents
                FROM grant_items WHERE donation_id = ?
            ) g ON g.donation_id = d.id
            LEFT JOIN (
                SELECT donation_id, SUM(amount_cents) AS reversed_cents
                FROM reversals WHERE donation_id = ?
            ) rv ON rv.donation_id = d.id
            WHERE d.id = ?
            """,
            (donation_pk, donation_pk, donation_pk, donation_pk),
        ).fetchone()
        result = dict(row)
        result["remaining_donation_cents"] = (
            result["amount_cents"] - result["refunded_cents"]
        )
        result["net_granted_cents"] = (
            result["gross_granted_cents"] - result["reversed_cents"]
        )
        return result
