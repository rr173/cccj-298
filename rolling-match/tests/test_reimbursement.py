from __future__ import annotations

import sqlite3

import pytest

from app.db import init_db
from app.reimbursement import ReimbursementService
from app.service import LedgerError, LedgerService


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "joint.db"
    init_db(path)
    return str(path)


@pytest.fixture
def ledger(db_path):
    svc = LedgerService(db_path)
    svc.create_campaign(
        campaign_id="c",
        name="Joint",
        total_pool_limit_cents=1_000_000,
        donor_match_cap_cents=1_000_000,
    )
    return svc


@pytest.fixture
def reimb(db_path):
    return ReimbursementService(db_path)


def _realize(ledger, project_id: str, amount_cents: int) -> None:
    """Give a project actual (not provisional) matching money."""
    ledger.create_project("c", project_id=project_id, name=project_id.upper())
    ledger.create_donation(
        "c",
        source_event_id=f"don-{project_id}",
        donor_id=f"donor-{project_id}",
        project_id=project_id,
        amount_cents=amount_cents,
    )
    ledger.settle_project("c", project_id, source_event_id=f"set-{project_id}")


def _register(reimb, shares, *, payee="Vendor", number="V-1", source="inv-1"):
    return reimb.register_invoice(
        "c",
        source_event_id=source,
        payee=payee,
        vendor_invoice_number=number,
        shares=shares,
    )


def _consent_all(reimb, invoice, prefix="consent"):
    for share in invoice["shares"]:
        reimb.set_invoice_consent(
            "c",
            invoice["id"],
            source_event_id=f"{prefix}-{invoice['id']}-{share['project_id']}",
            project_id=share["project_id"],
            consented=True,
        )


def test_registration_derives_total_and_exact_retry_returns_original(
    ledger, reimb
):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)

    first = _register(
        reimb,
        [
            {"project_id": "b", "share_cents": 400},
            {"project_id": "a", "share_cents": 600},
        ],
    )
    assert first.status_code == 201
    assert first.body["total_amount_cents"] == 1000
    assert first.body["payable"] is False
    assert first.body["not_payable_reason"] == "consent_pending"
    assert first.body["fully_consented"] is False

    replay = _register(
        reimb,
        [
            {"project_id": "a", "share_cents": 600},
            {"project_id": "b", "share_cents": 400},
        ],
    )
    assert replay.status_code == 200
    assert replay.replayed is True
    assert replay.body["id"] == first.body["id"]

    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM invoice_shares").fetchone()[0]
            == 2
        )


def test_same_vendor_invoice_with_changed_shares_is_rejected(ledger, reimb):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    _register(reimb, [{"project_id": "a", "share_cents": 600}])

    with pytest.raises(LedgerError) as exc:
        _register(
            reimb,
            [{"project_id": "a", "share_cents": 500}],
            source="inv-different-source",
        )
    assert exc.value.code == "invoice_registration_conflict"

    # Same payee + number is scoped to the campaign: another payee is fine.
    other = _register(
        reimb,
        [{"project_id": "a", "share_cents": 100}],
        payee="OtherVendor",
        number="V-1",
        source="inv-other",
    )
    assert other.status_code == 201


def test_shares_must_be_positive_and_projects_distinct(ledger, reimb):
    _realize(ledger, "a", 1000)
    with pytest.raises(LedgerError) as exc:
        _register(reimb, [{"project_id": "a", "share_cents": 0}])
    assert exc.value.code == "invoice_share_not_positive"

    with pytest.raises(LedgerError) as exc:
        _register(
            reimb,
            [
                {"project_id": "a", "share_cents": 10},
                {"project_id": "a", "share_cents": 20},
            ],
            source="dup",
        )
    assert exc.value.code == "invoice_duplicate_project"

    with pytest.raises(LedgerError) as exc:
        _register(reimb, [{"project_id": "missing", "share_cents": 10}])
    assert exc.value.code == "project_not_found"


