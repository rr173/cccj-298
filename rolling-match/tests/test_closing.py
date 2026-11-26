from __future__ import annotations

import json
import sqlite3

import pytest

from app.closing import ClosingService
from app.db import init_db
from app.reimbursement import ReimbursementService
from app.service import LedgerError, LedgerService


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "closing.db"
    init_db(path)
    return str(path)


@pytest.fixture
def ledger(db_path):
    svc = LedgerService(db_path)
    svc.create_campaign(
        campaign_id="c",
        name="Closing",
        total_pool_limit_cents=1_000_000,
        donor_match_cap_cents=1_000_000,
    )
    return svc


@pytest.fixture
def reimb(db_path):
    return ReimbursementService(db_path)


@pytest.fixture
def closing(db_path):
    return ClosingService(db_path)


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


def _topup(
    ledger: LedgerService, project_id: str, amount_cents: int, tag: str
) -> None:
    ledger.create_donation(
        "c",
        source_event_id=f"don-{project_id}-{tag}",
        donor_id=f"donor-{project_id}-{tag}",
        project_id=project_id,
        amount_cents=amount_cents,
    )
    ledger.settle_project(
        "c", project_id, source_event_id=f"set-{project_id}-{tag}"
    )


def _consent_all(reimb: ReimbursementService, invoice: dict) -> None:
    for share in invoice["shares"]:
        reimb.set_invoice_consent(
            "c",
            invoice["id"],
            source_event_id=f"consent-{invoice['id']}-{share['project_id']}",
            project_id=share["project_id"],
            consented=True,
        )


def _paid_instruction(
    ledger, reimb, project_id: str, amount: int, *, tag: str = ""
) -> str:
    suffix = f"-{tag}" if tag else ""
    _realize(ledger, project_id, amount)
    invoice = reimb.register_invoice(
        "c",
        source_event_id=f"inv-{project_id}-{amount}{suffix}",
        payee="Vendor",
        vendor_invoice_number=f"V-{project_id}-{amount}{suffix}",
        shares=[{"project_id": project_id, "share_cents": amount}],
    ).body
    _consent_all(reimb, invoice)
    number = reimb.submit_payment(
        "c", invoice["id"], source_event_id=f"pay-{project_id}-{amount}{suffix}"
    ).body["instruction_number"]
    reimb.deliver_payment_result(
        "c", number, outcome="succeeded", result_event_id=f"ok-{project_id}{suffix}"
    )
    return number


# ----------------------------------------------------------------------
# Statement content and per-period exclusion
# ----------------------------------------------------------------------
def test_first_period_lists_each_item_and_excludes_provisional(
    ledger, reimb, closing
):
    _realize(ledger, "a", 1000)
    # Provisional-only money must never enter the statement.
    ledger.create_project("c", project_id="p", name="P")
    ledger.create_donation(
        "c",
        source_event_id="don-p",
        donor_id="donor-p",
        project_id="p",
        amount_cents=500,
    )
    # A queued-but-not-submitted invoice reservation is also excluded.
    queued = reimb.register_invoice(
        "c",
        source_event_id="inv-queued",
        payee="Vendor",
        vendor_invoice_number="V-Q",
        shares=[{"project_id": "a", "share_cents": 300}],
    ).body
    _consent_all(reimb, queued)

    draft = closing.create_draft(
        "c", source_event_id="close-1"
    ).body
    assert draft["status"] == "draft"
    assert draft["period_no"] == 1
    assert draft["signable"] is True
    assert draft["blockers"] == []

    by_project = {p["project_id"]: p for p in draft["projects"]}
    a = by_project["a"]
    assert a["opening_book_balance_cents"] == 0
    assert a["period_grants_cents"] == 1000
    assert a["period_reversals_cents"] == 0
    assert a["bank_confirmed_reimbursement_cents"] == 0
    assert a["closing_book_balance_cents"] == 1000
    # The queued invoice is a temporary reservation, not an in-flight payment.
    assert a["outstanding_in_flight_cents"] == 0

    assert by_project["p"]["period_grants_cents"] == 0
    assert by_project["p"]["closing_book_balance_cents"] == 0
    # Provisional match exists but is absent from the statement.
    assert ledger.provisional("c")["projects"][-1][
        "provisional_match_cents"
    ] > 0

    # Each reported amount is backed by a per-item trace.
    assert [g["amount_cents"] for g in a["basis"]["grants"]] == [1000]
    assert a["basis"]["grants"][0]["donation_source_event_id"] == "don-a"


