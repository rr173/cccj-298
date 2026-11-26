#!/usr/bin/env python3
"""Reproducible periodic-closing scenario for the rolling-match backend.

Run with only the Python standard library + SQLite:

    python3 scripts/demo_closing.py

The script rebuilds ``demo_closing.db`` (and its independent
``demo_closing.db.bank.db``) each run.  It shows, in order:

1. period 1 closes *while a joint vendor payment is still in flight*; the
   statement carries it as a hold and the first signed statement is captured;
2. business keeps running while a draft awaits sign-off;
3. the bank later confirms the joint payment, and the ledger syncs it;
4. period 2 expenses that payment exactly once, with its opening basis linked
   back to the first signed statement;
5. the first signed statement is byte-for-byte unchanged, proving the expense
   was not counted twice and the opening balance carried forward correctly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.closing import ClosingService  # noqa: E402
from app.db import init_db  # noqa: E402
from app.reimbursement import ReimbursementService  # noqa: E402
from app.service import LedgerService  # noqa: E402

DB_PATH = ROOT / "demo_closing.db"
for suffix in ("", "-wal", "-shm"):
    Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
    Path(str(DB_PATH) + ".bank.db" + suffix).unlink(missing_ok=True)

init_db(DB_PATH)
ledger = LedgerService(str(DB_PATH))
reimb = ReimbursementService(str(DB_PATH))
closing = ClosingService(str(DB_PATH))


def cents(value: int) -> str:
    return f"{value}¢"


def show_projects(title: str, statement: dict) -> None:
    print(f"\n== {title} ==")
    print(
        "project   opening  +grants  -reversal  -bank-paid  =closing  "
        "hold  available  gap"
    )
    for p in statement["projects"]:
        print(
            f"{p['project_id']:<9} "
            f"{p['opening_book_balance_cents']:>7} "
            f"{p['period_grants_cents']:>7} "
            f"{p['period_reversals_cents']:>9} "
            f"{p['bank_confirmed_reimbursement_cents']:>10} "
            f"{p['closing_book_balance_cents']:>8} "
            f"{p['outstanding_in_flight_cents']:>4} "
            f"{p['available_after_in_flight_cents']:>9} "
            f"{p['gap_after_in_flight_cents']:>3}"
        )


def show_holds(statement: dict) -> None:
    holds = statement["payments"]["outstanding_in_flight"]
    if not holds:
        print("   (no outstanding in-flight instructions)")
        return
    for hold in holds:
        state = hold["hold_state"]
        print(
            f"   hold {hold['instruction_number']} "
            f"{cents(hold['total_amount_cents'])} "
            f"state={state} bank_status={hold['bank_status']} "
            f"shares={hold['shares']}"
        )


def show_paid(statement: dict) -> None:
    paid = statement["payments"]["bank_confirmed_in_period"]
    if not paid:
        print("   (no bank-confirmed expense in this period)")
        return
    for payment in paid:
        print(
            f"   expensed {payment['instruction_number']} "
            f"{cents(payment['total_amount_cents'])} "
            f"expensed_by_bank_first={payment['expensed_by_bank_first']} "
            f"ledger_status_at_cutoff={payment['ledger_status_at_cutoff']}"
        )
        if payment["prior_in_flight"]:
            print(
                "     carried from closing "
                f"#{payment['prior_in_flight'][0]['closing_id']} "
                f"(period {payment['prior_in_flight'][0]['period_no']})"
            )


def main() -> None:
    ledger.create_campaign(
        campaign_id="c",
        name="Periodic closing demo",
        total_pool_limit_cents=1_000_000,
        donor_match_cap_cents=1_000_000,
    )
    for project_id, amount in (("docs", 500), ("kernel", 400)):
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

    invoice = reimb.register_invoice(
        "c",
        source_event_id="inv-1",
        payee="Print Shop Ltd",
        vendor_invoice_number="INV-1001",
        shares=[
            {"project_id": "docs", "share_cents": 300},
            {"project_id": "kernel", "share_cents": 200},
        ],
    ).body
    for share in invoice["shares"]:
        reimb.set_invoice_consent(
            "c",
            invoice["id"],
            source_event_id=f"consent-{invoice['id']}-{share['project_id']}",
            project_id=share["project_id"],
            consented=True,
        )
    number = reimb.submit_payment(
        "c", invoice["id"], source_event_id="pay-1"
    ).body["instruction_number"]
    print(
        "1. Joint invoice INV-1001 is submitted as one instruction "
        f"{number} for {cents(invoice['total_amount_cents'])}; it is now "
        "in flight."
    )

    # ---- Period 1: close with the instruction still in flight ----
    first = closing.create_draft("c", source_event_id="close-1").body
    print(
        "\n2. Period 1 draft at a fixed cutoff "
        f"(period_no={first['period_no']}, signable={first['signable']})."
    )
    show_projects("3. Period 1 balances with the joint payment outstanding", first)
    show_holds(first)
    assert first["payments"]["bank_confirmed_in_period"] == []
    assert first["signable"] is True

    signed_first = closing.sign_closing(
        "c", first["id"], source_event_id="sign-1"
    ).body
    frozen_totals = json.dumps(signed_first["totals"], sort_keys=True)
    print(
        "\n4. Finance signs period 1. Its frozen totals are "
        f"{frozen_totals}."
    )

    # Business continues while drafts await sign-off.
    ledger.create_refund(
        "c",
        source_event_id="refund-docs",
        donation_source_event_id="don-docs",
        amount_cents=50,
    )
    ledger.create_donation(
        "c",
        source_event_id="don-kernel-topup",
        donor_id="donor-kernel-2",
        project_id="kernel",
        amount_cents=100,
    )
    ledger.settle_project(
        "c", "kernel", source_event_id="set-kernel-topup"
    )
    print(
        "\n5. After signing, docs gets a 50¢ reversal and kernel gets a new "
        "100¢ actual grant. These belong to period 2 only."
    )

    # Bank confirms the joint payment after the period-1 cutoff, while the
    # business ledger has NOT yet synced the receipt.
    reimb.bank.confirm(
        number, outcome="succeeded", result_event_id="bank-ok"
    )
    assert (
        reimb.get_payment_instruction("c", number)["status"] == "pending"
    )
    print(
        "\n6. The bank confirms success for the joint payment; the business "
        "ledger is still unsynced. The bank result is authoritative."
    )

    # ---- Period 2: the same instruction is this period's expense ----
    second = closing.create_draft("c", source_event_id="close-2").body
    print(
        "\n7. Period 2 draft, opening basis = the signed period-1 statement "
        f"#{second['opening_basis_closing_id']} "
        f"(period_no={second['opening_basis_period_no']})."
    )
    show_projects("8. Period 2 balances (joint payment expensed once here)", second)
    show_paid(second)
    by_project = {p["project_id"]: p for p in second["projects"]}
    assert by_project["docs"]["opening_book_balance_cents"] == 500
    assert by_project["docs"]["period_reversals_cents"] == 50
    assert by_project["docs"]["bank_confirmed_reimbursement_cents"] == 300
    assert by_project["docs"]["closing_book_balance_cents"] == 150
    assert by_project["kernel"]["period_grants_cents"] == 100
    assert by_project["kernel"]["bank_confirmed_reimbursement_cents"] == 200
    assert by_project["kernel"]["closing_book_balance_cents"] == 300
    assert second["payments"]["outstanding_in_flight"] == []
    paid_entry = second["payments"]["bank_confirmed_in_period"][0]
    assert paid_entry["ledger_status_at_cutoff"] == "pending"
    assert paid_entry["expensed_by_bank_first"] is True

    # The ledger syncs the (already bank-confirmed) result afterwards.  It must
    # not create a second expense, because the instruction is already counted.
    reimb.sync_payment_result("c", number)
    print(
        "\n9. The ledger later syncs the same instruction; because the bank "
        "already confirmed it in period 2, a later sync adds no second expense."
    )

    signed_second = closing.sign_closing(
        "c", second["id"], source_event_id="sign-2"
    ).body

    # ---- Period 3: the expense must not reappear ----
    third = closing.create_draft("c", source_event_id="close-3").body
    assert third["payments"]["bank_confirmed_in_period"] == []
    by_project = {p["project_id"]: p for p in third["projects"]}
    assert by_project["docs"]["opening_book_balance_cents"] == 150
    assert by_project["kernel"]["opening_book_balance_cents"] == 300
    print(
        "\n10. Period 3 draft opens from the period-2 signed balances "
        f"(docs={cents(150)}, kernel={cents(300)}) and lists no expense again."
    )

    stored_first = closing.get_closing("c", first["id"])
    assert json.dumps(stored_first["totals"], sort_keys=True) == frozen_totals
    assert stored_first["payments"]["bank_confirmed_in_period"] == []
    assert stored_first["payments"]["outstanding_in_flight"][0][
        "instruction_number"
    ] == number
    assert signed_second["status"] == "signed"
    print(
        "\n11. The first signed statement is unchanged: its totals still equal "
        "the frozen snapshot and the joint payment is still recorded there as "
        "an outstanding hold, exactly as reviewed."
    )

    print(
        "\nPASS: periodic closing demo completed. Inspect "
        f"{DB_PATH} and {DB_PATH}.bank.db with sqlite3 if desired.\n"
    )


if __name__ == "__main__":
    main()