def test_only_fully_consented_invoices_are_payable(ledger, reimb):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    invoice = _register(
        reimb,
        [
            {"project_id": "a", "share_cents": 600},
            {"project_id": "b", "share_cents": 400},
        ],
    ).body

    reimb.set_invoice_consent(
        "c",
        invoice["id"],
        source_event_id="ca",
        project_id="a",
        consented=True,
    )
    view = reimb.get_invoice("c", invoice["id"])
    assert view["payable"] is False
    assert view["missing_consent_projects"] == ["b"]

    reimb.set_invoice_consent(
        "c",
        invoice["id"],
        source_event_id="cb",
        project_id="b",
        consented=True,
    )
    view = reimb.get_invoice("c", invoice["id"])
    assert view["payable"] is True
    assert view["is_queued"] is True
    assert view["reserved_cents_by_project"] == {"a": 600, "b": 400}

    funds = {
        p["project_id"]: p for p in reimb.project_funds("c")["projects"]
    }
    assert funds["a"]["reserved_cents"] == 600
    assert funds["a"]["available_cents"] == 400
    assert funds["b"]["reserved_cents"] == 400
    assert funds["b"]["available_cents"] == 600


def test_withdraw_consent_before_payment_releases_reservation(ledger, reimb):
    _realize(ledger, "a", 1000)
    invoice = _register(reimb, [{"project_id": "a", "share_cents": 600}]).body
    _consent_all(reimb, invoice)
    assert reimb.get_invoice("c", invoice["id"])["payable"] is True

    reimb.set_invoice_consent(
        "c",
        invoice["id"],
        source_event_id="withdraw",
        project_id="a",
        consented=False,
    )
    view = reimb.get_invoice("c", invoice["id"])
    assert view["payable"] is False
    assert view["reserved_cents_by_project"] is None
    assert reimb.project_funds("c")["projects"][0]["reserved_cents"] == 0


def test_provisional_match_is_not_spendable(ledger, reimb):
    ledger.create_project("c", project_id="a", name="A")
    # Donation accepted but never settled: provisional only.
    ledger.create_donation(
        "c",
        source_event_id="don-a",
        donor_id="donor-a",
        project_id="a",
        amount_cents=1000,
    )
    assert ledger.provisional("c")["items"][0]["provisional_match_cents"] == 1000

    invoice = _register(reimb, [{"project_id": "a", "share_cents": 600}]).body
    _consent_all(reimb, invoice)
    view = reimb.get_invoice("c", invoice["id"])
    assert view["payable"] is False
    assert view["not_payable_reason"] == "insufficient_project_funds"
    assert view["shortfall_cents_by_project"] == {"a": 600}

    with pytest.raises(LedgerError) as exc:
        reimb.submit_payment("c", invoice["id"], source_event_id="pay")
    assert exc.value.code == "invoice_not_payable"


def test_projects_cannot_borrow_from_each_other(ledger, reimb):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 100)
    invoice = _register(
        reimb,
        [
            {"project_id": "a", "share_cents": 50},
            {"project_id": "b", "share_cents": 200},
        ],
    ).body
    _consent_all(reimb, invoice)
    view = reimb.get_invoice("c", invoice["id"])
    assert view["payable"] is False
    assert view["shortfall_cents_by_project"] == {"b": 100}
    # a is not reserved because the whole invoice does not fit.
    funds = {
        p["project_id"]: p for p in reimb.project_funds("c")["projects"]
    }
    assert funds["a"]["reserved_cents"] == 0


def test_queue_follows_registration_order_and_skips_whole_invoices(
    ledger, reimb
):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)

    first = _register(
        reimb,
        [{"project_id": "a", "share_cents": 700}],
        number="V-1",
        source="inv-1",
    ).body
    # This one no longer fits after the first and must be skipped as a whole.
    skipped = _register(
        reimb,
        [{"project_id": "a", "share_cents": 400}],
        number="V-2",
        source="inv-2",
    ).body
    # A later invoice on a different project still gets its share.
    later = _register(
        reimb,
        [{"project_id": "b", "share_cents": 900}],
        number="V-3",
        source="inv-3",
    ).body
    for invoice in (first, skipped, later):
        _consent_all(reimb, invoice)

    overview = reimb.invoice_overview("c")
    by_number = {i["vendor_invoice_number"]: i for i in overview["invoices"]}
    assert by_number["V-1"]["payable"] is True
    assert by_number["V-2"]["payable"] is False
    assert by_number["V-2"]["not_payable_reason"] == (
        "insufficient_project_funds"
    )
    assert by_number["V-2"]["shortfall_cents_by_project"] == {"a": 100}
    assert by_number["V-3"]["payable"] is True
    funds = {
        p["project_id"]: p for p in reimb.project_funds("c")["projects"]
    }
    # a's 700 is reserved by V-1 only; V-2 is skipped entirely.
    assert funds["a"]["reserved_cents"] == 700
    assert funds["a"]["available_cents"] == 300
    assert funds["b"]["reserved_cents"] == 900


