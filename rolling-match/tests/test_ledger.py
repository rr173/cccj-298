from __future__ import annotations

import sqlite3

import pytest

from app.db import init_db
from app.service import LedgerError, LedgerService


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "ledger.db"
    init_db(path)
    return str(path)


@pytest.fixture
def svc(db_path):
    return LedgerService(db_path)


@pytest.fixture
def ledger(svc):
    svc.create_campaign(
        campaign_id="c1",
        name="Test Campaign",
        total_pool_limit_cents=1000,
        donor_match_cap_cents=1000,
    )
    svc.create_project("c1", project_id="p1", name="One")
    svc.create_project("c1", project_id="p2", name="Two")
    svc.create_project("c1", project_id="p3", name="Three")
    return svc


def test_provisional_follows_acceptance_order_and_pool(ledger):
    ledger.create_donation(
        "c1",
        source_event_id="d1",
        donor_id="alice",
        project_id="p1",
        amount_cents=600,
    )
    ledger.create_donation(
        "c1",
        source_event_id="d2",
        donor_id="bob",
        project_id="p2",
        amount_cents=600,
    )

    view = ledger.provisional("c1")
    assert [i["provisional_match_cents"] for i in view["items"]] == [
        600,
        400,
    ]
    assert [i["waitlisted_cents"] for i in view["items"]] == [0, 200]
    assert view["items"][0]["pool_available_before_order_cents"] == 1000
    assert view["items"][1]["pool_available_before_order_cents"] == 400
    assert view["pool"]["provisional_match_cents"] == 1000
    assert view["pool"]["unallocated_after_provisional_cents"] == 0


def test_project_settlement_then_non_reversal_refund_moves_waitlist(ledger):
    ledger.create_donation(
        "c1",
        source_event_id="d1",
        donor_id="alice",
        project_id="p1",
        amount_cents=600,
    )
    ledger.create_donation(
        "c1",
        source_event_id="d2",
        donor_id="bob",
        project_id="p2",
        amount_cents=600,
    )

    settled = ledger.settle_project("c1", "p2", source_event_id="s1")
    assert settled.status_code == 201
    assert settled.body["total_amount_cents"] == 400
    assert settled.body["items"][0]["donation_source_event_id"] == "d2"

    after_settlement = ledger.provisional("c1")
    by_source = {i["source_event_id"]: i for i in after_settlement["items"]}
    assert by_source["d1"]["provisional_match_cents"] == 600
    assert by_source["d2"]["provisional_match_cents"] == 0
    assert by_source["d2"]["waitlisted_cents"] == 200

    refund = ledger.create_refund(
        "c1",
        source_event_id="r1",
        donation_source_event_id="d1",
        amount_cents=100,
    )
    assert refund.body["actual_reversal"] is None

    after_refund = ledger.provisional("c1")
    by_source = {i["source_event_id"]: i for i in after_refund["items"]}
    assert by_source["d1"]["remaining_donation_cents"] == 500
    assert by_source["d1"]["provisional_match_cents"] == 500
    assert by_source["d2"]["provisional_match_cents"] == 100
    assert by_source["d2"]["waitlisted_cents"] == 100

    projects = {p["project_id"]: p for p in after_refund["projects"]}
    assert projects["p2"]["provisional_match_cents"] == 100

    grants = ledger.list_project_grants("c1", "p2")
    assert grants["settlements"][0]["total_amount_cents"] == 400
    assert grants["gross_granted_cents"] == 400


def test_multiple_refunds_overrefund_and_actual_reversal(ledger):
    ledger.create_donation(
        "c1",
        source_event_id="d1",
        donor_id="alice",
        project_id="p1",
        amount_cents=600,
    )
    ledger.create_donation(
        "c1",
        source_event_id="d2",
        donor_id="bob",
        project_id="p2",
        amount_cents=600,
    )
    ledger.settle_project("c1", "p2", source_event_id="s1")
    ledger.create_refund(
        "c1",
        source_event_id="r1",
        donation_source_event_id="d1",
        amount_cents=600,
    )
    ledger.settle_project("c1", "p2", source_event_id="s2")

    ledger.create_donation(
        "c1",
        source_event_id="d3",
        donor_id="carol",
        project_id="p3",
        amount_cents=500,
    )
    before = ledger.provisional("c1")
    by_source = {i["source_event_id"]: i for i in before["items"]}
    assert by_source["d3"]["provisional_match_cents"] == 400

    # d2 originally 600 and has gross grants 400 + 200 = 600.
    refund = ledger.create_refund(
        "c1",
        source_event_id="r2",
        donation_source_event_id="d2",
        amount_cents=250,
    )
    assert refund.body["actual_reversal"]["amount_cents"] == 250

    after = ledger.provisional("c1")
    by_source = {i["source_event_id"]: i for i in after["items"]}
    assert by_source["d2"]["remaining_donation_cents"] == 350
    assert by_source["d2"]["net_granted_cents"] == 350
    assert by_source["d3"]["provisional_match_cents"] == 500
    # Released quota changes provisional allocation only; it must not create
    # an actual grant for the newly waiting project without settlement.
    assert ledger.list_project_grants("c1", "p3")["grant_batch_count"] == 0

    grants = ledger.list_project_grants("c1", "p2")
    assert [s["total_amount_cents"] for s in grants["settlements"]] == [
        400,
        200,
    ]
    reversals = ledger.list_reversals(
        "c1", donation_source_event_id="d2"
    )
    assert reversals["total_reversed_cents"] == 250
    assert reversals["donation"]["gross_granted_cents"] == 600

    with pytest.raises(LedgerError) as exc:
        ledger.create_refund(
            "c1",
            source_event_id="r3-too-much",
            donation_source_event_id="d2",
            amount_cents=351,
        )
    assert exc.value.code == "refund_exceeds_donation"
    assert exc.value.details["maximum_refund_cents"] == 350


