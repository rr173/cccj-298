from __future__ import annotations

import sqlite3

import pytest

from app.db import init_db
from app.merger import MergerService
from app.reimbursement import ReimbursementService
from app.service import LedgerError, LedgerService


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "merger.db"
    init_db(path)
    return str(path)


@pytest.fixture
def ledger(db_path):
    svc = LedgerService(db_path)
    svc.create_campaign(
        campaign_id="c",
        name="Merger",
        total_pool_limit_cents=1_000_000,
        donor_match_cap_cents=1_000_000,
    )
    return svc


@pytest.fixture
def reimb(db_path):
    return ReimbursementService(db_path)


@pytest.fixture
def merger(db_path):
    return MergerService(db_path)


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


def _invoice(reimb, shares, *, source, number, payee="Vendor"):
    return reimb.register_invoice(
        "c",
        source_event_id=source,
        payee=payee,
        vendor_invoice_number=number,
        shares=shares,
    )


def _consent(reimb, invoice, project_id, source):
    return reimb.set_invoice_consent(
        "c",
        invoice["id"],
        source_event_id=source,
        project_id=project_id,
        consented=True,
    )


def _consent_all(reimb, invoice):
    for share in invoice["shares"]:
        _consent(
            reimb,
            invoice,
            share["project_id"],
            f"consent-{invoice['id']}-{share['project_id']}",
        )


# ----------------------------------------------------------------------
# Validation and lifecycle
# ----------------------------------------------------------------------
def test_merge_requires_two_distinct_active_projects(ledger, merger):
    _realize(ledger, "a", 1000)
    ledger.create_project("c", project_id="b", name="B")

    with pytest.raises(LedgerError) as exc:
        merger.merge_projects(
            "c",
            source_event_id="m-same",
            source_project_id="a",
            successor_project_id="a",
        )
    assert exc.value.code == "merger_same_project"
    assert exc.value.status_code == 422

    with pytest.raises(LedgerError) as exc:
        merger.merge_projects(
            "c",
            source_event_id="m-missing",
            source_project_id="a",
            successor_project_id="ghost",
        )
    assert exc.value.code == "project_not_found"


def test_a_merged_project_cannot_merge_again(ledger, merger):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    _realize(ledger, "c2", 1000)

    merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )
    # The source is merged out and cannot be a source again.
    with pytest.raises(LedgerError) as exc:
        merger.merge_projects(
            "c",
            source_event_id="m2",
            source_project_id="a",
            successor_project_id="c2",
        )
    assert exc.value.code == "merger_source_not_active"

    # A merged-out project cannot be a successor either.
    with pytest.raises(LedgerError) as exc:
        merger.merge_projects(
            "c",
            source_event_id="m3",
            source_project_id="c2",
            successor_project_id="a",
        )
    assert exc.value.code == "merger_successor_not_active"


def test_merger_is_irreversible_but_successor_can_merge_again(ledger, merger):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    _realize(ledger, "c2", 1000)

    first = merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )
    assert first.body["irreversible"] is True
    second = merger.merge_projects(
        "c",
        source_event_id="m2",
        source_project_id="b",
        successor_project_id="c2",
    )
    assert second.status_code == 201

    with sqlite3.connect(merger.db_path) as conn:
        statuses = dict(
            conn.execute(
                "SELECT id, status FROM projects ORDER BY id"
            ).fetchall()
        )
    assert statuses == {"a": "merged", "b": "merged", "c2": "active"}

    view = merger.project_responsibility("c")
    current = {
        p["project_id"]: p["current_responsible_project_id"]
        for p in view["projects"]
    }
    assert current == {"a": "c2", "b": "c2", "c2": "c2"}
    chain = next(p for p in view["projects"] if p["project_id"] == "a")
    assert [step["to_project_id"] for step in chain["merger_chain"]] == [
        "b",
        "c2",
    ]