def test_signed_statement_is_immutable_and_next_period_only_new_amounts(
    ledger, reimb, closing
):
    _realize(ledger, "a", 1000)
    _paid_instruction(ledger, reimb, "b", 500)

    first = closing.create_draft("c", source_event_id="close-1").body
    signed = closing.sign_closing(
        "c", first["id"], source_event_id="sign-1"
    )
    assert signed.status_code == 200
    assert signed.body["status"] == "signed"
    signed_hash = signed.body["content_hash"]

    a = {p["project_id"]: p for p in signed.body["projects"]}["a"]
    assert a["closing_book_balance_cents"] == 1000
    b = {p["project_id"]: p for p in signed.body["projects"]}["b"]
    assert b["closing_book_balance_cents"] == 0

    # New events after the signed cutoff.
    _topup(ledger, "a", 200, "second")  # additional period grant
    ledger.create_refund(
        "c",
        source_event_id="refund-a",
        donation_source_event_id="don-a",
        amount_cents=100,
    )
    _paid_instruction(ledger, reimb, "c2", 150, tag="second")

    second = closing.create_draft("c", source_event_id="close-2").body
    assert second["period_no"] == 2
    assert second["opening_basis_closing_id"] == first["id"]
    by_project = {p["project_id"]: p for p in second["projects"]}
    a = by_project["a"]
    # Opening = signed closing balance; only new events count this period.
    assert a["opening_book_balance_cents"] == 1000
    assert a["period_grants_cents"] == 200
    assert a["period_reversals_cents"] == 100
    assert a["closing_book_balance_cents"] == 1000 + 200 - 100
    # The first grant is not counted again.
    assert [g["amount_cents"] for g in a["basis"]["grants"]] == [200]

    # The first signed statement is frozen.
    stored = closing.get_closing("c", first["id"])
    assert stored["status"] == "signed"
    assert stored["content_hash"] == signed_hash
    assert stored["totals"]["closing_book_balance_cents"] == 1000


def test_signing_uses_reviewed_content_not_current_balances(
    ledger, reimb, closing
):
    _realize(ledger, "a", 1000)
    draft = closing.create_draft("c", source_event_id="close-1").body
    # Business continues while the draft awaits sign-off.
    _topup(ledger, "a", 500, "mid")
    ledger.create_refund(
        "c",
        source_event_id="refund",
        donation_source_event_id="don-a",
        amount_cents=300,
    )

    signed = closing.sign_closing(
        "c", draft["id"], source_event_id="sign-1"
    ).body
    a = {p["project_id"]: p for p in signed["projects"]}["a"]
    # Still the exact reviewed snapshot, not the newer balances.
    assert a["closing_book_balance_cents"] == 1000
    assert signed["content_hash"] == draft["content_hash"]


# ----------------------------------------------------------------------
# Bank authority: result before sync, cross-period success
# ----------------------------------------------------------------------
def test_bank_success_before_cutoff_counts_even_if_ledger_not_synced(
    ledger, reimb, closing
):
    _realize(ledger, "a", 1000)
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
    # Bank confirms success, but the ledger is not synced yet.
    reimb.bank.confirm(
        number, outcome="succeeded", result_event_id="bank-ok"
    )
    assert (
        reimb.get_payment_instruction("c", number)["status"] == "pending"
    )

    draft = closing.create_draft("c", source_event_id="close").body
    a = {p["project_id"]: p for p in draft["projects"]}["a"]
    assert a["period_grants_cents"] == 1000
    assert a["bank_confirmed_reimbursement_cents"] == 600
    assert a["closing_book_balance_cents"] == 400
    payment = draft["payments"]["bank_confirmed_in_period"][0]
    assert payment["instruction_number"] == number
    assert payment["expensed_by_bank_first"] is True
    assert draft["signable"] is True


