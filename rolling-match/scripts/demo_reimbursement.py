#!/usr/bin/env python3
"""Reproducible joint-reimbursement scenario for the rolling-match backend.

Run with only the Python standard library + SQLite:

    python3 scripts/demo_reimbursement.py

Reuses the same ``LedgerService`` for campaigns/projects/donations/settlements/
refunds and the new ``ReimbursementService`` for joint vendor-invoice payment.
A standalone simulated bank database (``demo_reimbursement.db.bank.db``) models
the provider; no real bank is contacted.

The script demonstrates, in order:

1. several projects realize actual grant money (provisional match is not
   spendable);
2. an invoice registered with payee + vendor number + positive project shares;
3. exact retry returns the original invoice, changed shares are rejected;
4. each project consents separately; only a fully consented invoice queues;
5. registration order: an invoice that does not fit is skipped as a whole while
   a later one succeeds;
6. finance submits only a currently payable invoice; the hold is visible;
7. a refund during flight creates a project gap but never moves the hold;
8. the simulated bank accepts but loses the response; a retry/recovery returns
   the same instruction and the bank result is synced;
9. a definitive failure releases the hold and the invoice requeues;
10. a second attempt succeeds and keeps the per-project allocation detail.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.bank import SimulatedBank  # noqa: E402
from app.db import init_db  # noqa: E402
from app.reimbursement import ReimbursementService  # noqa: E402
from app.service import LedgerError, LedgerService  # noqa: E402

DB_PATH = ROOT / "demo_reimbursement.db"
BANK_PATH = ROOT / "demo_reimbursement.db.bank.db"
for path in (DB_PATH, BANK_PATH):
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)

init_db(DB_PATH)
SimulatedBank(BANK_PATH)
ledger = LedgerService(str(DB_PATH))
reimb = ReimbursementService(str(DB_PATH), str(BANK_PATH))


def cents(value: int) -> str:
    return f"{value}¢"


def show_funds(title: str) -> None:
    print(f"\n== {title} ==")
    print(
        "project   realized  paid  in-flight  reserved  available  gap"
    )
    for p in reimb.project_funds("c").get("projects", []):
        print(
            f"{p['project_id']:<9} "
            f"{p['realized_net_grant_cents']:>8} "
            f"{p['paid_cents']:>5} "
            f"{p['in_flight_cents']:>10} "
            f"{p['reserved_cents']:>9} "
            f"{p['available_cents']:>10} "
            f"{p['gap_cents']:>4}"
        )


def show_invoices(title: str) -> None:
    print(f"\n== {title} ==")
    print("order vendor_invoice payable state    reason        shortfall")
    for inv in reimb.invoice_overview("c")["invoices"]:
        print(
            f"{inv['registration_order']:>5} "
            f"{inv['vendor_invoice_number']:<14} "
            f"{str(inv['payable']):<7} "
            f"{inv['state']:<8} "
            f"{str(inv['not_payable_reason']):<13} "
            f"{inv['shortfall_cents_by_project']}"
        )


def expect_conflict(fn, code: str) -> LedgerError:
    try:
        fn()
    except LedgerError as exc:
        assert exc.code == code, (exc.code, exc.message)
        return exc
    raise AssertionError(f"Expected conflict {code}")


def register(number: str, shares: list[dict[str, Any]], *, source: str):
    return reimb.register_invoice(
        "c",
        source_event_id=source,
        payee="Print Shop Ltd",
        vendor_invoice_number=number,
        shares=shares,
    )


def consent(invoice_id: int, project_id: str, *, source: str) -> None:
    reimb.set_invoice_consent(
        "c",
        invoice_id,
        source_event_id=source,
        project_id=project_id,
        consented=True,
    )


def main() -> None:
    ledger.create_campaign(
        campaign_id="c",
        name="Joint invoice demo",
        total_pool_limit_cents=1_000_000,
        donor_match_cap_cents=1_000_000,
    )
    realized = {"docs": 500, "kernel": 400, "translation": 300}
    for project_id, amount in realized.items():
        ledger.create_project("c", project_id=project_id, name=project_id)
        ledger.create_donation(
            "c",
            source_event_id=f"don-{project_id}",
            donor_id=f"donor-{project_id}",
            project_id=project_id,
            amount_cents=amount,
        )
        ledger.settle_project(
            "c", project_id, source_event_id=f"set-{project_id}"
        )

    # A fifth project has only provisional match and therefore zero spendable
    # reimbursement money.
    ledger.create_project("c", project_id="website", name="website")
    ledger.create_donation(
        "c",
        source_event_id="don-website",
        donor_id="donor-website",
        project_id="website",
        amount_cents=900,
    )
    show_funds("1. Realized actual grants become reimbursement capacity")

    # --- Registration -------------------------------------------------
    inv1 = register(
        "INV-1001",
        [
            {"project_id": "docs", "share_cents": 300},
            {"project_id": "kernel", "share_cents": 200},
        ],
        source="reg-1",
    )
    assert inv1.status_code == 201
    invoice1 = inv1.body
    print(
        "\n2. Registered INV-1001: total is the sum of shares = "
        f"{cents(invoice1['total_amount_cents'])}, state="
        f"{invoice1['state']}, payable={invoice1['payable']}"
    )

    replay = register(
        "INV-1001",
        [
            {"project_id": "docs", "share_cents": 300},
            {"project_id": "kernel", "share_cents": 200},
        ],
        source="reg-1",
    )
    assert replay.replayed is True and replay.body["id"] == invoice1["id"]
    print(
        "3. Exact retry of reg-1 returns the ORIGINAL invoice "
        f"#{invoice1['id']} (replayed=true), not a second one."
    )
    conflict = expect_conflict(
        lambda: register(
            "INV-1001",
            [{"project_id": "docs", "share_cents": 350}],
            source="reg-1-changed",
        ),
        "invoice_registration_conflict",
    )
    assert conflict.status_code == 409
    print(
        "4. Re-registering the same campaign+payee+number with different "
        "shares is rejected with 409; shares cannot be edited by retry."
    )

    # --- Consent ------------------------------------------------------
    consent(invoice1["id"], "docs", source="consent-docs")
    view = reimb.get_invoice("c", invoice1["id"])
    assert view["payable"] is False
    print(
        "\n5. Only docs has consented; INV-1001 is still not payable "
        f"(missing={view['missing_consent_projects']})."
    )
    consent(invoice1["id"], "kernel", source="consent-kernel")
    view = reimb.get_invoice("c", invoice1["id"])
    assert view["payable"] is True
    show_funds("6. Fully consented INV-1001 temporarily reserves both shares")

    # Invoice that does not fit in registration order, plus a later fitting one.
    inv2 = register(
        "INV-1002",
        [{"project_id": "docs", "share_cents": 250}],
        source="reg-2",
    ).body
    inv3 = register(
        "INV-1003",
        [{"project_id": "translation", "share_cents": 250}],
        source="reg-3",
    ).body
    consent(inv2["id"], "docs", source="consent-2")
    consent(inv3["id"], "translation", source="consent-3")
    show_invoices(
        "7. INV-1002 needs 250¢ but docs only has 200¢ left, so it is "
        "skipped whole; INV-1003 still reserves translation"
    )
    assert reimb.get_invoice("c", inv2["id"])["payable"] is False
    assert (
        reimb.get_invoice("c", inv2["id"])["shortfall_cents_by_project"]
        == {"docs": 50}
    )
    assert reimb.get_invoice("c", inv3["id"])["payable"] is True

    # --- Submit and in-flight refund ---------------------------------
    submitted = reimb.submit_payment(
        "c", invoice1["id"], source_event_id="pay-1"
    )
    number = submitted.body["instruction_number"]
    print(
        "\n8. Finance submits INV-1001 as one instruction "
        f"{number} for {cents(submitted.body['amount_cents'])}."
    )
    show_funds("9. Both project shares are now held in flight")

    # Refund docs enough to leave less realized money than the hold.
    refund = ledger.create_refund(
        "c",
        source_event_id="refund-docs",
        donation_source_event_id="don-docs",
        amount_cents=250,
    )
    assert refund.body["actual_reversal"]["amount_cents"] == 250
    show_funds(
        "10. Refund reverses 250¢ of docs realized money; the in-flight hold "
        "stays at 300¢ and docs shows a 50¢ gap"
    )
    fund = {
        p["project_id"]: p
        for p in reimb.project_funds("c")["projects"]
    }["docs"]
    assert fund["realized_net_grant_cents"] == 250
    assert fund["gap_cents"] == 50
    instruction = reimb.get_payment_instruction("c", number)
    assert instruction["amount_cents"] == invoice1["total_amount_cents"]
    assert instruction["status"] == "pending"
    print(
        "   The instruction amount and the 300¢ hold were not changed by the "
        "refund; the shortfall is shown against docs."
    )

    # A later grant fills the gap first, only the excess becomes available.
    ledger.create_donation(
        "c",
        source_event_id="don-docs-topup",
        donor_id="donor-docs",
        project_id="docs",
        amount_cents=100,
    )
    ledger.settle_project(
        "c", "docs", source_event_id="set-docs-topup"
    )
    show_funds(
        "10b. A new 100¢ actual grant first fills the 50¢ gap; the other 50¢ "
        "becomes available headroom"
    )
    fund = {
        p["project_id"]: p
        for p in reimb.project_funds("c")["projects"]
    }["docs"]
    assert fund["gap_cents"] == 0
    assert fund["available_cents"] == 50

    # --- Lost response and restart -----------------------------------
    reimb.bank.set_lose_next_submit_response(True)
    # A *new* invoice is used so the demonstration shows a fresh submission.
    inv4 = register(
        "INV-1004",
        [{"project_id": "kernel", "share_cents": 200}],
        source="reg-4",
    ).body
    consent(inv4["id"], "kernel", source="consent-4")
    lost = reimb.submit_payment(
        "c", inv4["id"], source_event_id="pay-lost"
    )
    lost_number = lost.body["instruction_number"]
    assert reimb.bank.get(lost_number)["status"] == "pending"
    print(
        "\n11. Simulated bank accepts INV-1004 as "
        f"{lost_number} but the response is lost. The ledger instruction "
        "stays pending and is recoverable."
    )

    restarted = ReimbursementService(str(DB_PATH), str(BANK_PATH))
    recovered = restarted.get_payment_instruction("c", lost_number)
    assert recovered["bank"]["status"] == "pending"
    retry = restarted.submit_payment(
        "c", inv4["id"], source_event_id="pay-lost"
    )
    assert retry.body["instruction_number"] == lost_number
    assert retry.replayed is True
    print(
        "12. After an app restart the same instruction is recovered, and the "
        "old submit retry returns that instruction instead of paying again."
    )
    restarted.bank.confirm(
        lost_number, outcome="succeeded", result_event_id="bank-lost-ok"
    )
    synced = restarted.sync_payment_result("c", lost_number)
    assert synced["status"] == "succeeded"
    print(
        "13. The bank later confirms success; sync pulls the explicit result "
        "into the ledger without re-submitting anything."
    )

    # --- Failure, requeue, success -----------------------------------
    reimb.deliver_payment_result(
        "c", number, outcome="failed", result_event_id="bank-declined"
    )
    show_funds("14. INV-1001 payment failed: its holds are released")
    view = reimb.get_invoice("c", invoice1["id"])
    assert view["state"] == "unpaid"
    print(
        "15. INV-1001 requeues with its still-valid consents: payable="
        f"{view['payable']}, attempt count="
        f"{len(view['payment_instructions'])}."
    )

    # Pay it now with a second attempt.
    second = reimb.submit_payment(
        "c", invoice1["id"], source_event_id="pay-2"
    )
    second_number = second.body["instruction_number"]
    reimb.deliver_payment_result(
        "c", second_number, outcome="succeeded", result_event_id="bank-ok"
    )
    show_funds("16. Second attempt succeeded; paid amounts are kept per project")

    final = reimb.get_invoice("c", invoice1["id"])
    succeeded = reimb.get_payment_instruction("c", second_number)
    print(
        f"\n17. INV-1001 state={final['state']}; allocations on "
        f"{second_number}: {succeeded['allocations']}"
    )

    # Contradictory result cannot overwrite the confirmed success.
    try:
        reimb.deliver_payment_result(
            "c", second_number, outcome="failed", result_event_id="late-fail"
        )
    except LedgerError as exc:
        assert exc.code == "payment_result_conflict"
        print(
            "18. A late contradictory result is rejected (409 "
            f"{exc.code}); the confirmed payment stays."
        )
    else:
        raise AssertionError("expected contradictory result to be rejected")

    print(
        "\nPASS: joint reimbursement demo completed. Inspect "
        f"{DB_PATH} and {BANK_PATH} with sqlite3 if desired.\n"
    )


if __name__ == "__main__":
    main()
