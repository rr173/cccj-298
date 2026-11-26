from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "merger-api.db"


def _client(db_path) -> TestClient:
    return TestClient(create_app(db_path))


def _seed(client: TestClient) -> None:
    client.post(
        "/api/v1/campaigns",
        json={
            "campaign_id": "c",
            "name": "Merger API",
            "total_pool_limit_cents": 1_000_000,
            "donor_match_cap_cents": 1_000_000,
        },
    )
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
        client.post(
            f"/api/v1/campaigns/c/projects/{pid}/settlements",
            json={"source_event_id": f"set-{pid}"},
        )


def test_http_merger_flow_with_pending_payment_and_responsibility(db_path):
    with _client(db_path) as client:
        _seed(client)
        invoice = client.post(
            "/api/v1/campaigns/c/invoices",
            json={
                "source_event_id": "inv-1",
                "payee": "Vendor Co",
                "vendor_invoice_number": "V-1",
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
        number = client.post(
            f"/api/v1/campaigns/c/invoices/{invoice['id']}/payments",
            json={"source_event_id": "pay-1"},
        ).json()["instruction_number"]

        # Merge a into b while the payment is still in flight.
        merger = client.post(
            "/api/v1/campaigns/c/mergers",
            json={
                "source_event_id": "merge-1",
                "source_project_id": "a",
                "successor_project_id": "b",
            },
        )
        assert merger.status_code == 201, merger.text
        body = merger.json()
        assert body["source_project_id"] == "a"
        assert body["successor_project_id"] == "b"
        assert body["irreversible"] is True
        assert body["transferred_book_balance_cents"] == 1000
        assert {
            (e["direction"], e["amount_cents"])
            for e in body["internal_transfer_entries"]
        } == {("out", 1000), ("in", 1000)}

        # Exact retry returns the original merger.
        replay = client.post(
            "/api/v1/campaigns/c/mergers",
            json={
                "source_event_id": "merge-1",
                "source_project_id": "a",
                "successor_project_id": "b",
            },
        )
        assert replay.status_code == 200
        assert replay.headers.get("idempotency-replayed") == "true"
        assert replay.json()["id"] == body["id"]

        # Changed parameters are rejected.
        conflict = client.post(
            "/api/v1/campaigns/c/mergers",
            json={
                "source_event_id": "merge-1",
                "source_project_id": "b",
                "successor_project_id": "a",
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == (
            "idempotency_parameter_mismatch"
        )

        # The in-flight instruction is unchanged; the successor holds all of it.
        instruction = client.get(f"/api/v1/campaigns/c/payments/{number}").json()
        assert instruction["status"] == "pending"
        assert {a["responsible_project_id"] for a in instruction["allocations"]} == {
            "b"
        }

        # The responsibility view answers who is in charge now.
        responsibility = client.get(
            "/api/v1/campaigns/c/project-responsibility"
        ).json()
        by_id = {p["project_id"]: p for p in responsibility["projects"]}
        assert by_id["a"]["status"] == "merged"
        assert by_id["a"]["current_responsible_project_id"] == "b"
        assert by_id["b"]["current_responsible_project_id"] == "b"

        # Finance can list and fetch the merger record.
        listing = client.get("/api/v1/campaigns/c/mergers").json()
        assert listing["merger_count"] == 1
        fetched = client.get(
            f"/api/v1/campaigns/c/mergers/{body['id']}"
        ).json()
        assert fetched["id"] == body["id"]
        assert fetched["transferred_invoice_shares"][0][
            "responsible_project_id"
        ] == "b"

        # A new donation to the merged project is rejected.
        rejected = client.post(
            "/api/v1/campaigns/c/donations",
            json={
                "source_event_id": "late-don",
                "donor_id": "d",
                "project_id": "a",
                "amount_cents": 10,
            },
        )
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == (
            "project_merged_no_new_donations"
        )

        # Bank success settles the hold against the successor.
        result = client.post(
            f"/api/v1/campaigns/c/payments/{number}/results",
            json={"outcome": "succeeded", "result_event_id": "bank-ok"},
        )
        assert result.status_code == 200
        funds = {
            p["project_id"]: p
            for p in client.get(
                "/api/v1/campaigns/c/reimbursement-funds"
            ).json()["projects"]
        }
        assert funds["b"]["paid_cents"] == 1000
        assert funds["a"]["book_balance_cents"] == 0


def test_http_failure_requires_successor_reconfirm_before_resend(db_path):
    with _client(db_path) as client:
        _seed(client)
        invoice = client.post(
            "/api/v1/campaigns/c/invoices",
            json={
                "source_event_id": "inv-1",
                "payee": "Vendor Co",
                "vendor_invoice_number": "V-1",
                "shares": [{"project_id": "a", "share_cents": 600}],
            },
        ).json()
        client.post(
            f"/api/v1/campaigns/c/invoices/{invoice['id']}/consents",
            json={
                "source_event_id": "consent-a",
                "project_id": "a",
                "consented": True,
            },
        )
        first = client.post(
            f"/api/v1/campaigns/c/invoices/{invoice['id']}/payments",
            json={"source_event_id": "pay-1"},
        ).json()["instruction_number"]

        client.post(
            "/api/v1/campaigns/c/mergers",
            json={
                "source_event_id": "merge-1",
                "source_project_id": "a",
                "successor_project_id": "b",
            },
        )
        client.post(
            f"/api/v1/campaigns/c/payments/{first}/results",
            json={"outcome": "failed", "result_event_id": "bank-no"},
        )

        view = client.get(
            f"/api/v1/campaigns/c/invoices/{invoice['id']}"
        ).json()
        assert view["payable"] is False
        assert view["not_payable_reason"] == "consent_pending"

        # Resending before re-confirmation is rejected.
        too_early = client.post(
            f"/api/v1/campaigns/c/invoices/{invoice['id']}/payments",
            json={"source_event_id": "pay-too-early"},
        )
        assert too_early.status_code == 409
        assert too_early.json()["error"]["code"] == "invoice_not_payable"

        # The successor reconfirms, then a fresh attempt is accepted.
        reconfirm = client.post(
            f"/api/v1/campaigns/c/invoices/{invoice['id']}/consents",
            json={
                "source_event_id": "reconfirm-b",
                "project_id": "b",
                "consented": True,
            },
        )
        assert reconfirm.status_code == 200
        second = client.post(
            f"/api/v1/campaigns/c/invoices/{invoice['id']}/payments",
            json={"source_event_id": "pay-2"},
        )
        assert second.status_code == 201
        assert second.json()["attempt_no"] == 2


def test_http_merger_validation_and_404(db_path):
    with _client(db_path) as client:
        _seed(client)
        # Same source and successor.
        same = client.post(
            "/api/v1/campaigns/c/mergers",
            json={
                "source_event_id": "m",
                "source_project_id": "a",
                "successor_project_id": "a",
            },
        )
        assert same.status_code == 422
        assert same.json()["error"]["code"] == "merger_same_project"

        # Unknown project.
        missing = client.post(
            "/api/v1/campaigns/c/mergers",
            json={
                "source_event_id": "m2",
                "source_project_id": "a",
                "successor_project_id": "ghost",
            },
        )
        assert missing.status_code == 404

        # Unknown campaign on the read endpoint.
        no_campaign = client.get("/api/v1/campaigns/nope/mergers")
        assert no_campaign.status_code == 404