def test_merger_idempotent_replay_and_changed_parameter_rejected(
    ledger, merger
):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    ledger.create_project("c", project_id="c2", name="C2")

    first = merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )
    replay = merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )
    assert replay.replayed is True
    assert replay.body["id"] == first.body["id"]
    assert (
        replay.body["transferred_book_balance_cents"]
        == first.body["transferred_book_balance_cents"]
    )

    with pytest.raises(LedgerError) as exc:
        merger.merge_projects(
            "c",
            source_event_id="m1",
            source_project_id="a",
            successor_project_id="c2",
        )
    assert exc.value.code == "idempotency_parameter_mismatch"

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


# ----------------------------------------------------------------------
# Transferred book balance
# ----------------------------------------------------------------------
def test_transferred_balance_deducts_actual_spend_but_not_in_flight(
    ledger, reimb, merger
):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)

    paid_inv = _invoice(
        reimb,
        [{"project_id": "a", "share_cents": 250}],
        source="inv-paid",
        number="V-paid",
    ).body
    _consent_all(reimb, paid_inv)
    paid_number = reimb.submit_payment(
        "c", paid_inv["id"], source_event_id="pay-paid"
    ).body["instruction_number"]
    reimb.deliver_payment_result(
        "c", paid_number, outcome="succeeded", result_event_id="ok-paid"
    )

    inflight_inv = _invoice(
        reimb,
        [{"project_id": "a", "share_cents": 300}],
        source="inv-flight",
        number="V-flight",
    ).body
    _consent_all(reimb, inflight_inv)
    flight_number = reimb.submit_payment(
        "c", inflight_inv["id"], source_event_id="pay-flight"
    ).body["instruction_number"]

    result = merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )
    # Book = 1000 grant - 250 confirmed spend; the 300 in flight is NOT
    # deducted from the transferred book balance.
    assert result.body["transferred_book_balance_cents"] == 750
    entries = {
        e["direction"]: e["amount_cents"]
        for e in result.body["internal_transfer_entries"]
    }
    assert entries == {"out": 750, "in": 750}

    # The in-flight instruction is untouched and now occupies the successor.
    instruction = reimb.get_payment_instruction("c", flight_number)
    assert instruction["status"] == "pending"
    assert instruction["instruction_number"] == flight_number
    funds = {
        p["project_id"]: p
        for p in reimb.project_funds("c")["projects"]
    }
    # b: own 1000 + transferred 750 = 1750.  The transferred 750 already nets
    # the 250 confirmed spend, and the 300 in-flight hold is assumed in full;
    # available = 1750 - 300 in flight = 1450 (the historical 250 is never
    # counted twice).
    assert funds["b"]["realized_net_grant_cents"] == 1750
    assert funds["b"]["paid_cents"] == 250
    assert funds["b"]["book_balance_spend_cents"] == 0
    assert funds["b"]["in_flight_cents"] == 300
    assert funds["b"]["available_cents"] == 1450
    # The merged project keeps its historical grant and the spend confirmed
    # before the merger; the transfer-out zeroes its remaining book, and it no
    # longer carries any hold.
    assert funds["a"]["gross_granted_cents"] == 1000
    assert funds["a"]["internal_transfer_out_cents"] == 750
    assert funds["a"]["book_balance_spend_cents"] == 250
    assert funds["a"]["book_balance_cents"] == 0
    assert funds["a"]["in_flight_cents"] == 0
    assert funds["a"]["available_cents"] == 0