def test_cross_period_instruction_expensed_once_at_bank_success(
    ledger, reimb, closing
):
    """The key demo: a joint payment in flight at the first closing becomes
    next period's expense, and the first signed statement never changes."""
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
    assert reimb.bank.get(number)["status"] == "pending"

    # ---- Period 1 cutoff: instruction is an outstanding hold. ----
    first = closing.create_draft("c", source_event_id="close-1").body
    holds = first["payments"]["outstanding_in_flight"]
    assert [h["instruction_number"] for h in holds] == [number]
    assert holds[0]["hold_state"] == "bank_accepted_pending"
    assert first["payments"]["bank_confirmed_in_period"] == []

    by_project = {p["project_id"]: p for p in first["projects"]}
    a = by_project["a"]
    b = by_project["b"]
    assert a["closing_book_balance_cents"] == 1000
    assert a["outstanding_in_flight_cents"] == 600
    assert a["available_after_in_flight_cents"] == 400
    assert b["outstanding_in_flight_cents"] == 400
    assert b["available_after_in_flight_cents"] == 600

    signed_first = closing.sign_closing(
        "c", first["id"], source_event_id="sign-1"
    ).body
    frozen = json.dumps(signed_first["totals"], sort_keys=True)

    # ---- Bank confirms success after the first cutoff. ----
    reimb.bank.confirm(number, outcome="succeeded", result_event_id="bank-ok")
    # Ledger syncs later (also after the first cutoff).
    reimb.sync_payment_result("c", number)

    # ---- Period 2 cutoff: the expense appears once, this period. ----
    second = closing.create_draft("c", source_event_id="close-2").body
    assert second["opening_basis_closing_id"] == first["id"]
    paid = second["payments"]["bank_confirmed_in_period"]
    assert [p["instruction_number"] for p in paid] == [number]
    assert paid[0]["prior_in_flight"] == [
        {
            "closing_id": first["id"],
            "period_no": 1,
            "closing_cutoff_at": first["cutoff_at"],
        }
    ]
    by_project = {p["project_id"]: p for p in second["projects"]}
    assert by_project["a"]["opening_book_balance_cents"] == 1000
    assert by_project["a"]["bank_confirmed_reimbursement_cents"] == 600
    assert by_project["a"]["closing_book_balance_cents"] == 400
    assert by_project["b"]["closing_book_balance_cents"] == 600

    signed_second = closing.sign_closing(
        "c", second["id"], source_event_id="sign-2"
    ).body

    # ---- A third period must not expense it again. ----
    third = closing.create_draft("c", source_event_id="close-3").body
    assert third["payments"]["bank_confirmed_in_period"] == []
    assert third["payments"]["outstanding_in_flight"] == []
    by_project = {p["project_id"]: p for p in third["projects"]}
    assert by_project["a"]["opening_book_balance_cents"] == 400
    assert by_project["a"]["closing_book_balance_cents"] == 400

    # The first signed statement is unchanged even after all that.
    stored = closing.get_closing("c", first["id"])
    assert json.dumps(stored["totals"], sort_keys=True) == frozen
    assert stored["payments"]["outstanding_in_flight"][0][
        "instruction_number"
    ] == number
    assert signed_second["status"] == "signed"


def test_explicit_bank_failure_releases_hold_without_expense(
    ledger, reimb, closing
):
    _realize(ledger, "a", 1000)
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
    reimb.bank.confirm(number, outcome="failed", result_event_id="bank-no")

    draft = closing.create_draft("c", source_event_id="close").body
    a = {p["project_id"]: p for p in draft["projects"]}["a"]
    assert a["bank_confirmed_reimbursement_cents"] == 0
    assert a["closing_book_balance_cents"] == 1000
    assert a["outstanding_in_flight_cents"] == 0
    assert draft["payments"]["outstanding_in_flight"] == []
    failure = draft["payments"]["bank_explicit_failures"][0]
    assert failure["instruction_number"] == number
    assert failure["expense_cents"] == 0
    assert failure["released_hold"] is True


