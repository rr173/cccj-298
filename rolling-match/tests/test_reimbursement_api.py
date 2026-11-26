from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "api.db"


def _client(db_path, bank_path=None) -> TestClient:
    app = create_app(db_path, bank_path)
    return TestClient(app)


def seed(client: TestClient) -> None:
    r = client.post(
        "/api/v1/campaigns",
        json={
            "campaign_id": "c",
            "name": "Joint Reimbursement",
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


def register(client: TestClient) -> dict:
    r = client.post(
        "/api/v1/campaigns/c/invoices",
        json={
            "source_event_id": "inv-1",
            "payee": "Vendor Co",
            "vendor_invoice_number": "V-2026-001",
            "shares": [
                {"project_id": "a", "share_cents": 600},
                {"project_id": "b", "share_cents": 400},
            ],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def consent_all(client: TestClient, invoice_id: int) -> None:
    for pid in ("a", "b"):
        r = client.post(
            f"/api/v1/campaigns/c/invoices/{invoice_id}/consents",
            json={
                "source_event_id": f"consent-{pid}",
                "project_id": pid,
                "consented": True,
            },
        )
        assert r.status_code == 200, r.text


def test_full_http_reimbursement_flow(db_path):
    with _client(db_path) as client:
        seed(client)
        invoice = register(client)
        assert invoice["total_amount_cents"] == 1000
        assert invoice["payable"] is False
        assert invoice["not_payable_reason"] == "consent_pending"

        # Exact retry returns the same invoice and header.
        replay = client.post(
            "/api/v1/campaigns/c/invoices",
            json={
                "source_event_id": "inv-1",
                "payee": "Vendor Co",
                "vendor_invoice_number": "V-2026-001",
                "shares": [
                    {"project_id": "a", "share_cents": 600},
                    {"project_id": "b", "share_cents": 400},
                ],
            },
        )
        assert replay.status_code == 200
        assert replay.headers.get("idempotency-replayed") == "true"
        assert replay.json()["id"] == invoice["id"]

        # Retry with changed shares is a conflict, never a modification.
        changed = client.post(
            "/api/v1/campaigns/c/invoices",
            json={
                "source_event_id": "inv-1",
                "payee": "Vendor Co",
                "vendor_invoice_number": "V-2026-001",
                "shares": [{"project_id": "a", "share_cents": 500}],
            },
        )
        assert changed.status_code == 409

        consent_all(client, invoice["id"])
        view = client.get(
            f"/api/v1/campaigns/c/invoices/{invoice['id']}"
        ).json()
        assert view["payable"] is True

        funds = client.get("/api/v1/campaigns/c/reimbursement-funds").json()
        by_project = {p["project_id"]: p for p in funds["projects"]}
        assert by_project["a"]["reserved_cents"] == 600
        assert by_project["a"]["available_cents"] == 400

        submitted = client.post(
            f"/api/v1/campaigns/c/invoices/{invoice['id']}/payments",
            json={"source_event_id": "pay-1"},
        )
        assert submitted.status_code == 201, submitted.text
        number = submitted.json()["instruction_number"]

        # Finance can find the pending instruction and its bank acceptance.
        found = client.get(f"/api/v1/campaigns/c/payments/{number}")
        assert found.status_code == 200
        assert found.json()["status"] == "pending"
        assert found.json()["bank"]["status"] == "pending"
        assert found.json()["allocations"] == [
            {"project_id": "a", "share_cents": 600},
            {"project_id": "b", "share_cents": 400},
        ]

        # The standalone simulated bank was actually called.
        bank = client.get(f"/api/v1/simulated-bank/payments/{number}").json()
        assert bank["payment"]["status"] == "pending"

        result = client.post(
            f"/api/v1/campaigns/c/payments/{number}/results",
            json={"outcome": "succeeded", "result_event_id": "bank-ok-1"},
        )
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "succeeded"

        # Duplicate bank result is harmless.
        again = client.post(
            f"/api/v1/campaigns/c/payments/{number}/results",
            json={"outcome": "succeeded", "result_event_id": "bank-ok-1"},
        )
        assert again.status_code == 200
        assert again.headers.get("idempotency-replayed") == "true"

        # Contradictory terminal result cannot overwrite.
        contradiction = client.post(
            f"/api/v1/campaigns/c/payments/{number}/results",
            json={"outcome": "failed", "result_event_id": "bank-no"},
        )
        assert contradiction.status_code == 409

        paid_funds = client.get(
            "/api/v1/campaigns/c/reimbursement-funds"
        ).json()
        by_project = {p["project_id"]: p for p in paid_funds["projects"]}
        assert by_project["a"]["paid_cents"] == 600
        assert by_project["b"]["paid_cents"] == 400
        assert by_project["a"]["gap_cents"] == 0

        payments = client.get(
            f"/api/v1/campaigns/c/invoices/{invoice['id']}/payments"
        ).json()
        assert payments["payment_count"] == 1


def test_http_not_payable_and_failure_requeue(db_path):
    with _client(db_path) as client:
        seed(client)
        invoice = register(client)

        # Not all projects consented yet: finance cannot submit.
        client.post(
            f"/api/v1/campaigns/c/invoices/{invoice['id']}/consents",
            json={
                "source_event_id": "consent-a",
                "project_id": "a",
                "consented": True,
            },
        )
        blocked = client.post(
            f"/api/v1/campaigns/c/invoices/{invoice['id']}/payments",
            json={"source_event_id": "early-pay"},
        )
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "invoice_not_payable"
        assert blocked.json()["error"]["details"]["reason"] == "consent_pending"

        consent_all(client, invoice["id"])
        number = client.post(
            f"/api/v1/campaigns/c/invoices/{invoice['id']}/payments",
            json={"source_event_id": "pay-1"},
        ).json()["instruction_number"]

        failed = client.post(
            f"/api/v1/campaigns/c/payments/{number}/results",
            json={"outcome": "failed", "result_event_id": "bank-fail"},
        )
        assert failed.status_code == 200
        assert failed.json()["status"] == "failed"

        view = client.get(
            f"/api/v1/campaigns/c/invoices/{invoice['id']}"
        ).json()
        assert view["state"] == "unpaid"
        assert view["payable"] is True

        second = client.post(
            f"/api/v1/campaigns/c/invoices/{invoice['id']}/payments",
            json={"source_event_id": "pay-2"},
        )
        assert second.status_code == 201
        assert second.json()["attempt_no"] == 2


def test_http_lost_response_restart_and_retry(db_path):
    # First app instance loses the bank's submit acknowledgement.
    with _client(db_path) as client:
        seed(client)
        invoice = register(client)
        consent_all(client, invoice["id"])

        client.post(
            "/api/v1/simulated-bank/lose-next-response",
            json={"enabled": True},
        )
        submitted = client.post(
            f"/api/v1/campaigns/c/invoices/{invoice['id']}/payments",
            json={"source_event_id": "pay-1"},
        )
        assert submitted.status_code == 201
        number = submitted.json()["instruction_number"]

        # The bank accepted it even though the ledger saw no response.
        bank = client.get(f"/api/v1/simulated-bank/payments/{number}").json()
        assert bank["payment"]["status"] == "pending"

    # Restart: a brand new app against the same two databases.
    with _client(db_path) as client:
        recovered = client.get(f"/api/v1/campaigns/c/payments/{number}")
        assert recovered.status_code == 200
        assert recovered.json()["status"] == "pending"
        assert recovered.json()["bank"]["status"] == "pending"

        # Re-sending the old submit request must not create a new payment.
        retried = client.post(
            f"/api/v1/campaigns/c/invoices/{invoice['id']}/payments",
            json={"source_event_id": "pay-1"},
        )
        assert retried.status_code == 200
        assert retried.headers.get("idempotency-replayed") == "true"
        assert retried.json()["instruction_number"] == number

        payments = client.get(
            f"/api/v1/campaigns/c/invoices/{invoice['id']}/payments"
        ).json()
        assert payments["payment_count"] == 1

        # Bank later confirms success; finance syncs the result after restart.
        client.post(
            f"/api/v1/simulated-bank/payments/{number}/results",
            json={"outcome": "succeeded", "result_event_id": "bank-ok"},
        )
        synced = client.post(
            f"/api/v1/campaigns/c/payments/{number}/sync"
        )
        assert synced.status_code == 200
        assert synced.json()["status"] == "succeeded"

        funds = client.get("/api/v1/campaigns/c/reimbursement-funds").json()
        by_project = {p["project_id"]: p for p in funds["projects"]}
        assert by_project["a"]["paid_cents"] == 600
        assert by_project["b"]["paid_cents"] == 400


def test_http_validation(db_path):
    with _client(db_path) as client:
        seed(client)
        bad = client.post(
            "/api/v1/campaigns/c/invoices",
            json={
                "source_event_id": "bad",
                "payee": "V",
                "vendor_invoice_number": "N",
                "shares": [{"project_id": "a", "share_cents": 0}],
            },
        )
        assert bad.status_code == 422

        missing = client.get("/api/v1/campaigns/c/invoices/999")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "invoice_not_found"