def test_transferred_balance_may_be_negative(ledger, reimb, merger):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    invoice = _invoice(
        reimb,
        [{"project_id": "a", "share_cents": 1000}],
        source="inv",
        number="V-1",
    ).body
    _consent_all(reimb, invoice)
    number = reimb.submit_payment(
        "c", invoice["id"], source_event_id="pay"
    ).body["instruction_number"]
    reimb.deliver_payment_result(
        "c", number, outcome="succeeded", result_event_id="ok"
    )
    # A later reversal pushes the realized money below the confirmed spend.
    ledger.create_refund(
        "c",
        source_event_id="refund",
        donation_source_event_id="don-a",
        amount_cents=400,
    )

    result = merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )
    # Book = 1000 - 400 reversal - 1000 confirmed spend = -400.
    assert result.body["transferred_book_balance_cents"] == -400
    funds = {
        p["project_id"]: p
        for p in reimb.project_funds("c")["projects"]
    }
    # The successor's realized money already nets the negative transfer:
    # 1000 own - 400 inherited shortfall = 600.  The shortfall is not counted
    # a second time as a gap because the 1000 historical spend stays with the
    # merged project's book (zeroed by the transfer).
    assert funds["b"]["realized_net_grant_cents"] == 600
    assert funds["b"]["internal_transfer_in_cents"] == -400
    assert funds["b"]["book_balance_cents"] == 600
    assert funds["b"]["gap_cents"] == 0
    assert funds["b"]["available_cents"] == 600
    # The merged project's book nets to zero: its shortfall left via transfer.
    assert funds["a"]["book_balance_cents"] == 0
    assert funds["a"]["available_cents"] == 0


def test_bank_confirmed_but_unsynced_spend_is_deducted_at_merger(
    ledger, reimb, merger
):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    invoice = _invoice(
        reimb,
        [{"project_id": "a", "share_cents": 600}],
        source="inv",
        number="V-1",
    ).body
    _consent_all(reimb, invoice)
    number = reimb.submit_payment(
        "c", invoice["id"], source_event_id="pay"
    ).body["instruction_number"]
    # The bank confirms success but the ledger has not synced it.
    reimb.bank.confirm(number, outcome="succeeded", result_event_id="bank-ok")
    assert reimb.get_payment_instruction("c", number)["status"] == "pending"

    result = merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )
    # The merger never syncs, but the bank-first balance already deducts 600.
    assert result.body["transferred_book_balance_cents"] == 400
    assert result.body["bank_balance_basis"] == (
        "bank_first_confirmed_spend_deducted"
    )
    # It must not have submitted or synced anything.
    assert reimb.get_payment_instruction("c", number)["status"] == "pending"


# ----------------------------------------------------------------------
# What a merged project can no longer do
# ----------------------------------------------------------------------
def test_merger_refuses_when_bank_record_is_unreadable(
    ledger, merger, tmp_path
):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    unreadable = tmp_path / "bank-as-file"
    unreadable.write_text("not a database")
    blocked = MergerService(merger.db_path, bank_db_path=str(unreadable))
    with pytest.raises(LedgerError) as exc:
        blocked.merge_projects(
            "c",
            source_event_id="m1",
            source_project_id="a",
            successor_project_id="b",
        )
    assert exc.value.code == "merger_bank_unavailable"
    assert exc.value.status_code == 503
    # Nothing was written; the same source event can be retried afterwards.
    with sqlite3.connect(merger.db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM project_mergers").fetchone()[0]
            == 0
        )
    retry = merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )
    assert retry.status_code == 201


def test_merged_project_cannot_receive_donations_or_settle_or_share(
    ledger, reimb, merger
):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )

    with pytest.raises(LedgerError) as exc:
        ledger.create_donation(
            "c",
            source_event_id="new-don",
            donor_id="d",
            project_id="a",
            amount_cents=10,
        )
    assert exc.value.code == "project_merged_no_new_donations"

    with pytest.raises(LedgerError) as exc:
        ledger.settle_project("c", "a", source_event_id="set-late")
    assert exc.value.code == "project_merged_cannot_settle"

    with pytest.raises(LedgerError) as exc:
        ledger.set_project_ready("c", "a", False)
    assert exc.value.code == "project_merged_cannot_change_ready"

    with pytest.raises(LedgerError) as exc:
        _invoice(
            reimb,
            [{"project_id": "a", "share_cents": 10}],
            source="new-inv",
            number="V-new",
        )
    assert exc.value.code == "project_merged_no_new_invoice_share"