def test_undiscovered_bank_record_is_normal_in_transit(
    ledger, reimb, closing
):
    _realize(ledger, "a", 1000)
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
    # Simulate "process died before the bank ever received it".
    with sqlite3.connect(reimb.bank_db_path) as conn:
        conn.execute(
            "DELETE FROM bank_payments WHERE instruction_number = ?",
            (number,),
        )
        conn.commit()

    draft = closing.create_draft("c", source_event_id="close").body
    assert draft["signable"] is True
    hold = draft["payments"]["outstanding_in_flight"][0]
    assert hold["bank_record"] == "absent"
    assert hold["hold_state"] == "bank_not_accepted"
    a = {p["project_id"]: p for p in draft["projects"]}["a"]
    assert a["outstanding_in_flight_cents"] == 600


def test_same_invoice_attempts_are_separate(
    ledger, reimb, closing
):
    _realize(ledger, "a", 1000)
    invoice = reimb.register_invoice(
        "c",
        source_event_id="inv",
        payee="Vendor",
        vendor_invoice_number="V-1",
        shares=[{"project_id": "a", "share_cents": 600}],
    ).body
    _consent_all(reimb, invoice)
    first = reimb.submit_payment(
        "c", invoice["id"], source_event_id="pay-1"
    ).body["instruction_number"]
    reimb.deliver_payment_result(
        "c", first, outcome="failed", result_event_id="no"
    )
    second = reimb.submit_payment(
        "c", invoice["id"], source_event_id="pay-2"
    ).body["instruction_number"]

    draft = closing.create_draft("c", source_event_id="close").body
    assert [h["instruction_number"] for h in draft["payments"][
        "outstanding_in_flight"
    ]] == [second]
    assert [f["instruction_number"] for f in draft["payments"][
        "bank_explicit_failures"
    ]] == [first]
    # Each attempt keeps its own identity, though they share an invoice.
    assert draft["payments"]["outstanding_in_flight"][0]["invoice_id"] == (
        draft["payments"]["bank_explicit_failures"][0]["invoice_id"]
    )