def test_successful_payment_keeps_shares_and_blocks_second_payment(
    ledger, reimb
):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    invoice = _register(
        reimb,
        [
            {"project_id": "a", "share_cents": 600},
            {"project_id": "b", "share_cents": 400},
        ],
    ).body
    _consent_all(reimb, invoice)

    submitted = reimb.submit_payment(
        "c", invoice["id"], source_event_id="pay-1"
    )
    number = submitted.body["instruction_number"]
    assert submitted.body["amount_cents"] == 1000

    result = reimb.deliver_payment_result(
        "c", number, outcome="succeeded", result_event_id="bank-ok"
    )
    assert result.body["status"] == "succeeded"
    assert result.body["allocations"] == [
        {
            "project_id": "a",
            "responsible_project_id": "a",
            "share_cents": 600,
        },
        {
            "project_id": "b",
            "responsible_project_id": "b",
            "share_cents": 400,
        },
    ]

    funds = {
        p["project_id"]: p for p in reimb.project_funds("c")["projects"]
    }
    assert funds["a"]["paid_cents"] == 600
    assert funds["b"]["paid_cents"] == 400
    assert funds["a"]["available_cents"] == 400

    view = reimb.get_invoice("c", invoice["id"])
    assert view["state"] == "paid"
    assert view["succeeded_instruction"]["instruction_number"] == number
    assert view["not_payable_reason"] == "invoice_already_paid"

    with pytest.raises(LedgerError) as exc:
        reimb.submit_payment("c", invoice["id"], source_event_id="pay-2")
    assert exc.value.code == "invoice_already_paid"

    # Consent is locked after success.
    with pytest.raises(LedgerError) as exc:
        reimb.set_invoice_consent(
            "c",
            invoice["id"],
            source_event_id="late-withdraw",
            project_id="a",
            consented=False,
        )
    assert exc.value.code == "invoice_consent_locked"


def test_duplicate_and_contradictory_results_do_not_change_confirmed_state(
    ledger, reimb
):
    _realize(ledger, "a", 1000)
    invoice = _register(reimb, [{"project_id": "a", "share_cents": 600}]).body
    _consent_all(reimb, invoice)
    number = reimb.submit_payment(
        "c", invoice["id"], source_event_id="pay"
    ).body["instruction_number"]

    first = reimb.deliver_payment_result(
        "c", number, outcome="succeeded", result_event_id="bank-1"
    )
    assert first.body["status"] == "succeeded"

    duplicate = reimb.deliver_payment_result(
        "c", number, outcome="succeeded", result_event_id="bank-1"
    )
    assert duplicate.replayed is True
    assert duplicate.body["status"] == "succeeded"

    with pytest.raises(LedgerError) as exc:
        reimb.deliver_payment_result(
            "c", number, outcome="failed", result_event_id="bank-2"
        )
    assert exc.value.code == "payment_result_conflict"

    assert reimb.get_invoice("c", invoice["id"])["state"] == "paid"
    assert reimb.project_funds("c")["projects"][0]["paid_cents"] == 600