def test_old_donations_still_refund_but_reversal_follows_successor(
    ledger, merger
):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )

    # The original donation order is still refundable in full, and the
    # matching/reversal rules are unchanged.
    refund = ledger.create_refund(
        "c",
        source_event_id="refund-a",
        donation_source_event_id="don-a",
        amount_cents=1000,
    )
    reversal = refund.body["actual_reversal"]
    assert reversal is not None
    assert reversal["amount_cents"] == 1000
    assert reversal["responsible_project_id"] == "b"

    # Traceability: the reversal still names the original donation.
    listing = ledger.list_reversals("c")
    row = listing["reversals"][0]
    assert row["donation_source_event_id"] == "don-a"
    assert row["original_project_id"] == "a"
    assert row["responsible_project_id"] == "b"

    funds = {
        p["project_id"]: p
        for p in ReimbursementService(merger.db_path).project_funds("c")[
            "projects"
        ]
    }
    # b's own 1000 minus the 1000 reversal it inherited, plus the transferred
    # book (which was zero after the reversal-consistent transfer).
    assert funds["b"]["reversed_cents"] == 1000


def test_successor_settles_waiting_donations_of_merged_project(ledger, merger):
    # A donation to 'a' that never settled keeps a provisional match.  After
    # a -> b, the successor settles it; the grant keeps 'a' as the original
    # project but records b as responsible.
    ledger.create_project("c", project_id="a", name="A")
    _realize(ledger, "b", 1000)
    ledger.create_donation(
        "c",
        source_event_id="don-a",
        donor_id="donor-a",
        project_id="a",
        amount_cents=500,
    )
    merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )

    settled = ledger.settle_project(
        "c", "b", source_event_id="set-b-a"
    ).body
    assert settled["total_amount_cents"] == 500
    item = settled["items"][0]
    assert item["original_project_id"] == "a"
    assert item["responsible_project_id"] == "b"
    assert item["donation_source_event_id"] == "don-a"

    # Grant history remains traceable under both ids.
    grants = ledger.list_project_grants("c", "b")
    assert grants["gross_granted_cents"] == 1500
    original_ids = {
        i["original_project_id"]
        for s in grants["settlements"]
        for i in s["items"]
    }
    assert original_ids == {"a", "b"}


# ----------------------------------------------------------------------
# Invoice shares, consent and queueing after a merger
# ----------------------------------------------------------------------
def test_share_responsibility_and_consent_are_transferred(
    ledger, reimb, merger
):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    invoice = _invoice(
        reimb,
        [{"project_id": "a", "share_cents": 600}],
        source="inv",
        number="V-1",
    ).body
    _consent_all(reimb, invoice)
    assert reimb.get_invoice("c", invoice["id"])["payable"] is True

    merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )

    view = reimb.get_invoice("c", invoice["id"])
    share = view["shares"][0]
    assert share["project_id"] == "a"
    assert share["responsible_project_id"] == "b"
    # The old consent is invalidated and the invoice no longer queues.
    assert share["consented"] is False
    assert view["fully_consented"] is False
    assert view["payable"] is False
    assert view["not_payable_reason"] == "consent_pending"
    assert view["missing_consent_projects"] == ["b"]

    # The original shareholder can no longer confirm; only the successor.
    with pytest.raises(LedgerError) as exc:
        _consent(reimb, invoice, "a", "late-a")
    assert exc.value.code == "invoice_consent_project_not_responsible"

    _consent(reimb, invoice, "b", "reconfirm-b")
    view = reimb.get_invoice("c", invoice["id"])
    assert view["payable"] is True
    assert view["reserved_cents_by_project"] == {"b": 600}


