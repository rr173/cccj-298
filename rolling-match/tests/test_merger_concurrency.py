from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from app.db import init_db
from app.merger import MergerService
from app.reimbursement import ReimbursementService
from app.service import LedgerError, LedgerService


def _setup(tmp_path, name: str):
    db_path = tmp_path / f"{name}.db"
    init_db(db_path)
    ledger = LedgerService(str(db_path))
    ledger.create_campaign(
        campaign_id="c",
        name="C",
        total_pool_limit_cents=1_000_000,
        donor_match_cap_cents=1_000_000,
    )
    return (
        str(db_path),
        ledger,
        ReimbursementService(str(db_path)),
        MergerService(str(db_path)),
    )


def _realize(ledger: LedgerService, project_id: str, amount: int) -> None:
    ledger.create_project("c", project_id=project_id, name=project_id.upper())
    ledger.create_donation(
        "c",
        source_event_id=f"don-{project_id}",
        donor_id=f"donor-{project_id}",
        project_id=project_id,
        amount_cents=amount,
    )
    ledger.settle_project("c", project_id, source_event_id=f"set-{project_id}")


def test_concurrent_identical_merger_applies_once(tmp_path):
    _db, ledger, _reimb, merger = _setup(tmp_path, "conc-merge-same")
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    barrier = threading.Barrier(12)

    def merge():
        barrier.wait()
        try:
            return ("ok", merger.merge_projects(
                "c",
                source_event_id="same-merge",
                source_project_id="a",
                successor_project_id="b",
            ))
        except LedgerError as exc:
            return ("error", exc.code)

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(lambda _: merge(), range(12)))

    ok = [o[1] for o in outcomes if o[0] == "ok"]
    assert len(ok) == 12
    assert {r.body["id"] for r in ok} == {ok[0].body["id"]}
    assert sum(1 for r in ok if not r.replayed) == 1
    with sqlite3.connect(merger.db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM project_mergers").fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM internal_transfer_entries"
            ).fetchone()[0]
            == 2
        )
        status = conn.execute(
            "SELECT status FROM projects WHERE id='a'"
        ).fetchone()[0]
    assert status == "merged"


def test_concurrent_merger_and_payment_never_double_spend_or_lose_hold(
    tmp_path,
):
    """A merger interleaved with invoice payment submission keeps one clear
    ordering: either the payment reserves before the merger (then the
    successor assumes the hold) or after it (then the successor's balance
    pays).  The same money is never spendable twice and the hold is never
    lost."""
    db_path, ledger, reimb, merger = _setup(tmp_path, "conc-merge-pay")
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    invoice = reimb.register_invoice(
        "c",
        source_event_id="inv",
        payee="Vendor",
        vendor_invoice_number="V-1",
        shares=[{"project_id": "a", "share_cents": 600}],
    ).body
    reimb.set_invoice_consent(
        "c",
        invoice["id"],
        source_event_id="consent-a",
        project_id="a",
        consented=True,
    )
    barrier = threading.Barrier(2)

    def worker(i: int):
        barrier.wait()
        if i == 0:
            try:
                return (
                    "merge",
                    merger.merge_projects(
                        "c",
                        source_event_id="merge-1",
                        source_project_id="a",
                        successor_project_id="b",
                    ),
                )
            except LedgerError as exc:
                return ("merge-error", exc.code)
        try:
            return (
                "pay",
                reimb.submit_payment(
                    "c", invoice["id"], source_event_id="pay-1"
                ),
            )
        except LedgerError as exc:
            return ("pay-error", exc.code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, range(2)))

    kinds = {kind for kind, _ in results}
    assert "merge" in kinds

    with sqlite3.connect(db_path) as conn:
        merger_count = conn.execute(
            "SELECT COUNT(*) FROM project_mergers"
        ).fetchone()[0]
        assert merger_count == 1
        # The share always ends pointing at the current responsible project.
        responsible = conn.execute(
            "SELECT responsible_project_id FROM invoice_shares"
        ).fetchone()[0]
        assert responsible == "b"
        pending = conn.execute(
            "SELECT COUNT(*) FROM payment_instructions WHERE status='pending'"
        ).fetchone()[0]

    funds = {
        p["project_id"]: p
        for p in reimb.project_funds("c")["projects"]
    }
    if "pay" in kinds:
        # The payment was accepted exactly once; the successor holds it.
        assert pending == 1
        assert funds["b"]["in_flight_cents"] == 600
        # The hold is not also left on the merged project.
        assert funds["a"]["in_flight_cents"] == 0
        # b's available money already excludes the single 600 hold.
        assert funds["b"]["available_cents"] == (
            funds["b"]["realized_net_grant_cents"] - 600
        )
    else:
        # The merger committed first and invalidated the stale consent, so the
        # payment was refused rather than silently reserved against a project
        # that no longer owns the share.
        assert pending == 0
        view = reimb.get_invoice("c", invoice["id"])
        assert view["not_payable_reason"] == "consent_pending"


def test_concurrent_refund_and_merger_attribute_reversal_once(tmp_path):
    db_path, ledger, _reimb, merger = _setup(tmp_path, "conc-merge-refund")
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    barrier = threading.Barrier(2)

    def worker(i: int):
        barrier.wait()
        if i == 0:
            try:
                return (
                    "merge",
                    merger.merge_projects(
                        "c",
                        source_event_id="merge-1",
                        source_project_id="a",
                        successor_project_id="b",
                    ),
                )
            except LedgerError as exc:
                return ("merge-error", exc.code)
        try:
            return (
                "refund",
                ledger.create_refund(
                    "c",
                    source_event_id="refund-1",
                    donation_source_event_id="don-a",
                    amount_cents=400,
                ),
            )
        except LedgerError as exc:
            return ("refund-error", exc.code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, range(2)))

    with sqlite3.connect(db_path) as conn:
        # Exactly one reversal row exists and it is attributed to one project.
        rows = conn.execute(
            """
            SELECT rv.responsible_project_id, rv.amount_cents
            FROM reversals rv
            """
        ).fetchall()
    assert len(rows) == 1
    responsible, amount = rows[0]
    assert amount == 400
    # Whichever committed first, the money consequence is consistent: a
    # reversal against a's donation is never recorded twice.
    assert responsible in {"a", "b"}
    # And the transfer plus reversal still conserve the campaign's book total.
    with sqlite3.connect(db_path) as conn:
        total_grant = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM grant_items"
        ).fetchone()[0]
        total_reversal = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM reversals"
        ).fetchone()[0]
        transfer_net = conn.execute(
            """
            SELECT COALESCE(SUM(
                CASE WHEN direction='in' THEN amount_cents
                     ELSE -amount_cents END),0)
            FROM internal_transfer_entries
            """
        ).fetchone()[0]
    assert transfer_net == 0
    assert total_grant - total_reversal == 2000 - 400