def test_failed_payment_releases_hold_and_requeues_for_a_new_attempt(
    ledger, reimb
):
    _realize(ledger, "a", 1000)
    invoice = _register(reimb, [{"project_id": "a", "share_cents": 600}]).body
    _consent_all(reimb, invoice)

    first = reimb.submit_payment(
        "c", invoice["id"], source_event_id="pay-1"
    ).body
    assert first["status"] == "pending"
    funds = reimb.project_funds("c")["projects"][0]
    assert funds["in_flight_cents"] == 600
    assert funds["available_cents"] == 400

    failed = reimb.deliver_payment_result(
        "c", first["instruction_number"], outcome="failed", result_event_id="no"
    )
    assert failed.body["status"] == "failed"

    view = reimb.get_invoice("c", invoice["id"])
    assert view["state"] == "unpaid"
    assert view["payable"] is True
    funds = reimb.project_funds("c")["projects"][0]
    assert funds["in_flight_cents"] == 0
    assert funds["available_cents"] == 400
    assert funds["reserved_cents"] == 600

    second = reimb.submit_payment(
        "c", invoice["id"], source_event_id="pay-2"
    ).body
    assert second["attempt_no"] == 2
    assert second["instruction_number"] != first["instruction_number"]

    # A late result for the old failed instruction cannot touch the new one.
    with pytest.raises(LedgerError) as exc:
        reimb.deliver_payment_result(
            "c",
            first["instruction_number"],
            outcome="succeeded",
            result_event_id="late-ok",
        )
    assert exc.value.code == "payment_result_conflict"
    current = reimb.get_payment_instruction(
        "c", second["instruction_number"]
    )
    assert current["status"] == "pending"
    assert reimb.get_invoice("c", invoice["id"])["state"] == "in_flight"


def test_reversal_during_flight_preserves_hold_and_shows_gap(ledger, reimb):
    _realize(ledger, "a", 1000)
    invoice = _register(reimb, [{"project_id": "a", "share_cents": 600}]).body
    _consent_all(reimb, invoice)
    number = reimb.submit_payment(
        "c", invoice["id"], source_event_id="pay"
    ).body["instruction_number"]

    # Refund enough to make realized funding smaller than the in-flight hold.
    refund = ledger.create_refund(
        "c",
        source_event_id="refund",
        donation_source_event_id="don-a",
        amount_cents=700,
    )
    assert refund.body["actual_reversal"]["amount_cents"] == 700

    fund = reimb.project_funds("c")["projects"][0]
    assert fund["realized_net_grant_cents"] == 300
    assert fund["in_flight_cents"] == 600
    assert fund["available_cents"] == 0
    assert fund["gap_cents"] == 300

    # The in-flight instruction is untouched.
    instruction = reimb.get_payment_instruction("c", number)
    assert instruction["status"] == "pending"
    assert instruction["amount_cents"] == 600
    assert reimb.get_invoice("c", invoice["id"])["state"] == "in_flight"

    # A new grant first fills the gap before becoming available.
    ledger.create_donation(
        "c",
        source_event_id="don-topup",
        donor_id="topup",
        project_id="a",
        amount_cents=200,
    )
    ledger.settle_project("c", "a", source_event_id="set-topup")
    fund = reimb.project_funds("c")["projects"][0]
    assert fund["realized_net_grant_cents"] == 500
    assert fund["gap_cents"] == 100
    assert fund["available_cents"] == 0

    # On a definitive success the gap follows the paid amount.
    reimb.deliver_payment_result(
        "c", number, outcome="succeeded", result_event_id="ok"
    )
    fund = reimb.project_funds("c")["projects"][0]
    assert fund["paid_cents"] == 600
    assert fund["in_flight_cents"] == 0
    assert fund["gap_cents"] == 100


def test_funds_recomputed_from_current_consents_and_cash(ledger, reimb):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    first = _register(
        reimb,
        [{"project_id": "a", "share_cents": 600}],
        number="V-1",
        source="inv-1",
    ).body
    second = _register(
        reimb,
        [{"project_id": "a", "share_cents": 300}],
        number="V-2",
        source="inv-2",
    ).body
    _consent_all(reimb, first)
    _consent_all(reimb, second)
    # a: 1000, first reserves 600, second reserves 300, 100 free.
    funds = reimb.project_funds("c")["projects"]
    a = {p["project_id"]: p for p in funds}["a"]
    assert a["reserved_cents"] == 900
    assert a["available_cents"] == 100
    assert reimb.get_invoice("c", second["id"])["payable"] is True

    # Withdrawing the first invoice's consent frees its 600 immediately and
    # the second invoice is unaffected.
    reimb.set_invoice_consent(
        "c",
        first["id"],
        source_event_id="withdraw-1",
        project_id="a",
        consented=False,
    )
    a = {p["project_id"]: p for p in reimb.project_funds("c")["projects"]}["a"]
    assert a["reserved_cents"] == 300
    assert a["available_cents"] == 700
    assert reimb.get_invoice("c", second["id"])["payable"] is True


