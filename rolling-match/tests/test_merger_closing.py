from __future__ import annotations

import json

from app.closing import ClosingService
from app.db import init_db
from app.merger import MergerService
from app.reimbursement import ReimbursementService
from app.service import LedgerService


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
        ClosingService(str(db_path)),
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


def _consent_all(reimb, invoice):
    for share in invoice["shares"]:
        reimb.set_invoice_consent(
            "c",
            invoice["id"],
            source_event_id=f"k-{invoice['id']}-{share['project_id']}",
            project_id=share["project_id"],
            consented=True,
        )


def test_merger_transfer_lines_appear_in_effective_period_only(tmp_path):
    _db, ledger, reimb, merger, closing = _setup(tmp_path, "m-close-1")
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    created = merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )

    draft = closing.create_draft("c", source_event_id="close-1").body
    by_project = {p["project_id"]: p for p in draft["projects"]}
    assert by_project["a"]["period_transfer_out_cents"] == 1000
    assert by_project["a"]["period_transfer_in_cents"] == 0
    assert by_project["b"]["period_transfer_in_cents"] == 1000
    assert by_project["b"]["period_transfer_out_cents"] == 0
    # Each project's closing book includes its transfer line.
    assert by_project["a"]["closing_book_balance_cents"] == 0
    assert by_project["b"]["closing_book_balance_cents"] == 2000
    # Transfer-in and transfer-out net to zero campaign-wide.
    assert (
        draft["totals"]["period_transfer_in_cents"]
        == draft["totals"]["period_transfer_out_cents"]
        == 1000
    )
    assert draft["totals"]["closing_book_balance_cents"] == 2000

    # The statement names the merger record and both directions.
    effective = draft["mergers"]["effective_in_period"]
    assert len(effective) == 1
    assert effective[0]["merger_id"] == created.body["id"]
    assert effective[0]["source_project_id"] == "a"
    assert effective[0]["successor_project_id"] == "b"
    assert effective[0]["transferred_book_balance_cents"] == 1000
    directions = {
        (e["direction"], e["amount_cents"])
        for p in draft["projects"]
        for e in p["basis"]["internal_transfers"]
    }
    assert directions == {("out", 1000), ("in", 1000)}

    signed = closing.sign_closing(
        "c", draft["id"], source_event_id="sign-1"
    ).body
    # The next period carries opening balances and counts no transfer again.
    second = closing.create_draft("c", source_event_id="close-2").body
    bp = {p["project_id"]: p for p in second["projects"]}
    assert bp["a"]["opening_book_balance_cents"] == 0
    assert bp["b"]["opening_book_balance_cents"] == 2000
    assert bp["a"]["period_transfer_out_cents"] == 0
    assert bp["b"]["period_transfer_in_cents"] == 0
    assert second["mergers"]["effective_in_period"] == []

    # The signed statement stays byte-identical afterwards.
    stored = closing.get_closing("c", draft["id"])
    assert stored["content_hash"] == signed["content_hash"]


def test_in_flight_payment_crossing_merger_settles_against_successor(tmp_path):
    _db, ledger, reimb, merger, closing = _setup(tmp_path, "m-close-2")
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
    _consent_all(reimb, invoice)
    number = reimb.submit_payment(
        "c", invoice["id"], source_event_id="pay"
    ).body["instruction_number"]

    # Period 1 closes while the payment is in flight.
    first = closing.create_draft("c", source_event_id="close-1").body
    closing.sign_closing("c", first["id"], source_event_id="sign-1")

    # The merger happens after period 1 while the instruction is still
    # pending; the hold and the instruction move responsibility to b.
    merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )
    # The bank then confirms success.  The whole invoice (1000) is now borne
    # by the successor, including a's original 600 share.
    reimb.deliver_payment_result(
        "c", number, outcome="succeeded", result_event_id="ok"
    )

    second = closing.create_draft("c", source_event_id="close-2").body
    by_project = {p["project_id"]: p for p in second["projects"]}
    # Opening balances come from period 1 (a=1000, b=1000) before the merger.
    assert by_project["a"]["opening_book_balance_cents"] == 1000
    assert by_project["b"]["opening_book_balance_cents"] == 1000
    # The transfer appears in period 2.
    assert by_project["a"]["period_transfer_out_cents"] == 1000
    assert by_project["b"]["period_transfer_in_cents"] == 1000
    # The full 1000 expense is attributed to b (responsible at bank success).
    assert by_project["a"]["bank_confirmed_reimbursement_cents"] == 0
    assert by_project["b"]["bank_confirmed_reimbursement_cents"] == 1000
    # a ends at zero; b = 1000 opening + 1000 transfer - 1000 spend = 1000.
    assert by_project["a"]["closing_book_balance_cents"] == 0
    assert by_project["b"]["closing_book_balance_cents"] == 1000
    # The expense is counted once and the period-1 statement is unchanged.
    paid = second["payments"]["bank_confirmed_in_period"]
    assert [p["instruction_number"] for p in paid] == [number]
    stored_first = closing.get_closing("c", first["id"])
    assert stored_first["payments"]["bank_confirmed_in_period"] == []
    assert len(stored_first["payments"]["outstanding_in_flight"]) == 1