def test_refund_cannot_exceed_donation_without_grants(svc):
    svc.create_campaign(
        campaign_id="c",
        name="C",
        total_pool_limit_cents=10000,
        donor_match_cap_cents=10000,
    )
    svc.create_project("c", project_id="p", name="P")
    svc.create_donation(
        "c",
        source_event_id="d",
        donor_id="donor",
        project_id="p",
        amount_cents=100,
    )
    svc.create_refund(
        "c", source_event_id="r1", donation_source_event_id="d", amount_cents=60
    )
    svc.create_refund(
        "c", source_event_id="r2", donation_source_event_id="d", amount_cents=40
    )
    with pytest.raises(LedgerError) as exc:
        svc.create_refund(
            "c", source_event_id="r3", donation_source_event_id="d", amount_cents=1
        )
    assert exc.value.details["maximum_refund_cents"] == 0


def test_donor_cap_reversal_releases_later_donor_order(svc):
    svc.create_campaign(
        campaign_id="c",
        name="C",
        total_pool_limit_cents=10000,
        donor_match_cap_cents=500,
    )
    svc.create_project("c", project_id="p1", name="P1")
    svc.create_project("c", project_id="p2", name="P2")
    svc.create_donation(
        "c",
        source_event_id="d1",
        donor_id="a",
        project_id="p1",
        amount_cents=600,
    )
    svc.create_donation(
        "c",
        source_event_id="d2",
        donor_id="a",
        project_id="p2",
        amount_cents=600,
    )

    view = svc.provisional("c")
    assert [i["provisional_match_cents"] for i in view["items"]] == [500, 0]

    svc.settle_project("c", "p1", source_event_id="s1")
    refund = svc.create_refund(
        "c",
        source_event_id="r1",
        donation_source_event_id="d1",
        amount_cents=200,
    )
    assert refund.body["actual_reversal"]["amount_cents"] == 100

    view = svc.provisional("c")
    assert [i["provisional_match_cents"] for i in view["items"]] == [0, 100]
    donor = view["donors"][0]
    assert donor["net_granted_cents"] == 400
    assert donor["provisional_match_cents"] == 100
    assert donor["cap_remaining_after_provisional_cents"] == 0


def test_idempotent_commands_return_original_and_reject_changed_params(ledger):
    payload = dict(
        campaign_id="c1",
        source_event_id="d-idem",
        donor_id="alice",
        project_id="p1",
        amount_cents=123,
    )
    first = ledger.create_donation(**payload)
    replay = ledger.create_donation(**payload)
    assert first.body["id"] == replay.body["id"]
    assert replay.replayed is True
    assert replay.body == first.body

    with pytest.raises(LedgerError) as exc:
        ledger.create_donation(**{**payload, "amount_cents": 124})
    assert exc.value.code == "idempotency_parameter_mismatch"

    refund_payload = dict(
        campaign_id="c1",
        source_event_id="r-idem",
        donation_source_event_id="d-idem",
        amount_cents=20,
    )
    r1 = ledger.create_refund(**refund_payload)
    r2 = ledger.create_refund(**refund_payload)
    assert r1.body["id"] == r2.body["id"]
    assert r2.body == r1.body
    with pytest.raises(LedgerError):
        ledger.create_refund(**{**refund_payload, "amount_cents": 21})

    s1 = ledger.settle_project("c1", "p1", source_event_id="s-idem")
    s2 = ledger.settle_project("c1", "p1", source_event_id="s-idem")
    assert s1.body["id"] == s2.body["id"]
    assert s2.body == s1.body
    with pytest.raises(LedgerError):
        ledger.settle_project("c1", "p2", source_event_id="s-idem")

    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM donations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM grant_items").fetchone()[0] == 1


def test_zero_match_settlement_is_recorded_and_idempotent(svc):
    svc.create_campaign(
        campaign_id="c",
        name="C",
        total_pool_limit_cents=0,
        donor_match_cap_cents=100,
    )
    svc.create_project("c", project_id="p", name="P")
    svc.create_donation(
        "c",
        source_event_id="d",
        donor_id="a",
        project_id="p",
        amount_cents=100,
    )
    first = svc.settle_project("c", "p", source_event_id="empty")
    assert first.status_code == 200
    assert first.body["status"] == "no_eligible_match"
    assert first.body["items"] == []

    replay = svc.settle_project("c", "p", source_event_id="empty")
    assert replay.replayed is True
    assert replay.body["id"] == first.body["id"]

    grants = svc.list_project_grants("c", "p")
    assert grants["grant_batch_count"] == 0