def test_gap_blocks_new_invoices_until_filled(ledger, reimb):
    _realize(ledger, "a", 1000)
    invoice = _register(reimb, [{"project_id": "a", "share_cents": 600}]).body
    _consent_all(reimb, invoice)
    reimb.submit_payment("c", invoice["id"], source_event_id="pay")

    ledger.create_refund(
        "c",
        source_event_id="big-refund",
        donation_source_event_id="don-a",
        amount_cents=900,
    )
    fund = reimb.project_funds("c")["projects"][0]
    assert fund["realized_net_grant_cents"] == 100
    assert fund["gap_cents"] == 500

    # Even a tiny new invoice cannot use the negative headroom.
    new_invoice = _register(
        reimb,
        [{"project_id": "a", "share_cents": 50}],
        number="V-2",
        source="inv-2",
    ).body
    _consent_all(reimb, new_invoice)
    view = reimb.get_invoice("c", new_invoice["id"])
    assert view["payable"] is False
    assert view["shortfall_cents_by_project"] == {"a": 50}


def test_sync_resubmits_same_instruction_if_bank_never_received_it(
    ledger, reimb, monkeypatch
):
    _realize(ledger, "a", 1000)
    invoice = _register(reimb, [{"project_id": "a", "share_cents": 600}]).body
    _consent_all(reimb, invoice)

    # Simulate a crash between the durable ledger instruction and the bank
    # call: create the instruction but skip the bank submission.
    monkeypatch.setattr(reimb, "_ensure_bank_submitted", lambda _body: None)
    number = reimb.submit_payment(
        "c", invoice["id"], source_event_id="pay"
    ).body["instruction_number"]
    assert reimb.bank.get(number) is None
    monkeypatch.undo()

    # Recovery re-submits the very same instruction number exactly once.
    recovered = reimb.sync_payment_result("c", number)
    assert recovered["instruction_number"] == number
    assert recovered["status"] == "pending"
    assert recovered["bank"]["status"] == "pending"

    recovered_again = reimb.sync_payment_result("c", number)
    assert recovered_again["instruction_number"] == number
    accepted = [
        e
        for e in reimb.bank.list_events(number)
        if e["event_type"] == "accepted"
    ]
    assert len(accepted) == 1


def test_result_for_unknown_instruction_is_rejected(ledger, reimb):
    _realize(ledger, "a", 1000)
    with pytest.raises(LedgerError) as exc:
        reimb.deliver_payment_result(
            "c", "PAY-999-1", outcome="succeeded", result_event_id="x"
        )
    assert exc.value.code == "payment_instruction_not_found"


def test_consent_cannot_change_while_in_flight(ledger, reimb):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    invoice = _register(
        reimb,
        [
            {"project_id": "a", "share_cents": 600},
            {"project_id": "b", "share_cents": 400},
        ],
    ).body
    _consent_all(reimb, invoice)
    number = reimb.submit_payment(
        "c", invoice["id"], source_event_id="pay"
    ).body["instruction_number"]

    with pytest.raises(LedgerError) as exc:
        reimb.set_invoice_consent(
            "c",
            invoice["id"],
            source_event_id="withdraw",
            project_id="a",
            consented=False,
        )
    assert exc.value.code == "invoice_consent_locked"
    assert exc.value.details["instruction_number"] == number

    # Retrying consent with the exact same source event during flight replays
    # the original accepted response rather than mutating anything.
    replay = reimb.set_invoice_consent(
        "c",
        invoice["id"],
        source_event_id="consent-%d-a" % invoice["id"],
        project_id="a",
        consented=True,
    )
    assert replay.replayed is True
