from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from app.db import init_db
from app.service import LedgerError, LedgerService


def test_concurrent_identical_settlement_creates_one_batch(tmp_path):
    db_path = tmp_path / "concurrent-idem.db"
    init_db(db_path)
    svc = LedgerService(str(db_path))
    svc.create_campaign(
        campaign_id="c",
        name="C",
        total_pool_limit_cents=1000,
        donor_match_cap_cents=100000,
    )
    svc.create_project("c", project_id="p", name="P")
    svc.create_donation(
        "c",
        source_event_id="d",
        donor_id="donor",
        project_id="p",
        amount_cents=600,
    )

    barrier = threading.Barrier(16)

    def settle():
        barrier.wait()
        return svc.settle_project("c", "p", source_event_id="same-settle")

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: settle(), range(16)))

    body_ids = {r.body["id"] for r in results}
    assert len(body_ids) == 1
    assert sum(1 for r in results if not r.replayed) == 1
    assert sum(1 for r in results if r.replayed) == 15

    import sqlite3

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM grant_items").fetchone()[0] == 1
        assert (
            conn.execute("SELECT SUM(amount_cents) FROM grant_items").fetchone()[0]
            == 600
        )


def test_concurrent_project_settlements_do_not_overdraw_pool(tmp_path):
    db_path = tmp_path / "concurrent-pool.db"
    init_db(db_path)
    svc = LedgerService(str(db_path))
    svc.create_campaign(
        campaign_id="c",
        name="C",
        total_pool_limit_cents=1000,
        donor_match_cap_cents=1000,
    )

    count = 10
    for i in range(count):
        pid = f"p{i}"
        svc.create_project("c", project_id=pid, name=pid.upper())
        svc.create_donation(
            "c",
            source_event_id=f"d{i}",
            donor_id=f"donor{i}",
            project_id=pid,
            amount_cents=100,
        )

    barrier = threading.Barrier(count)

    def settle_one(i: int):
        barrier.wait()
        return svc.settle_project(
            "c", f"p{i}", source_event_id=f"settle-{i}"
        )

    with ThreadPoolExecutor(max_workers=count) as pool:
        results = list(pool.map(settle_one, range(count)))

    assert all(r.body["total_amount_cents"] == 100 for r in results)

    import sqlite3

    with sqlite3.connect(db_path) as conn:
        total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM grant_items"
        ).fetchone()[0]
        assert total == 1000
        assert conn.execute("SELECT COUNT(*) FROM grant_items").fetchone()[0] == 10

    view = svc.provisional("c")
    assert view["pool"]["net_granted_cents"] == 1000
    assert view["pool"]["actual_pool_remaining_cents"] == 0
    assert all(i["provisional_match_cents"] == 0 for i in view["items"])


def test_concurrent_refunds_cannot_exceed_original_donation(tmp_path):
    db_path = tmp_path / "concurrent-refunds.db"
    init_db(db_path)
    svc = LedgerService(str(db_path))
    svc.create_campaign(
        campaign_id="c",
        name="C",
        total_pool_limit_cents=100000,
        donor_match_cap_cents=100000,
    )
    svc.create_project("c", project_id="p", name="P")
    svc.create_donation(
        "c",
        source_event_id="d",
        donor_id="donor",
        project_id="p",
        amount_cents=1000,
    )

    workers = 20
    barrier = threading.Barrier(workers)

    def refund_one(i: int):
        barrier.wait()
        try:
            return (
                "ok",
                svc.create_refund(
                    "c",
                    source_event_id=f"r{i}",
                    donation_source_event_id="d",
                    amount_cents=100,
                ),
            )
        except LedgerError as exc:
            return ("error", exc.code)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(refund_one, range(workers)))

    successes = [o for o in outcomes if o[0] == "ok"]
    over_refunds = [o for o in outcomes if o == ("error", "refund_exceeds_donation")]
    assert len(successes) == 10
    assert len(over_refunds) == 10

    import sqlite3

    with sqlite3.connect(db_path) as conn:
        total_refunded = conn.execute(
            "SELECT SUM(amount_cents) FROM refunds WHERE donation_id = 1"
        ).fetchone()[0]
        assert total_refunded == 1000
        assert conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0] == 10


def test_concurrent_donor_cap_is_not_overdrawn(tmp_path):
    db_path = tmp_path / "concurrent-donor.db"
    init_db(db_path)
    svc = LedgerService(str(db_path))
    svc.create_campaign(
        campaign_id="c",
        name="C",
        total_pool_limit_cents=100000,
        donor_match_cap_cents=1000,
    )

    count = 10
    for i in range(count):
        pid = f"p{i}"
        svc.create_project("c", project_id=pid, name=pid.upper())
        svc.create_donation(
            "c",
            source_event_id=f"d{i}",
            donor_id="same-donor",
            project_id=pid,
            amount_cents=100,
        )

    barrier = threading.Barrier(count)

    def settle_one(i: int):
        barrier.wait()
        return svc.settle_project(
            "c", f"p{i}", source_event_id=f"s{i}"
        )

    with ThreadPoolExecutor(max_workers=count) as pool:
        results = list(pool.map(settle_one, range(count)))

    # Serialized allocation + settlement gives exactly the donor cap.  Depending
    # on commit order, some source events can be no-match settlements.
    total_granted = sum(r.body["total_amount_cents"] for r in results)
    assert total_granted == 1000

    view = svc.provisional("c")
    donor = view["donors"][0]
    assert donor["net_granted_cents"] == 1000
    assert donor["actual_cap_remaining_cents"] == 0
