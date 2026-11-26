#!/usr/bin/env python3
"""Reproducible project-handover (merger) scenario for the rolling-match backend.

Run with only the Python standard library + SQLite:

    python3 scripts/demo_merger.py

The script rebuilds ``demo_merger.db`` (and its independent
``demo_merger.db.bank.db``) each run, then walks the whole handover story:

1. two projects ``docs`` and ``kernel`` realize actual matching money;
2. a joint vendor invoice is registered and a payment is submitted, so the
   source project ``docs`` hands over while a payment is still in flight;
3. finance merges ``docs`` into ``kernel`` in one irreversible, idempotent
   step: the source's book balance (actual spend deducted, in-flight hold not
   deducted) is booked out and the same amount is booked into the successor;
4. the successor immediately uses the combined balance for another invoice,
   proving the same money is spendable exactly once;
5. the in-flight payment keeps its original instruction, then succeeds and is
   settled on the books against the successor (including the docs share);
6. an old docs donation is refunded: the order still refunds by its original
   slip while the resulting reversal is borne by the current successor;
7. a failed later attempt shows the transferred share's old consent is void
   and the successor must re-confirm before resending;
8. a periodic closing shows the transfer-out / transfer-in lines referencing
   the merger record, plus how the payment settles in the books.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.closing import ClosingService  # noqa: E402
from app.db import init_db  # noqa: E402
from app.merger import MergerService  # noqa: E402
from app.reimbursement import ReimbursementService  # noqa: E402
from app.service import LedgerError, LedgerService  # noqa: E402

DB_PATH = ROOT / "demo_merger.db"
BANK_PATH = ROOT / "demo_merger.db.bank.db"
for path in (DB_PATH, BANK_PATH):
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)

init_db(DB_PATH)
ledger = LedgerService(str(DB_PATH))
reimb = ReimbursementService(str(DB_PATH), str(BANK_PATH))
merger = MergerService(str(DB_PATH), str(BANK_PATH))
closing = ClosingService(str(DB_PATH), str(BANK_PATH))

CAMPAIGN = "oss-2026"


def cents(value: int) -> str:
    return f"{value}¢"


def show_funds(title: str) -> None:
    print(f"\n== {title} ==")
    print(
        "project  status  realized  book  paid  in-flight  reserved  "
        "available  gap"
    )
    for p in reimb.project_funds(CAMPAIGN)["projects"]:
        print(
            f"{p['project_id']:<8} "
            f"{p['status']:<7} "
            f"{p['realized_net_grant_cents']:>8} "
            f"{p['book_balance_cents']:>5} "
            f"{p['paid_cents']:>5} "
            f"{p['in_flight_cents']:>10} "
            f"{p['reserved_cents']:>9} "
            f"{p['available_cents']:>10} "
            f"{p['gap_cents']:>4}"
        )


def realize(project_id: str, amount: int) -> None:
    ledger.create_project(
        CAMPAIGN, project_id=project_id, name=project_id.upper()
    )
    ledger.create_donation(
        CAMPAIGN,
        source_event_id=f"don-{project_id}",
        donor_id=f"donor-{project_id}",
        project_id=project_id,
        amount_cents=amount,
    )
    ledger.settle_project(
        CAMPAIGN, project_id, source_event_id=f"set-{project_id}"
    )


def main() -> None:
    ledger.create_campaign(
        campaign_id=CAMPAIGN,
        name="Project handover demo",
        total_pool_limit_cents=1_000_000,
        donor_match_cap_cents=1_000_000,
    )
    # 1. Both projects realize actual matching money.
    realize("docs", 1000)
    realize("kernel", 1000)
    print(
        "1. docs and kernel each realize 1000¢ of actual matching money "
        "(provisional match alone is never spendable)."
    )
    show_funds("Starting funds")

    # 2. A joint invoice with a docs share that goes in flight before handover.
    handover_invoice = reimb.register_invoice(
        CAMPAIGN,
        source_event_id="inv-handover",
        payee="Print Shop Ltd",
        vendor_invoice_number="INV-HANDOVER",
        shares=[
            {"project_id": "docs", "share_cents": 600},
            {"project_id": "kernel", "share_cents": 200},
        ],
    ).body
    for share in handover_invoice["shares"]:
        reimb.set_invoice_consent(
            CAMPAIGN,
            handover_invoice["id"],
            source_event_id=(
                f"consent-{handover_invoice['id']}-{share['project_id']}"
            ),
            project_id=share["project_id"],
            consented=True,
        )

    # A second, not-yet-submitted invoice carried only by docs.  Its consent
    # exists before the merger and must be voided for the transferred share.
    queued_invoice = reimb.register_invoice(
        CAMPAIGN,
        source_event_id="inv-docs-queued",
        payee="Docs Tooling Co",
        vendor_invoice_number="INV-DOCS-QUEUED",
        shares=[{"project_id": "docs", "share_cents": 150}],
    ).body
    reimb.set_invoice_consent(
        CAMPAIGN,
        queued_invoice["id"],
        source_event_id="consent-docs-queued",
        project_id="docs",
        consented=True,
    )

    handover_number = reimb.submit_payment(
        CAMPAIGN,
        handover_invoice["id"],
        source_event_id="pay-handover",
    ).body["instruction_number"]
    print(
        "\n2. Joint invoice INV-HANDOVER (800¢: docs 600¢ + kernel 200¢) is "
        f"submitted as {handover_number}; it is still in flight at handover. "
        "A second 150¢ docs-only invoice is registered and consented but not "
        "yet submitted."
    )
    show_funds("Funds with the in-flight hold")

    # 3. The irreversible merger while the payment is pending.
    merged = merger.merge_projects(
        CAMPAIGN,
        source_event_id="merge-docs-into-kernel",
        source_project_id="docs",
        successor_project_id="kernel",
    ).body
    print(
        "\n3. Finance merges docs into kernel while the payment is pending. "
        f"Merger #{merged['id']} is effective immediately and irreversible."
    )
    for entry in merged["internal_transfer_entries"]:
        print(
            f"   internal transfer {entry['direction']:<3} "
            f"{entry['project_id']:<7} {cents(entry['amount_cents'])} "
            f"(merger #{entry['merger_id']})"
        )
    print(
        f"   transferred book balance: "
        f"{cents(merged['transferred_book_balance_cents'])} "
        "(actual spend deducted, in-flight hold NOT deducted)"
    )
    print(
        "   transferred invoice shares: "
        + ", ".join(
            f"{s['original_project_id']} -> {s['responsible_project_id']} "
            f"{cents(s['share_cents'])} (invoice #{s['invoice_id']})"
            for s in merged["transferred_invoice_shares"]
        )
    )
    show_funds("Funds immediately after the merger")

    # The not-yet-submitted docs invoice lost its consent for the transferred
    # share and cannot queue until the successor re-confirms.
    queued_view = reimb.get_invoice(CAMPAIGN, queued_invoice["id"])
    print(
        "\n4a. The docs-only queued invoice no longer queues: docs' old "
        f"consent is void (payable={queued_view['payable']}, reason="
        f"{queued_view['not_payable_reason']!r}); the share now reads "
        f"{queued_view['shares'][0]['project_id']} -> "
        f"{queued_view['shares'][0]['responsible_project_id']}."
    )
    reimb.set_invoice_consent(
        CAMPAIGN,
        queued_invoice["id"],
        source_event_id="reconfirm-queued-kernel",
        project_id="kernel",
        consented=True,
    )
    queued_view = reimb.get_invoice(CAMPAIGN, queued_invoice["id"])
    print(
        "    kernel re-confirms the transferred share and the invoice queues "
        f"again (payable={queued_view['payable']})."
    )

    # Idempotency: same source event and content returns the original result.
    replay = merger.merge_projects(
        CAMPAIGN,
        source_event_id="merge-docs-into-kernel",
        source_project_id="docs",
        successor_project_id="kernel",
    )
    assert replay.replayed and replay.body["id"] == merged["id"]
    print(
        "\n4. Repeating the same merger source event returns the original "
        "record (idempotent); changing parameters is rejected."
    )
    try:
        merger.merge_projects(
            CAMPAIGN,
            source_event_id="merge-docs-into-kernel",
            source_project_id="docs",
            successor_project_id="kernel-other",
        )
    except LedgerError as exc:
        print(f"   changed-parameter retry -> 409 {exc.code}")

    # docs can no longer receive new donations or take new invoice shares.
    try:
        ledger.create_donation(
            CAMPAIGN,
            source_event_id="late-docs-donation",
            donor_id="late-donor",
            project_id="docs",
            amount_cents=10,
        )
    except LedgerError as exc:
        print(
            f"   new donation to merged docs -> 409 {exc.code}; new invoice "
            "shares are refused the same way."
        )

    # 5. The successor spends the combined balance on a new invoice.  The two
    #    shares docs used to bear are now judged against kernel's one balance.
    successor_invoice = reimb.register_invoice(
        CAMPAIGN,
        source_event_id="inv-successor",
        payee="Cloud Hosting Inc",
        vendor_invoice_number="INV-KERNEL-NEW",
        shares=[{"project_id": "kernel", "share_cents": 900}],
    ).body
    reimb.set_invoice_consent(
        CAMPAIGN,
        successor_invoice["id"],
        source_event_id="consent-successor-kernel",
        project_id="kernel",
        consented=True,
    )
    successor_view = reimb.get_invoice(
        CAMPAIGN, successor_invoice["id"]
    )
    assert successor_view["payable"] is True
    print(
        "\n5. kernel registers a new 900¢ invoice. It is payable from the "
        "combined balance: its own 1000¢ plus the 1000¢ transferred from "
        "docs, with the 800¢ in-flight hold already reserved."
    )
    successor_number = reimb.submit_payment(
        CAMPAIGN,
        successor_invoice["id"],
        source_event_id="pay-successor",
    ).body["instruction_number"]
    print(f"   submitted as {successor_number} (a second, separate payment).")
    show_funds("Funds while both payments are in flight")

    # 6. The original in-flight payment keeps its instruction and then succeeds.
    assert handover_number == reimb.get_payment_instruction(
        CAMPAIGN, handover_number
    )["instruction_number"]
    handover_result = reimb.deliver_payment_result(
        CAMPAIGN,
        handover_number,
        outcome="succeeded",
        result_event_id="bank-handover-ok",
    ).body
    print(
        f"\n6. The original instruction {handover_number} is never changed or "
        "re-sent. On bank success it settles against kernel, including the "
        "600¢ share docs used to bear:"
    )
    for allocation in handover_result["allocations"]:
        print(
            f"   original {allocation['project_id']:<7} now responsible "
            f"{allocation['responsible_project_id']:<7} "
            f"{cents(allocation['share_cents'])}"
        )
    reimb.deliver_payment_result(
        CAMPAIGN,
        successor_number,
        outcome="succeeded",
        result_event_id="bank-successor-ok",
    )
    show_funds("Funds after both payments succeed")

    # 7. Refunding an old docs order follows the original slip but the
    #    reversal is borne by the current successor.
    refund = ledger.create_refund(
        CAMPAIGN,
        source_event_id="refund-docs",
        donation_source_event_id="don-docs",
        amount_cents=300,
    ).body
    reversal = refund["actual_reversal"]
    print(
        "\n7. A 300¢ refund against the original docs donation still uses the "
        "original order; the matching rules are unchanged but the reversal is "
        "borne by the successor:"
    )
    print(
        f"   donation {refund['donation_source_event_id']} (original project "
        f"docs) -> reversal {cents(reversal['amount_cents'])} responsible "
        f"project {reversal['responsible_project_id']}"
    )
    show_funds("Funds after the post-merger reversal")

    # 8. A failed attempt on an invoice the successor bears.  Give kernel a
    #    fresh grant first so the invoice is affordable, then show that a bank
    #    failure releases the hold and the invoice requeues under the
    #    successor's responsibility.
    ledger.create_donation(
        CAMPAIGN,
        source_event_id="don-kernel-topup",
        donor_id="donor-kernel-topup",
        project_id="kernel",
        amount_cents=500,
    )
    ledger.settle_project(
        CAMPAIGN,
        "kernel",
        source_event_id="set-kernel-topup",
    )
    reconfirm_invoice = reimb.register_invoice(
        CAMPAIGN,
        source_event_id="inv-reconfirm",
        payee="Design Studio",
        vendor_invoice_number="INV-RECONFIRM-2",
        shares=[{"project_id": "kernel", "share_cents": 200}],
    ).body
    reimb.set_invoice_consent(
        CAMPAIGN,
        reconfirm_invoice["id"],
        source_event_id="consent-reconfirm",
        project_id="kernel",
        consented=True,
    )
    reconfirm_number = reimb.submit_payment(
        CAMPAIGN,
        reconfirm_invoice["id"],
        source_event_id="pay-reconfirm",
    ).body["instruction_number"]
    reimb.deliver_payment_result(
        CAMPAIGN,
        reconfirm_number,
        outcome="failed",
        result_event_id="bank-reconfirm-failed",
    )
    view = reimb.get_invoice(CAMPAIGN, reconfirm_invoice["id"])
    print(
        f"\n8. Payment {reconfirm_number} fails. The hold is released and the "
        f"invoice requeues (state={view['state']}, payable={view['payable']}, "
        f"reason={view['not_payable_reason']!r}). As with the transferred "
        "docs share in step 4a, only the project currently responsible can "
        "confirm a resend."
    )
    # The successor re-confirms, then a fresh attempt uses a new instruction.
    reimb.set_invoice_consent(
        CAMPAIGN,
        reconfirm_invoice["id"],
        source_event_id="reconfirm-after-failure",
        project_id="kernel",
        consented=True,
    )
    resent = reimb.submit_payment(
        CAMPAIGN,
        reconfirm_invoice["id"],
        source_event_id="pay-reconfirm-2",
    ).body
    print(
        f"   after kernel re-confirms, the resend is accepted as "
        f"{resent['instruction_number']} (attempt {resent['attempt_no']})."
    )
    reimb.deliver_payment_result(
        CAMPAIGN,
        resent["instruction_number"],
        outcome="succeeded",
        result_event_id="bank-reconfirm-ok",
    )

    # 9. Responsibility view and the closing statement.
    responsibility = merger.project_responsibility(CAMPAIGN)
    print("\n9. Current responsibility:")
    for project in responsibility["projects"]:
        print(
            f"   {project['project_id']:<7} status={project['status']:<7} "
            f"responsible now: "
            f"{project['current_responsible_project_id']}"
        )

    draft = closing.create_draft(
        CAMPAIGN, source_event_id="close-handover"
    ).body
    print(
        "\n10. Periodic closing statement (the merger is an internal transfer "
        "in this period):"
    )
    print(
        "project  opening  +grants  -reversal  +transfer-in  -transfer-out  "
        "-bank-paid  =closing"
    )
    for p in draft["projects"]:
        print(
            f"{p['project_id']:<8} "
            f"{p['opening_book_balance_cents']:>7} "
            f"{p['period_grants_cents']:>7} "
            f"{p['period_reversals_cents']:>10} "
            f"{p['period_transfer_in_cents']:>13} "
            f"{p['period_transfer_out_cents']:>15} "
            f"{p['bank_confirmed_reimbursement_cents']:>11} "
            f"{p['closing_book_balance_cents']:>9}"
        )
    for record in draft["mergers"]["effective_in_period"]:
        print(
            f"   merger #{record['merger_id']} "
            f"{record['source_project_id']} -> "
            f"{record['successor_project_id']} transferred "
            f"{cents(record['transferred_book_balance_cents'])} "
            f"(source event {record['source_event_id']})"
        )
    signed = closing.sign_closing(
        CAMPAIGN, draft["id"], source_event_id="sign-handover"
    )
    assert signed.body["status"] == "signed"
    assert draft["totals"]["period_transfer_in_cents"] == (
        draft["totals"]["period_transfer_out_cents"]
    )
    print(
        "\nPASS: project handover demo completed. Inspect "
        f"{DB_PATH} and {BANK_PATH} with sqlite3 if desired.\n"
    )


if __name__ == "__main__":
    main()
