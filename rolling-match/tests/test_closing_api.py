from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "closing-api.db"


def _client(db_path, bank_path=None) -> TestClient:
    return TestClient(create_app(db_path, bank_path))


def seed(client: TestClient) -> None:
    r = client.post(
        "/api/v1/campaigns",
        json={
            "campaign_id": "c",
            "name": "Closing API",
            "total_pool_limit_cents": 1_000_000,
            "donor_match_cap_cents": 1_000_000,
        },
    )
    assert r.status_code == 201, r.text
    for pid in ("a", "b"):
        client.post(
            "/api/v1/campaigns/c/projects",
            json={"project_id": pid, "name": pid.upper()},
        )
        client.post(
            "/api/v1/campaigns/c/donations",
            json={
                "source_event_id": f"don-{pid}",
                "donor_id": f"donor-{pid}",
                "project_id": pid,
                "amount_cents": 1000,
            },
        )
        r = client.post(
            f"/api/v1/campaigns/c/projects/{pid}/settlements",
            json={"source_event_id": f"set-{pid}"},
        )
        assert r.status_code == 201, r.text


def _joint_invoice(client: TestClient) -> dict:
    invoice = client.post(
        "/api/v1/campaigns/c/invoices",
        json={
            "source_event_id": "inv",
            "payee": "Vendor Co",
            "vendor_invoice_number": "V-2026-001",
            "shares": [
                {"project_id": "a", "share_cents": 600},
                {"project_id": "b", "share_cents": 400},
            ],
        },
    ).json()
    for pid in ("a", "b"):
        client.post(
            f"/api/v1/campaigns/c/invoices/{invoice['id']}/consents",
            json={
                "source_event_id": f"consent-{pid}",
                "project_id": pid,
                "consented": True,
            },
        )
    return invoice


def test_http_closing_flow_across_instructions(db_path):
    with _client(db_path) as client:
        seed(client)
        invoice = _joint_invoice(client)
        number = client.post(
            f"/api/v1/campaigns/c/invoices/{invoice['id']}/payments",
            json={"source_event_id": "pay"},
        ).json()["instruction_number"]

        # Period 1: the joint payment is an outstanding hold.
        first = client.post(
            "/api/v1/campaigns/c/closings",
            json={"source_event_id": "close-1"},
        )
        assert first.status_code == 201, first.text
        body = first.json()
        assert body["period_no"] == 1
        assert body["status"] == "draft"
        assert body["signable"] is True
        assert [h["instruction_number"] for h in body["payments"][
            "outstanding_in_flight"
        ]] == [number]
        by_project = {p["project_id"]: p for p in body["projects"]}
        assert by_project["a"]["closing_book_balance_cents"] == 1000
        assert by_project["a"]["outstanding_in_flight_cents"] == 600
        assert by_project["a"]["available_after_in_flight_cents"] == 400

        signed = client.post(
            f"/api/v1/campaigns/c/closings/{body['id']}/sign",
            json={"source_event_id": "sign-1"},
        )
        assert signed.status_code == 200, signed.text
        assert signed.json()["status"] == "signed"

        # Bank result arrives after the signed statement.
        client.post(
            f"/api/v1/campaigns/c/payments/{number}/results",
            json={"outcome": "succeeded", "result_event_id": "bank-ok"},
        )

        # Period 2: same instruction is this period's expense, not again.
        second = client.post(
            "/api/v1/campaigns/c/closings",
            json={"source_event_id": "close-2"},
        ).json()
        assert second["period_no"] == 2
        assert second["opening_basis_closing_id"] == body["id"]
        paid = second["payments"]["bank_confirmed_in_period"]
        assert [p["instruction_number"] for p in paid] == [number]
        by_project = {p["project_id"]: p for p in second["projects"]}
        assert by_project["a"]["bank_confirmed_reimbursement_cents"] == 600
        assert by_project["a"]["closing_book_balance_cents"] == 400
        assert by_project["b"]["closing_book_balance_cents"] == 600

        # Listing and single fetch agree.
        listing = client.get("/api/v1/campaigns/c/closings").json()
        assert listing["signed_count"] == 1
        assert listing["draft_count"] == 1
        got = client.get(
            f"/api/v1/campaigns/c/closings/{body['id']}"
        ).json()
        assert got["status"] == "signed"
        # The signed statement is still the period-1 snapshot.
        assert got["payments"]["bank_confirmed_in_period"] == []


