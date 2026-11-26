from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from app.db import init_db
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
    return ledger, ReimbursementService(str(db_path))


def _realize(ledger: LedgerService, project_id: str, amount_cents: int) -> None:
    ledger.create_project("c", project_id=project_id, name=project_id.upper())
    ledger.create_donation(
        "c",
        source_event_id=f"don-{project_id}",
        donor_id=f"donor-{project_id}",
        project_id=project_id,
        amount_cents=amount_cents,
    )
    ledger.settle_project("c", project_id, source_event_id=f"set-{project_id}")


def _invoice_two_projects(reimb: ReimbursementService):
    invoice = reimb.register_invoice(
        "c",
        source_event_id="inv",
        payee="Vendor",
        vendor_invoice_number="V-1",
        shares=[
            {"project_id": "a", "share_cents": 600},
            {"project_id": "b", "share_cents": 400},
        ],
    ).body
    for share in invoice["shares"]:
        reimb.set_invoice_consent(
            "c",
            invoice["id"],
            source_event_id=f"consent-{share['project_id']}",
            project_id=share["project_id"],
            consented=True,
        )
    return invoice


def test_concurrent_identical_submission_creates_one_payment(tmp_path):
    ledger, reimb = _setup(tmp_path, "concurrent-submit")
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    invoice = _invoice_two_projects(reimb)

    barrier = threading.Barrier(12)

    def submit():
        barrier.wait()
        try:
            result = reimb.submit_payment(
                "c", invoice["id"], source_event_id="same-pay"
            )
            return ("ok", result)
        except LedgerError as exc:
            return ("error", exc.code)

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(lambda _: submit(), range(12)))

    ok = [o[1] for o in outcomes if o[0] == "ok"]
    assert len(ok) == 12
    assert {r.body["instruction_number"] for r in ok} == {"PAY-1-1"}
    assert sum(1 for r in ok if not r.replayed) == 1

    with sqlite3.connect(ledger.db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM payment_instructions"
            ).fetchone()[0]
            == 1
        )
    # The bank also holds exactly one payment for the instruction number.
    bank_payment = reimb.bank.get("PAY-1-1")
    assert bank_payment is not None
    assert bank_payment["instruction_number"] == "PAY-1-1"
    accepted = [
        e
        for e in reimb.bank.list_events("PAY-1-1")
        if e["event_type"] == "accepted"
    ]
    assert len(accepted) == 1


def test_concurrent_distinct_submissions_pay_only_one(tmp_path):
    ledger, reimb = _setup(tmp_path, "concurrent-distinct")
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    invoice = _invoice_two_projects(reimb)

    barrier = threading.Barrier(10)

    def submit(i: int):
        barrier.wait()
        try:
            return ("ok", reimb.submit_payment(
                "c", invoice["id"], source_event_id=f"pay-{i}"
            ))
        except LedgerError as exc:
            return ("error", exc.code)

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(submit, range(10)))

    successes = [o for o in outcomes if o[0] == "ok"]
    errors = [o for o in outcomes if o[0] == "error"]
    assert len(successes) == 1
    assert all(code == "invoice_payment_in_flight" for _, code in errors)

    with sqlite3.connect(ledger.db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM payment_instructions"
            ).fetchone()[0]
            == 1
        )


def test_concurrent_results_and_refunds_never_debit_partial_projects(
    tmp_path,
):
    ledger, reimb = _setup(tmp_path, "concurrent-result")
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    invoice = _invoice_two_projects(reimb)
    number = reimb.submit_payment(
        "c", invoice["id"], source_event_id="pay"
    ).body["instruction_number"]

    barrier = threading.Barrier(12)

    def worker(i: int):
        barrier.wait()
        if i % 3 == 0:
            # Refund against project a's donation; may create a reversal.
            try:
                ledger.create_refund(
                    "c",
                    source_event_id=f"refund-{i}",
                    donation_source_event_id="don-a",
                    amount_cents=100,
                )
                return "refund"
            except LedgerError:
                return "refund-rejected"
        try:
            reimb.deliver_payment_result(
                "c",
                number,
                outcome="succeeded",
                result_event_id=f"bank-{i}",
            )
            return "result"
        except LedgerError:
            return "result-rejected"

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(worker, range(12)))

    # Every project's paid amount is either 0 or exactly its share: there is
    # never a half-applied invoice.
    funds = {
        p["project_id"]: p for p in reimb.project_funds("c")["projects"]
    }
    assert funds["a"]["paid_cents"] in (0, 600)
    assert funds["b"]["paid_cents"] in (0, 400)
    assert funds["a"]["paid_cents"] / 600 == funds["b"]["paid_cents"] / 400

    instruction = reimb.get_payment_instruction("c", number)
    if instruction["status"] == "succeeded":
        assert funds["a"]["paid_cents"] == 600
        assert funds["b"]["paid_cents"] == 400
    else:
        assert funds["a"]["paid_cents"] == 0
        assert funds["b"]["paid_cents"] == 0


def test_concurrent_consent_and_submission_are_consistent(tmp_path):
    ledger, reimb = _setup(tmp_path, "concurrent-consent")
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    invoice = reimb.register_invoice(
        "c",
        source_event_id="inv",
        payee="Vendor",
        vendor_invoice_number="V-1",
        shares=[
            {"project_id": "a", "share_cents": 600},
            {"project_id": "b", "share_cents": 400},
        ],
    ).body

    barrier = threading.Barrier(8)
    results: list[str] = []
    lock = threading.Lock()

    def worker(i: int):
        barrier.wait()
        if i < 4:
            project_id = "a" if i == 0 else "b"
            if i in (2, 3):
                project_id = "ab"[i - 2]
            try:
                reimb.set_invoice_consent(
                    "c",
                    invoice["id"],
                    source_event_id=f"consent-{i}-{project_id}",
                    project_id=project_id,
                    consented=True,
                )
                outcome = "consent-ok"
            except LedgerError as exc:
                outcome = exc.code
        else:
            try:
                reimb.submit_payment(
                    "c", invoice["id"], source_event_id=f"pay-{i}"
                )
                outcome = "pay-ok"
            except LedgerError as exc:
                outcome = exc.code
        with lock:
            results.append(outcome)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))

    view = reimb.get_invoice("c", invoice["id"])
    if view["state"] == "in_flight":
        # If a payment was accepted, both consents must have been present and
        # fully reserved before the hold.
        assert view["fully_consented"] is True
        funds = {
            p["project_id"]: p
            for p in reimb.project_funds("c")["projects"]
        }
        assert funds["a"]["in_flight_cents"] == 600
        assert funds["b"]["in_flight_cents"] == 400
    with sqlite3.connect(ledger.db_path) as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM payment_instructions WHERE status='pending'"
        ).fetchone()[0]
        assert pending <= 1