def test_combined_shares_are_judged_against_one_balance(ledger, reimb, merger):
    # Two original shareholders a and a2 both merge into b.  An invoice (and
    # the queue) must combine their shares against b's single balance rather
    # than letting each share spend the same money once.
    _realize(ledger, "a", 1000)
    _realize(ledger, "a2", 1000)
    _realize(ledger, "b", 1000)

    invoice = _invoice(
        reimb,
        [
            {"project_id": "a", "share_cents": 700},
            {"project_id": "a2", "share_cents": 700},
        ],
        source="inv",
        number="V-1",
    ).body
    _consent_all(reimb, invoice)
    merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )
    merger.merge_projects(
        "c",
        source_event_id="m2",
        source_project_id="a2",
        successor_project_id="b",
    )
    # One successor confirmation covers every transferred share it now bears
    # on the invoice.
    _consent(reimb, invoice, "b", "reconfirm-b")
    view = reimb.get_invoice("c", invoice["id"])
    # b holds its own 1000 + 1000 + 1000 transferred = 3000; combined 1400
    # fits, and the shortfall is reported once against the combined total.
    assert view["payable"] is True
    assert view["reserved_cents_by_project"] == {"b": 1400}

    # Now an invoice whose combined shares exceed what is left is skipped as a
    # whole, with one combined shortfall.  It is registered under the current
    # responsible project because the merged originals cannot take new shares.
    second = _invoice(
        reimb,
        [{"project_id": "b", "share_cents": 1800}],
        source="inv2",
        number="V-2",
    ).body
    _consent(reimb, second, "b", "reconfirm-b2")
    view2 = reimb.get_invoice("c", second["id"])
    assert view2["payable"] is False
    assert view2["not_payable_reason"] == "insufficient_project_funds"
    # 1800 against 3000 - 1400 reserved = 1600 -> 200 short once.
    assert view2["shortfall_cents_by_project"] == {"b": 200}


def test_shares_of_uninvolved_project_keep_consent_and_responsibility(
    ledger, reimb, merger
):
    # An invoice shared by the merged source a and an unrelated project c:
    # only a's share moves; c's confirmation stays valid and is not reset.
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    _realize(ledger, "c2", 1000)
    invoice = _invoice(
        reimb,
        [
            {"project_id": "a", "share_cents": 300},
            {"project_id": "c2", "share_cents": 200},
        ],
        source="inv",
        number="V-1",
    ).body
    _consent_all(reimb, invoice)
    assert reimb.get_invoice("c", invoice["id"])["payable"] is True

    merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )

    view = reimb.get_invoice("c", invoice["id"])
    by_original = {s["project_id"]: s for s in view["shares"]}
    # c2's share is untouched: same responsible project, consent preserved.
    assert by_original["c2"]["responsible_project_id"] == "c2"
    assert by_original["c2"]["consented"] is True
    # a's share moved to b and its old consent is void.
    assert by_original["a"]["responsible_project_id"] == "b"
    assert by_original["a"]["consented"] is False
    assert view["fully_consented"] is False
    assert view["missing_consent_projects"] == ["b"]

    # Only b re-confirms; c2 is not asked to confirm again.
    _consent(reimb, invoice, "b", "reconfirm-b")
    view = reimb.get_invoice("c", invoice["id"])
    assert view["fully_consented"] is True
    assert view["payable"] is True
    # The combined judgement groups a's transferred share under b while c2
    # still spends its own money separately.
    assert view["reserved_cents_by_project"] == {"b": 300, "c2": 200}


def test_registration_order_and_whole_invoice_rules_survive_merger(
    ledger, reimb, merger
):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    first = _invoice(
        reimb,
        [{"project_id": "a", "share_cents": 800}],
        source="inv1",
        number="V-1",
    ).body
    second = _invoice(
        reimb,
        [{"project_id": "a", "share_cents": 700}],
        source="inv2",
        number="V-2",
    ).body
    third = _invoice(
        reimb,
        [{"project_id": "b", "share_cents": 900}],
        source="inv3",
        number="V-3",
    ).body
    for invoice in (first, second, third):
        _consent_all(reimb, invoice)

    merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )
    for invoice in (first, second):
        _consent(reimb, invoice, "b", f"b-{invoice['id']}")

    overview = reimb.invoice_overview("c")
    by_number = {i["vendor_invoice_number"]: i for i in overview["invoices"]}
    # b now has 2000 total; first reserves 800, second needs 700 of 1200 left
    # and fits, third needs 900 of 500 left and is skipped as a whole.
    assert by_number["V-1"]["payable"] is True
    assert by_number["V-2"]["payable"] is True
    assert by_number["V-3"]["payable"] is False
    assert by_number["V-3"]["not_payable_reason"] == (
        "insufficient_project_funds"
    )
    assert by_number["V-3"]["shortfall_cents_by_project"] == {"b": 400}
    # Registration order is unchanged.
    assert [i["vendor_invoice_number"] for i in overview["invoices"]] == [
        "V-1",
        "V-2",
        "V-3",
    ]


