#!/usr/bin/env python3
"""Reproducible small ledger for the rolling matching backend.

This demo intentionally calls ``LedgerService`` directly so it can run with
only the Python standard library + SQLite:

    python3 scripts/demo_ledger.py

Install requirements and run pytest to exercise the FastAPI HTTP layer:

    pip install -r requirements.txt
    pytest

The script recreates demo_ledger.db on every run and prints:

* acceptance-order provisional allocation;
* settling only one ready project;
* a refund without reversal moving provisional match to a waiting project;
* a later refund causing an order-level reversal and moving another waitlist;
* immutable old grant batches;
* idempotent retry / changed-parameter rejection.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import init_db  # noqa: E402
from app.service import CommandResult, LedgerError, LedgerService  # noqa: E402

DB_PATH = ROOT / "demo_ledger.db"
for suffix in ("", "-wal", "-shm"):
    Path(str(DB_PATH) + suffix).unlink(missing_ok=True)

init_db(DB_PATH)
ledger = LedgerService(str(DB_PATH))


def cents(value: int) -> str:
    return f"{value:>4}¢"


def created(result: CommandResult) -> dict[str, Any]:
    assert result.status_code == 201, result
    return result.body


def print_allocation(title: str, view: dict[str, Any]) -> None:
    print(f"\n== {title} ==")
    pool = view["pool"]
    print(
        "pool: "
        f"limit={cents(pool['total_pool_limit_cents'])} "
        f"net_granted={cents(pool['net_granted_cents'])} "
        f"actual_remaining={cents(pool['actual_pool_remaining_cents'])} "
        f"provisional={cents(pool['provisional_match_cents'])} "
        f"unallocated={cents(pool['unallocated_after_provisional_cents'])}"
    )
    print(
        "order don   donor  project      orig refnd remain grant net uncov prov wait"
    )
    for i in view["items"]:
        print(
            f"{i['accepted_order']:>5} "
            f"{i['source_event_id']:<4} "
            f"{i['donor_id']:<6} "
            f"{i['project_id']:<12} "
            f"{i['donation_amount_cents']:>4} "
            f"{i['refunded_cents']:>4} "
            f"{i['remaining_donation_cents']:>6} "
            f"{i['gross_granted_cents']:>5} "
            f"{i['net_granted_cents']:>3} "
            f"{i['ungranted_balance_cents']:>5} "
            f"{i['provisional_match_cents']:>4} "
            f"{i['waitlisted_cents']:>4}"
        )
    print("project totals:")
    for p in view["projects"]:
        print(
            f"  {p['project_id']:<12} "
            f"actual_net={p['net_granted_cents']:>4}¢ "
            f"provisional={p['provisional_match_cents']:>4}¢ "
            f"waitlisted={p['waitlisted_cents']:>4}¢"
        )


def expect_conflict(fn: Callable[[], Any], code: str) -> LedgerError:
    try:
        fn()
    except LedgerError as exc:
        assert exc.code == code, (exc.code, exc.message)
        return exc
    raise AssertionError(f"Expected conflict {code}")


def main() -> None:
    created(
        ledger.create_campaign(
            campaign_id="oss-2026",
            name="Open Source Foundation Demo",
            total_pool_limit_cents=1000,
            donor_match_cap_cents=1000,
        )
    )
    for pid, name in [
        ("docs", "Documentation"),
        ("kernel", "Kernel"),
        ("translation", "Translation"),
    ]:
        created(
            ledger.create_project(
                "oss-2026", project_id=pid, name=name
            )
        )

    # Acceptance order is don-1, don-2, don-3.
    created(
        ledger.create_donation(
            "oss-2026",
            source_event_id="don-1",
            donor_id="alice",
            project_id="docs",
            amount_cents=600,
        )
    )
    created(
        ledger.create_donation(
            "oss-2026",
            source_event_id="don-2",
            donor_id="bob",
            project_id="kernel",
            amount_cents=600,
        )
    )

    view = ledger.provisional("oss-2026")
    print_allocation("1. Donations accepted: pool gives don-2 only 400¢", view)
    assert [i["provisional_match_cents"] for i in view["items"]] == [600, 400]

    s1 = created(
        ledger.settle_project(
            "oss-2026", "kernel", source_event_id="set-kernel-1"
        )
    )
    print(
        "\n2. Kernel prepared and settled only itself: "
        f"settlement #{s1['id']} granted {s1['total_amount_cents']}¢ "
        "against don-2."
    )
    assert s1["total_amount_cents"] == 400

    view = ledger.provisional("oss-2026")
    print_allocation(
        "3. After actual kernel grant: docs still first; kernel waits", view
    )
    by_source = {i["source_event_id"]: i for i in view["items"]}
    assert by_source["don-2"]["provisional_match_cents"] == 0

    r1 = created(
        ledger.create_refund(
            "oss-2026",
            source_event_id="ref-1",
            donation_source_event_id="don-1",
            amount_cents=100,
            reason="small correction; no grant on don-1",
        )
    )
    assert r1["actual_reversal"] is None
    print(
        "\n4. Refund ref-1 = 100¢ against don-1 causes NO actual reversal."
    )

    view = ledger.provisional("oss-2026")
    print_allocation(
        "5. Refund shrinks earlier order, so waiting kernel gets 100¢ provisional",
        view,
    )
    by_source = {i["source_event_id"]: i for i in view["items"]}
    assert by_source["don-1"]["provisional_match_cents"] == 500
    assert by_source["don-2"]["provisional_match_cents"] == 100

    # Second partial refund completes the don-1 refund.
    created(
        ledger.create_refund(
            "oss-2026",
            source_event_id="ref-2",
            donation_source_event_id="don-1",
            amount_cents=500,
        )
    )
    view = ledger.provisional("oss-2026")
    by_source = {i["source_event_id"]: i for i in view["items"]}
    assert by_source["don-2"]["provisional_match_cents"] == 200
    print(
        "\n6. Second refund ref-2 = 500¢ fully refunds don-1; "
        "kernel provisional rises to 200¢."
    )

    over_refund = expect_conflict(
        lambda: ledger.create_refund(
            "oss-2026",
            source_event_id="ref-over",
            donation_source_event_id="don-1",
            amount_cents=1,
        ),
        "refund_exceeds_donation",
    )
    print(
        "7. Another 1¢ refund is rejected: "
        f"{over_refund.details['maximum_refund_cents']}¢ refundable."
    )

    retry = ledger.create_refund(
        "oss-2026",
        source_event_id="ref-1",
        donation_source_event_id="don-1",
        amount_cents=100,
        reason="small correction; no grant on don-1",
    )
    assert retry.replayed is True
    assert retry.body["cumulative_refunded_cents"] == 100
    print(
        "8. Same ref-1 parameters retry returns the ORIGINAL response "
        "(replayed=true, cumulative_refunded=100¢ at that event), not "
        "today's 600¢ total. HTTP also sets Idempotency-Replayed: true."
    )

    mismatch = expect_conflict(
        lambda: ledger.create_refund(
            "oss-2026",
            source_event_id="ref-1",
            donation_source_event_id="don-1",
            amount_cents=101,
        ),
        "idempotency_parameter_mismatch",
    )
    assert mismatch.status_code == 409
    print("9. Same ref-1 number with changed amount is rejected with 409.")

    s2 = created(
        ledger.settle_project(
            "oss-2026", "kernel", source_event_id="set-kernel-2"
        )
    )
    assert s2["total_amount_cents"] == 200
    print(
        "\n10. Kernel's second actual grant set-kernel-2 = "
        f"{s2['total_amount_cents']}¢. First grant history is not rewritten."
    )

    created(
        ledger.create_donation(
            "oss-2026",
            source_event_id="don-3",
            donor_id="carol",
            project_id="translation",
            amount_cents=500,
        )
    )
    view = ledger.provisional("oss-2026")
    by_source = {i["source_event_id"]: i for i in view["items"]}
    assert by_source["don-3"]["provisional_match_cents"] == 400
    print(
        "11. New translation donation don-3=500¢ gets 400¢ provisional "
        "because 600¢ of pool is actually granted."
    )

    r3 = created(
        ledger.create_refund(
            "oss-2026",
            source_event_id="ref-3",
            donation_source_event_id="don-2",
            amount_cents=250,
            reason="partial refund after kernel grants",
        )
    )
    reversal = r3["actual_reversal"]
    assert reversal and reversal["amount_cents"] == 250
    print(
        "\n12. Refund don-2 by 250¢ leaves 350¢ real money against 600¢ "
        f"net old grants, so actual reversal rev-#{reversal['id']} = 250¢."
    )

    view = ledger.provisional("oss-2026")
    print_allocation(
        "13. Reversal releases pool: translation provisional rises 400→500",
        view,
    )
    by_source = {i["source_event_id"]: i for i in view["items"]}
    assert by_source["don-3"]["provisional_match_cents"] == 500
    assert ledger.list_project_grants("oss-2026", "translation")[
        "grant_batch_count"
    ] == 0

    grants = ledger.list_project_grants("oss-2026", "kernel")
    print("\n== Kernel immutable actual grant batches ==")
    for batch in grants["settlements"]:
        details = ", ".join(
            f"{it['donation_source_event_id']}={it['amount_cents']}¢"
            for it in batch["items"]
        )
        print(
            f"  {batch['source_event_id']} "
            f"settlement_id={batch['id']} total={batch['total_amount_cents']}¢ "
            f"[{details}]"
        )
    assert [b["total_amount_cents"] for b in grants["settlements"]] == [
        400,
        200,
    ]

    reversals = ledger.list_reversals(
        "oss-2026", donation_source_event_id="don-2"
    )
    print("\n== don-2 original-order reversals ==")
    for rev in reversals["reversals"]:
        print(
            f"  reversal_id={rev['id']} "
            f"refund={rev['refund_source_event_id']} "
            f"amount={rev['amount_cents']}¢"
        )
    assert reversals["total_reversed_cents"] == 250

    print(
        "\nPASS: demo completed. Inspect "
        f"{DB_PATH} with sqlite3 if desired.\n"
    )


if __name__ == "__main__":
    main()