def test_http_draft_conflicts_and_discard(db_path):
    with _client(db_path) as client:
        seed(client)
        first = client.post(
            "/api/v1/campaigns/c/closings",
            json={"source_event_id": "close-1"},
        )
        assert first.status_code == 201
        replay = client.post(
            "/api/v1/campaigns/c/closings",
            json={"source_event_id": "close-1"},
        )
        assert replay.status_code == 200
        assert replay.headers.get("idempotency-replayed") == "true"
        assert replay.json()["id"] == first.json()["id"]

        conflict = client.post(
            "/api/v1/campaigns/c/closings",
            json={"source_event_id": "close-2"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "closing_draft_exists"
        assert conflict.json()["error"]["details"]["closing_id"] == (
            first.json()["id"]
        )

        discarded = client.post(
            f"/api/v1/campaigns/c/closings/{first.json()['id']}/discard",
            json={"source_event_id": "discard"},
        )
        assert discarded.status_code == 200
        assert discarded.json()["status"] == "discarded"

        late = client.post(
            f"/api/v1/campaigns/c/closings/{first.json()['id']}/sign",
            json={"source_event_id": "sign-late"},
        )
        assert late.status_code == 409
        assert late.json()["error"]["code"] == "closing_discarded"

        # A fresh cutoff is now allowed.
        fresh = client.post(
            "/api/v1/campaigns/c/closings",
            json={"source_event_id": "close-3"},
        )
        assert fresh.status_code == 201


def test_http_non_signable_draft_returns_blockers(db_path):
    with _client(db_path) as client:
        seed(client)
        invoice = _joint_invoice(client)
        number = client.post(
            f"/api/v1/campaigns/c/invoices/{invoice['id']}/payments",
            json={"source_event_id": "pay"},
        ).json()["instruction_number"]
        # Ledger says succeeded; the bank never got the instruction.
        client.post(
            f"/api/v1/campaigns/c/payments/{number}/results",
            json={"outcome": "succeeded", "result_event_id": "x"},
        )
        # Erase the bank's record to model a bank that cannot confirm it.
        import sqlite3

        bank_db = client.app.state.bank_db_path
        with sqlite3.connect(bank_db) as conn:
            conn.execute(
                "UPDATE bank_payments SET status='pending', result_at=NULL,"
                " result_event_id=NULL WHERE instruction_number=?",
                (number,),
            )
            conn.commit()

        draft = client.post(
            "/api/v1/campaigns/c/closings",
            json={"source_event_id": "close"},
        ).json()
        assert draft["signable"] is False
        assert any(
            b["code"] == "ledger_terminal_without_bank_terminal"
            for b in draft["blockers"]
        )

        blocked = client.post(
            f"/api/v1/campaigns/c/closings/{draft['id']}/sign",
            json={"source_event_id": "sign"},
        )
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "closing_not_signable"
        assert blocked.json()["error"]["details"]["blockers"]


def test_http_validation(db_path):
    with _client(db_path) as client:
        seed(client)
        bad = client.post(
            "/api/v1/campaigns/c/closings",
            json={"source_event_id": "close", "cutoff_at": "not-a-time"},
        )
        assert bad.status_code == 422
        assert bad.json()["error"]["code"] == "closing_invalid_cutoff"

        future = client.post(
            "/api/v1/campaigns/c/closings",
            json={
                "source_event_id": "close-future",
                "cutoff_at": "2999-01-01T00:00:00Z",
            },
        )
        assert future.status_code == 422
        assert future.json()["error"]["code"] == "closing_cutoff_in_future"

        missing = client.get("/api/v1/campaigns/c/closings/999")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "closing_not_found"

        no_campaign = client.get("/api/v1/campaigns/nope/closings")
        assert no_campaign.status_code == 404