def test_bank_success_before_merger_is_expensed_to_original_project(tmp_path):
    """Bank-first attribution: a success confirmed before the merger belongs
    to the original project even though the ledger syncs it afterwards."""
    _db, ledger, reimb, merger, closing = _setup(tmp_path, "m-close-3")
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    invoice = reimb.register_invoice(
        "c",
        source_event_id="inv",
        payee="Vendor",
        vendor_invoice_number="V-1",
        shares=[{"project_id": "a", "share_cents": 600}],
    ).body
    _consent_all(reimb, invoice)
    number = reimb.submit_payment(
        "c", invoice["id"], source_event_id="pay"
    ).body["instruction_number"]
    # Bank confirms first; the ledger has not synced when the merger runs.
    reimb.bank.confirm(number, outcome="succeeded", result_event_id="bank-ok")
    created = merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )
    # The transferred book already nets the bank-confirmed 600.
    assert created.body["transferred_book_balance_cents"] == 400
    reimb.sync_payment_result("c", number)

    draft = closing.create_draft("c", source_event_id="close-1").body
    by_project = {p["project_id"]: p for p in draft["projects"]}
    # The expense is attributed to a (responsible at bank confirmation), the
    # remaining 400 moved to b through the transfer.
    assert by_project["a"]["bank_confirmed_reimbursement_cents"] == 600
    assert by_project["a"]["period_transfer_out_cents"] == 400
    assert by_project["a"]["closing_book_balance_cents"] == 0
    assert by_project["b"]["bank_confirmed_reimbursement_cents"] == 0
    assert by_project["b"]["period_transfer_in_cents"] == 400
    assert by_project["b"]["closing_book_balance_cents"] == 1400
    # Money is conserved campaign-wide.
    assert draft["totals"]["closing_book_balance_cents"] == 1400
    assert draft["signable"] is True


def test_draft_taken_before_merger_is_not_retroactively_changed(tmp_path):
    _db, ledger, reimb, merger, closing = _setup(tmp_path, "m-close-4")
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    draft = closing.create_draft("c", source_event_id="close-1").body
    frozen = json.dumps(draft["totals"], sort_keys=True)

    # A merger happens while the draft awaits sign-off.
    merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )
    reviewed = closing.get_closing("c", draft["id"])
    # The reviewed content is the frozen pre-merger snapshot.
    assert json.dumps(reviewed["totals"], sort_keys=True) == frozen
    assert reviewed["mergers"]["effective_in_period"] == []
    signed = closing.sign_closing(
        "c", draft["id"], source_event_id="sign-1"
    ).body
    assert signed["content_hash"] == draft["content_hash"]


def test_reversal_after_merger_follows_successor_in_closing(tmp_path):
    _db, ledger, reimb, merger, closing = _setup(tmp_path, "m-close-5")
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )
    # Refund the old donation; the reversal is borne by the successor.
    ledger.create_refund(
        "c",
        source_event_id="refund-a",
        donation_source_event_id="don-a",
        amount_cents=300,
    )
    draft = closing.create_draft("c", source_event_id="close-1").body
    by_project = {p["project_id"]: p for p in draft["projects"]}
    assert by_project["a"]["period_reversals_cents"] == 0
    assert by_project["b"]["period_reversals_cents"] == 300
    basis = by_project["b"]["basis"]["reversals"][0]
    assert basis["original_project_id"] == "a"
    assert basis["responsible_project_id"] == "b"
    # b: 1000 own + 1000 transfer - 300 reversal = 1700; a zeroes out.
    assert by_project["b"]["closing_book_balance_cents"] == 1700
    assert by_project["a"]["closing_book_balance_cents"] == 0