# ----------------------------------------------------------------------
# In-flight payments across a merger
# ----------------------------------------------------------------------
def test_in_flight_payment_keeps_instruction_and_successor_assumes_hold(
    ledger, reimb, merger
):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    invoice = _invoice(
        reimb,
        [
            {"project_id": "a", "share_cents": 600},
            {"project_id": "b", "share_cents": 400},
        ],
        source="inv",
        number="V-1",
    ).body
    _consent_all(reimb, invoice)
    number = reimb.submit_payment(
        "c", invoice["id"], source_event_id="pay"
    ).body["instruction_number"]

    merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )

    # The instruction itself is never changed or re-sent.
    instruction = reimb.get_payment_instruction("c", number)
    assert instruction["status"] == "pending"
    assert instruction["instruction_number"] == number
    assert instruction["amount_cents"] == 1000
    # Allocations keep the original shareholder but show b responsible twice.
    by_original = {
        a["project_id"]: a["responsible_project_id"]
        for a in instruction["allocations"]
    }
    assert by_original == {"a": "b", "b": "b"}

    # A second submission is still blocked by the in-flight instruction.
    with pytest.raises(LedgerError) as exc:
        reimb.submit_payment(
            "c", invoice["id"], source_event_id="pay-duplicate"
        )
    assert exc.value.code == "invoice_payment_in_flight"

    # The successor carries the full 1000 hold, counted once.
    funds = {
        p["project_id"]: p
        for p in reimb.project_funds("c")["projects"]
    }
    assert funds["b"]["in_flight_cents"] == 1000
    assert funds["a"]["in_flight_cents"] == 0

    # Success settles the hold against the successor.
    reimb.deliver_payment_result(
        "c", number, outcome="succeeded", result_event_id="ok"
    )
    funds = {
        p["project_id"]: p
        for p in reimb.project_funds("c")["projects"]
    }
    assert funds["b"]["paid_cents"] == 1000
    assert funds["b"]["in_flight_cents"] == 0
    assert funds["a"]["paid_cents"] == 0


def test_failed_in_flight_payment_releases_hold_and_requires_reconfirm(
    ledger, reimb, merger
):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    invoice = _invoice(
        reimb,
        [{"project_id": "a", "share_cents": 600}],
        source="inv",
        number="V-1",
    ).body
    _consent_all(reimb, invoice)
    first_number = reimb.submit_payment(
        "c", invoice["id"], source_event_id="pay1"
    ).body["instruction_number"]

    merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )
    # Failure releases the whole hold onto the successor.
    reimb.deliver_payment_result(
        "c", first_number, outcome="failed", result_event_id="no"
    )
    funds = {
        p["project_id"]: p
        for p in reimb.project_funds("c")["projects"]
    }
    assert funds["b"]["in_flight_cents"] == 0

    view = reimb.get_invoice("c", invoice["id"])
    assert view["state"] == "unpaid"
    assert view["payable"] is False
    assert view["not_payable_reason"] == "consent_pending"

    # The successor must reconfirm before the invoice can be sent again.
    with pytest.raises(LedgerError) as exc:
        reimb.submit_payment(
            "c", invoice["id"], source_event_id="pay-too-early"
        )
    assert exc.value.code == "invoice_not_payable"

    _consent(reimb, invoice, "b", "reconfirm-b")
    second = reimb.submit_payment(
        "c", invoice["id"], source_event_id="pay2"
    ).body
    assert second["attempt_no"] == 2
    assert second["instruction_number"] != first_number
    reimb.deliver_payment_result(
        "c",
        second["instruction_number"],
        outcome="succeeded",
        result_event_id="ok2",
    )
    funds = {
        p["project_id"]: p
        for p in reimb.project_funds("c")["projects"]
    }
    assert funds["b"]["paid_cents"] == 600