# ----------------------------------------------------------------------
# Reconciliation blockers
# ----------------------------------------------------------------------
def _closing_row_count(db_path: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM closings").fetchone()[0]


def test_bank_accepted_after_cutoff_is_not_yet_a_hold_at_cutoff(
    ledger, reimb, closing
):
    """If the bank only accepted after the cutoff, then at the cutoff there
    was no bank acceptance; that is the 'not accepted' hold state, not the
    'accepted, pending' one."""
    from datetime import datetime, timedelta, timezone

    _realize(ledger, "a", 1000)
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
    # Pretend the bank accepted the instruction well after our cutoff.
    future = (
        datetime.now(timezone.utc) + timedelta(days=1)
    ).isoformat().replace("+00:00", "Z")
    with sqlite3.connect(reimb.bank_db_path) as conn:
        conn.execute(
            "UPDATE bank_payments SET accepted_at = ? "
            "WHERE instruction_number = ?",
            (future, number),
        )
        conn.commit()
    # Use the default cutoff ("now"), which is after the instruction existed
    # but well before the bank's future acceptance timestamp.
    draft = closing.create_draft("c", source_event_id="close").body
    hold = draft["payments"]["outstanding_in_flight"][0]
    assert hold["hold_state"] == "bank_not_accepted"
    assert hold["bank_status"] == "pending"


def test_ledger_terminal_without_bank_terminal_blocks_signing(
    ledger, reimb, closing
):
    _realize(ledger, "a", 1000)
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
    # Bank confirms, ledger syncs, then the bank's terminal result is erased
    # (simulating a provider whose record cannot be found).
    reimb.deliver_payment_result(
        "c", number, outcome="succeeded", result_event_id="ok"
    )
    with sqlite3.connect(reimb.bank_db_path) as conn:
        conn.execute(
            "UPDATE bank_payments SET status='pending', result_event_id=NULL,"
            " result_at=NULL WHERE instruction_number = ?",
            (number,),
        )
        conn.commit()

    draft = closing.create_draft("c", source_event_id="close").body
    assert draft["signable"] is False
    codes = {b["code"] for b in draft["blockers"]}
    assert "ledger_terminal_without_bank_terminal" in codes
    blocker = next(
        b
        for b in draft["blockers"]
        if b["code"] == "ledger_terminal_without_bank_terminal"
    )
    assert blocker["details"]["instruction_number"] == number

    with pytest.raises(LedgerError) as exc:
        closing.sign_closing(
            "c", draft["id"], source_event_id="sign"
        )
    assert exc.value.code == "closing_not_signable"
    assert closing.get_closing("c", draft["id"])["status"] == "draft"


def test_conflicting_terminals_block_signing(ledger, reimb, closing):
    _realize(ledger, "a", 1000)
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
    reimb.deliver_payment_result(
        "c", number, outcome="succeeded", result_event_id="ok"
    )
    with sqlite3.connect(reimb.bank_db_path) as conn:
        conn.execute(
            "UPDATE bank_payments SET status='failed' "
            "WHERE instruction_number = ?",
            (number,),
        )
        conn.commit()

    draft = closing.create_draft("c", source_event_id="close").body
    assert draft["signable"] is False
    assert "bank_terminal_conflict" in {b["code"] for b in draft["blockers"]}


def test_bank_field_mismatch_blocks_signing(ledger, reimb, closing):
    _realize(ledger, "a", 1000)
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
    with sqlite3.connect(reimb.bank_db_path) as conn:
        conn.execute(
            "UPDATE bank_payments SET amount_cents = 599 "
            "WHERE instruction_number = ?",
            (number,),
        )
        conn.commit()

    draft = closing.create_draft("c", source_event_id="close").body
    assert draft["signable"] is False
    blocker = next(
        b for b in draft["blockers"] if b["code"] == "bank_instruction_mismatch"
    )
    assert blocker["details"]["mismatched_fields"]["amount_cents"] == {
        "ledger": 600,
        "bank": 599,
    }


def test_unreachable_bank_blocks_signing(db_path, ledger, tmp_path):
    _realize(ledger, "a", 1000)
    import os
    import tempfile

    directory = tempfile.mkdtemp()
    unreadable = os.path.join(directory, "bank-as-file")
    with open(unreadable, "w") as handle:
        handle.write("not a database")
    # The constructor is handed a path that exists but is not a valid bank
    # database; the closing must surface an unreadable-bank blocker.
    service = ClosingService(db_path, bank_db_path=unreadable)
    draft = service.create_draft("c", source_event_id="close").body
    assert draft["signable"] is False
    assert "bank_unavailable" in {b["code"] for b in draft["blockers"]}
    with pytest.raises(LedgerError) as exc:
        service.sign_closing(
            "c", draft["id"], source_event_id="sign"
        )
    assert exc.value.code == "closing_not_signable"


# ----------------------------------------------------------------------
# Draft lifecycle, idempotency, and period integrity
# ----------------------------------------------------------------------
def test_only_one_draft_and_retries_point_to_it(ledger, closing):
    _realize(ledger, "a", 1000)
    first = closing.create_draft("c", source_event_id="close-1")
    assert first.status_code == 201
    retry = closing.create_draft("c", source_event_id="close-1")
    assert retry.replayed is True
    assert retry.body["id"] == first.body["id"]
    assert retry.body["cutoff_at"] == first.body["cutoff_at"]

    with pytest.raises(LedgerError) as exc:
        closing.create_draft("c", source_event_id="close-2")
    assert exc.value.code == "closing_draft_exists"
    assert exc.value.details["closing_id"] == first.body["id"]

    assert _closing_row_count(closing.db_path) == 1


def test_retry_with_changed_cutoff_is_rejected(ledger, closing):
    _realize(ledger, "a", 1000)
    closing.create_draft("c", source_event_id="close-1")
    with pytest.raises(LedgerError) as exc:
        closing.create_draft(
            "c",
            source_event_id="close-1",
            cutoff_at="2000-01-01T00:00:00Z",
        )
    assert exc.value.code == "idempotency_parameter_mismatch"


def test_discarded_draft_cannot_be_signed_and_new_cutoff_allowed(
    ledger, closing
):
    _realize(ledger, "a", 1000)
    draft = closing.create_draft("c", source_event_id="close-1").body
    discarded = closing.discard_closing(
        "c", draft["id"], source_event_id="discard-1"
    ).body
    assert discarded["status"] == "discarded"

    with pytest.raises(LedgerError) as exc:
        closing.sign_closing("c", draft["id"], source_event_id="sign")
    assert exc.value.code == "closing_discarded"

    # A new cutoff is allowed after discarding.
    second = closing.create_draft("c", source_event_id="close-2").body
    assert second["period_no"] == 1
    assert second["id"] != draft["id"]


def test_signing_is_idempotent_and_never_opens_a_second_period(
    ledger, closing
):
    _realize(ledger, "a", 1000)
    draft = closing.create_draft("c", source_event_id="close").body
    signed = closing.sign_closing(
        "c", draft["id"], source_event_id="sign"
    )
    replay = closing.sign_closing(
        "c", draft["id"], source_event_id="sign"
    )
    assert replay.replayed is True
    assert replay.body["id"] == signed.body["id"]
    assert replay.body["signed_at"] == signed.body["signed_at"]

    with pytest.raises(LedgerError) as exc:
        closing.sign_closing(
            "c", draft["id"], source_event_id="sign-other"
        )
    assert exc.value.code == "closing_already_signed"
    assert _closing_row_count(closing.db_path) == 1


def test_new_signed_cutoff_must_be_after_previous(ledger, closing):
    _realize(ledger, "a", 1000)
    first = closing.create_draft("c", source_event_id="close-1").body
    closing.sign_closing("c", first["id"], source_event_id="sign-1")

    # A cutoff at or before the prior signed cutoff would overlap the period.
    with pytest.raises(LedgerError) as exc:
        closing.create_draft(
            "c",
            source_event_id="close-2",
            cutoff_at=first["cutoff_at"],
        )
    assert exc.value.code == "closing_cutoff_not_after_previous"


def test_cutoff_in_future_rejected(ledger, closing):
    _realize(ledger, "a", 1000)
    with pytest.raises(LedgerError) as exc:
        closing.create_draft(
            "c",
            source_event_id="close",
            cutoff_at="2999-01-01T00:00:00Z",
        )
    assert exc.value.code == "closing_cutoff_in_future"


def test_closing_never_mutates_reimbursement_state(
    ledger, reimb, closing
):
    _realize(ledger, "a", 1000)
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
    reimb.bank.confirm(number, outcome="succeeded", result_event_id="ok")
    before_instruction = reimb.get_payment_instruction("c", number)

    closing.create_draft("c", source_event_id="close")
    after_instruction = reimb.get_payment_instruction("c", number)
    assert after_instruction["status"] == before_instruction["status"]
    assert after_instruction["status"] == "pending"
    assert after_instruction["bank"]["status"] == "succeeded"
    # The closing did not submit or sync anything.
    with sqlite3.connect(reimb.bank_db_path) as conn:
        events = conn.execute(
            "SELECT COUNT(*) FROM bank_events WHERE instruction_number = ?",
            (number,),
        ).fetchone()[0]
    assert events == 2  # accepted + result, no extra submission/sync


def test_opening_basis_links_back_to_signed_statement(ledger, closing):
    _realize(ledger, "a", 1000)
    first = closing.create_draft("c", source_event_id="close-1").body
    signed = closing.sign_closing(
        "c", first["id"], source_event_id="sign-1"
    ).body
    listing = closing.list_closings("c")
    assert listing["latest_signed_closing_id"] == first["id"]
    second = closing.create_draft("c", source_event_id="close-2").body
    assert second["opening_basis_closing_id"] == first["id"]
    assert second["opening_basis_cutoff_at"] == signed["cutoff_at"]
    assert second["opening_basis_period_no"] == 1


def test_ledger_result_synced_after_cutoff_does_not_retcon_period(
    ledger, reimb, closing
):
    """A ledger terminal marker dated after the cutoff must not be treated as
    a cutoff terminal state, so reconciliation stays period-accurate."""
    _realize(ledger, "a", 1000)
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
    reimb.bank.confirm(
        number, outcome="succeeded", result_event_id="bank-ok"
    )
    draft = closing.create_draft("c", source_event_id="close").body
    # The bank's confirmation is at/before the cutoff, so it is expensed even
    # though the ledger has not synced it yet.  The ledger's own status at the
    # cutoff is reported as pending.
    paid = draft["payments"]["bank_confirmed_in_period"][0]
    assert paid["instruction_number"] == number
    assert paid["ledger_status_at_cutoff"] == "pending"
    assert paid["expensed_by_bank_first"] is True


def test_pre_cutoff_draft_ignores_bank_result_after_cutoff(
    ledger, reimb, closing
):
    """A payment whose bank success lands after the cutoff is a hold in that
    period, never a retroactive expense."""
    from datetime import datetime, timedelta, timezone

    _realize(ledger, "a", 1000)
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
    # The bank later confirms success, timestamped a day in the future.
    future = (
        datetime.now(timezone.utc) + timedelta(days=1)
    ).isoformat().replace("+00:00", "Z")
    reimb.bank.confirm(
        number, outcome="succeeded", result_event_id="bank-ok"
    )

    with sqlite3.connect(reimb.bank_db_path) as conn:
        conn.execute(
            "UPDATE bank_payments SET result_at = ? "
            "WHERE instruction_number = ?",
            (future, number),
        )
        conn.commit()

    draft = closing.create_draft("c", source_event_id="close").body
    assert draft["payments"]["bank_confirmed_in_period"] == []
    holds = draft["payments"]["outstanding_in_flight"]
    assert [h["instruction_number"] for h in holds] == [number]
    assert holds[0]["hold_state"] == "bank_accepted_pending"


def test_cutoff_earlier_than_later_events_excludes_them(ledger, closing):
    from datetime import datetime, timedelta, timezone

    _realize(ledger, "a", 1000)
    # Take a cutoff in the past (before the grant already recorded).
    past = (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).isoformat().replace("+00:00", "Z")
    draft = closing.create_draft(
        "c", source_event_id="close-past", cutoff_at=past
    ).body
    a = {p["project_id"]: p for p in draft["projects"]}["a"]
    assert a["period_grants_cents"] == 0
    assert a["closing_book_balance_cents"] == 0
    assert a["basis"]["grants"] == []


def test_same_fractional_second_event_is_included(ledger, closing):
    """A grant written in the same wall-clock millisecond as the cutoff must
    still be captured; comparisons parse times, not raw strings."""
    from app.closing import _at_or_before

    _realize(ledger, "a", 1000)
    # The grant uses SQLite's millisecond form; a Python cutoff carries
    # microseconds.  Text ordering would place the "Z"-terminated millisecond
    # stamp *after* the microsecond stamp; parsed-time comparison must not.
    assert _at_or_before(
        "2026-01-01T00:00:00.410Z", "2026-01-01T00:00:00.410123Z"
    )
    assert not _at_or_before(
        "2026-01-01T00:00:00.410123Z", "2026-01-01T00:00:00.410Z"
    )
    assert not _at_or_before(
        "2026-01-01T00:00:00.411999Z", "2026-01-01T00:00:00.410Z"
    )

    # The default cutoff is taken after the grant, so it is captured.
    draft = closing.create_draft("c", source_event_id="close").body
    a = {p["project_id"]: p for p in draft["projects"]}["a"]
    assert a["period_grants_cents"] == 1000
    assert a["closing_book_balance_cents"] == 1000
