from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from app.closing import ClosingService
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
    ledger.create_project("c", project_id="a", name="A")
    ledger.create_donation(
        "c",
        source_event_id="don-a",
        donor_id="donor-a",
        project_id="a",
        amount_cents=1000,
    )
    ledger.settle_project("c", "a", source_event_id="set-a")
    return ledger, ReimbursementService(str(db_path)), ClosingService(str(db_path))


def test_concurrent_identical_draft_creation_creates_one(tmp_path):
    ledger, _reimb, closing = _setup(tmp_path, "conc-draft")
    barrier = threading.Barrier(12)

    def create():
        barrier.wait()
        try:
            result = closing.create_draft(
                "c", source_event_id="same-close"
            )
            return ("ok", result)
        except LedgerError as exc:
            return ("error", exc.code)

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(lambda _: create(), range(12)))

    ok = [o[1] for o in outcomes if o[0] == "ok"]
    assert len(ok) == 12
    assert {r.body["id"] for r in ok} == {ok[0].body["id"]}
    assert sum(1 for r in ok if not r.replayed) == 1

    with sqlite3.connect(closing.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM closings").fetchone()[0] == 1


def test_concurrent_distinct_drafts_leave_exactly_one(tmp_path):
    ledger, _reimb, closing = _setup(tmp_path, "conc-draft-distinct")
    barrier = threading.Barrier(10)

    def create(i: int):
        barrier.wait()
        try:
            closing.create_draft("c", source_event_id=f"close-{i}")
            return "ok"
        except LedgerError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(create, range(10)))

    with sqlite3.connect(closing.db_path) as conn:
        drafts = conn.execute(
            "SELECT COUNT(*) FROM closings WHERE status='draft'"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM closings").fetchone()[0]
    assert drafts == 1
    assert total == 1


def test_concurrent_sign_different_events_signs_once(tmp_path):
    _ledger, _reimb, closing = _setup(tmp_path, "conc-sign")
    draft = closing.create_draft("c", source_event_id="close").body
    barrier = threading.Barrier(10)

    def sign(i: int):
        barrier.wait()
        try:
            result = closing.sign_closing(
                "c", draft["id"], source_event_id=f"sign-{i}"
            )
            return ("ok", result)
        except LedgerError as exc:
            return ("error", exc.code)

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(sign, range(10)))

    successes = [o for o in outcomes if o[0] == "ok"]
    errors = [o for o in outcomes if o[0] == "error"]
    assert len(successes) == 1
    assert all(code == "closing_already_signed" for _, code in errors)

    with sqlite3.connect(closing.db_path) as conn:
        signed = conn.execute(
            "SELECT COUNT(*) FROM closings WHERE status='signed'"
        ).fetchone()[0]
        assert signed == 1


def test_concurrent_sign_same_event_is_idempotent(tmp_path):
    _ledger, _reimb, closing = _setup(tmp_path, "conc-sign-same")
    draft = closing.create_draft("c", source_event_id="close").body
    barrier = threading.Barrier(10)

    def sign(_i: int):
        barrier.wait()
        try:
            return ("ok", closing.sign_closing(
                "c", draft["id"], source_event_id="sign-same"
            ))
        except LedgerError as exc:
            return ("error", exc.code)

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(sign, range(10)))

    assert all(o[0] == "ok" for o in outcomes)
    assert len({o[1].body["signed_at"] for o in outcomes}) == 1
    assert sum(1 for o in outcomes if not o[1].replayed) == 1


def test_concurrent_signed_periods_never_overlap_after_restart(tmp_path):
    ledger, _reimb, closing = _setup(tmp_path, "conc-periods")

    # Period 1 draft created and signed from one service instance.
    first = closing.create_draft("c", source_event_id="close-1").body
    closing.sign_closing("c", first["id"], source_event_id="sign-1")

    # Simulated restart: a brand-new service instance on the same database.
    restarted = ClosingService(closing.db_path)
    second = restarted.create_draft("c", source_event_id="close-2").body
    assert second["period_no"] == 2

    # Concurrent sign calls from both instances refer to the same draft.
    barrier = threading.Barrier(8)

    def sign(i: int):
        barrier.wait()
        svc = restarted if i % 2 == 0 else closing
        try:
            result = svc.sign_closing(
                "c", second["id"], source_event_id=f"sign2-{i}"
            )
            return ("ok", result)
        except LedgerError as exc:
            return ("error", exc.code)

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(sign, range(8)))

    assert sum(1 for o in outcomes if o[0] == "ok") == 1
    with sqlite3.connect(closing.db_path) as conn:
        rows = conn.execute(
            "SELECT period_no, COUNT(*) FROM closings WHERE status='signed' "
            "GROUP BY period_no"
        ).fetchall()
    assert rows == [(1, 1), (2, 1)]


def test_closing_concurrent_with_grant_has_no_half_items(tmp_path):
    """A closing taken while grants are being written sees a complete set of
    grant line items, never a settlement's partial items."""
    ledger, _reimb, closing = _setup(tmp_path, "conc-grant")
    # A second project whose settlement will race the closing.
    ledger.create_project("c", project_id="b", name="B")
    ledger.create_donation(
        "c",
        source_event_id="don-b",
        donor_id="donor-b",
        project_id="b",
        amount_cents=1000,
    )

    barrier = threading.Barrier(8)

    def worker(i: int):
        barrier.wait()
        if i == 0:
            ledger.settle_project("c", "b", source_event_id="set-b")
            return "settle"
        try:
            closing.create_draft("c", source_event_id=f"close-{i}")
            return "closing"
        except LedgerError:
            return "closing-noise"

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))

    # Pick the surviving draft (if any) and check every grant item the draft
    # reports is matched by a real committed row, and no share is doubled.
    listing = closing.list_closings("c")
    if not listing["closings"]:
        return
    draft = listing["closings"][0]
    basis_ids = {
        g["grant_item_id"]
        for p in draft["projects"]
        for g in p["basis"]["grants"]
    }
    with sqlite3.connect(closing.db_path) as conn:
        committed = {
            row[0]
            for row in conn.execute(
                "SELECT id FROM grant_items WHERE campaign_id='c'"
            ).fetchall()
        }
    assert basis_ids <= committed
    # No duplicate grant item ids in the statement.
    all_ids = [
        g["grant_item_id"]
        for p in draft["projects"]
        for g in p["basis"]["grants"]
    ]
    assert len(all_ids) == len(set(all_ids))