def test_succeeded_invoice_keeps_historical_consent_and_is_untouched(
    ledger, reimb, merger
):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    invoice = _invoice(
        reimb,
        [{"project_id": "a", "share_cents": 600}],
        source="inv",
        number="V-1",
    ).body
    _consent_all(reimb, invoice)
    number = reimb.submit_payment(
        "c", invoice["id"], source_event_id="pay"
    ).body["instruction_number"]
    reimb.deliver_payment_result(
        "c", number, outcome="succeeded", result_event_id="ok"
    )

    merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )
    view = reimb.get_invoice("c", invoice["id"])
    assert view["state"] == "paid"
    # The historical consent record is preserved.
    assert view["consents"] == {"a": True}
    assert view["shares"][0]["responsible_project_id"] == "b"

    with sqlite3.connect(merger.db_path) as conn:
        consent = conn.execute(
            "SELECT consented FROM invoice_consents WHERE invoice_id = ?",
            (invoice["id"],),
        ).fetchone()
    assert consent[0] == 1


# ----------------------------------------------------------------------
# Read models and traceability
# ----------------------------------------------------------------------
def test_merger_listing_and_responsibility_view(ledger, reimb, merger):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    invoice = _invoice(
        reimb,
        [{"project_id": "a", "share_cents": 300}],
        source="inv",
        number="V-1",
    ).body
    _consent_all(reimb, invoice)
    created = merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )

    listing = merger.list_mergers("c")
    assert listing["merger_count"] == 1
    assert listing["mergers"][0]["id"] == created.body["id"]
    fetched = merger.get_merger("c", created.body["id"])
    assert fetched["source_project_id"] == "a"
    assert fetched["successor_project_id"] == "b"
    assert fetched["transferred_invoice_shares"] == [
        {
            "invoice_id": invoice["id"],
            "original_project_id": "a",
            "responsible_project_id": "b",
            "share_cents": 300,
        }
    ]

    view = merger.project_responsibility("c")
    by_id = {p["project_id"]: p for p in view["projects"]}
    assert by_id["a"]["status"] == "merged"
    assert by_id["a"]["is_active"] is False
    assert by_id["a"]["merged_into_project_id"] == "b"
    assert by_id["a"]["current_responsible_project_id"] == "b"
    assert by_id["b"]["is_active"] is True
    assert by_id["b"]["current_responsible_project_id"] == "b"

    projects = ledger.list_projects("c")
    assert {p["id"]: p["is_active"] for p in projects} == {
        "a": False,
        "b": True,
    }


def test_original_records_remain_traceable_after_merger(
    ledger, reimb, merger
):
    _realize(ledger, "a", 1000)
    _realize(ledger, "b", 1000)
    invoice = _invoice(
        reimb,
        [{"project_id": "a", "share_cents": 600}],
        source="inv",
        number="V-1",
    ).body
    _consent_all(reimb, invoice)
    number = reimb.submit_payment(
        "c", invoice["id"], source_event_id="pay"
    ).body["instruction_number"]
    reimb.deliver_payment_result(
        "c", number, outcome="succeeded", result_event_id="ok"
    )
    merger.merge_projects(
        "c",
        source_event_id="m1",
        source_project_id="a",
        successor_project_id="b",
    )

    # The original donation, its grant and the invoice share all retain the
    # original project id while exposing the current responsible project.
    donations = ledger.list_donations("c")
    donation = next(d for d in donations if d["source_event_id"] == "don-a")
    assert donation["project_id"] == "a"

    with sqlite3.connect(merger.db_path) as conn:
        share = conn.execute(
            "SELECT project_id, responsible_project_id FROM invoice_shares "
            "WHERE invoice_id = ?",
            (invoice["id"],),
        ).fetchone()
    assert tuple(share) == ("a", "b")

    instruction = reimb.get_payment_instruction("c", number)
    assert instruction["allocations"][0]["project_id"] == "a"
    assert instruction["allocations"][0]["responsible_project_id"] == "b"
