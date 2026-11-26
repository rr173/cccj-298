from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "api.db")
    with TestClient(app) as c:
        yield c


def seed(client: TestClient) -> None:
    r = client.post(
        "/api/v1/campaigns",
        json={
            "campaign_id": "c1",
            "name": "API Campaign",
            "total_pool_limit_cents": 1000,
            "donor_match_cap_cents": 1000,
        },
    )
    assert r.status_code == 201, r.text
    for pid in ("p1", "p2"):
        r = client.post(
            "/api/v1/campaigns/c1/projects",
            json={"project_id": pid, "name": pid.upper()},
        )
        assert r.status_code == 201, r.text


def test_http_full_flow_idempotency_and_views(client):
    seed(client)

    donation = {
        "source_event_id": "d1",
        "donor_id": "alice",
        "project_id": "p1",
        "amount_cents": 600,
    }
    r1 = client.post("/api/v1/campaigns/c1/donations", json=donation)
    assert r1.status_code == 201, r1.text
    r2 = client.post("/api/v1/campaigns/c1/donations", json=donation)
    assert r2.status_code == 200
    assert r2.headers.get("idempotency-replayed") == "true"
    assert r1.json()["id"] == r2.json()["id"]

    changed = {**donation, "amount_cents": 601}
    r3 = client.post("/api/v1/campaigns/c1/donations", json=changed)
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "idempotency_parameter_mismatch"

    client.post(
        "/api/v1/campaigns/c1/donations",
        json={
            "source_event_id": "d2",
            "donor_id": "bob",
            "project_id": "p2",
            "amount_cents": 600,
        },
    )

    prov = client.get("/api/v1/campaigns/c1/provisional")
    assert prov.status_code == 200
    assert [i["provisional_match_cents"] for i in prov.json()["items"]] == [
        600,
        400,
    ]

    settle_payload = {"source_event_id": "s1"}
    s1 = client.post(
        "/api/v1/campaigns/c1/projects/p2/settlements",
        json=settle_payload,
    )
    assert s1.status_code == 201, s1.text
    assert s1.json()["total_amount_cents"] == 400

    s2 = client.post(
        "/api/v1/campaigns/c1/projects/p2/settlements",
        json=settle_payload,
    )
    assert s2.status_code == 200
    assert s2.json()["id"] == s1.json()["id"]
    assert s2.headers.get("idempotency-replayed") == "true"

    refund = client.post(
        "/api/v1/campaigns/c1/refunds",
        json={
            "source_event_id": "r1",
            "donation_source_event_id": "d1",
            "amount_cents": 100,
            "reason": "duplicate charge",
        },
    )
    assert refund.status_code == 201, refund.text
    assert refund.json()["actual_reversal"] is None

    prov_after = client.get("/api/v1/campaigns/c1/provisional").json()
    by_source = {i["source_event_id"]: i for i in prov_after["items"]}
    assert by_source["d2"]["provisional_match_cents"] == 100

    grants = client.get("/api/v1/campaigns/c1/projects/p2/grants")
    assert grants.status_code == 200
    assert grants.json()["settlements"][0]["total_amount_cents"] == 400

    reversals = client.get("/api/v1/campaigns/c1/reversals")
    assert reversals.status_code == 200
    assert reversals.json()["reversals"] == []

    donation_reversals = client.get(
        "/api/v1/campaigns/c1/donations/d1/reversals"
    )
    assert donation_reversals.status_code == 200
    body = donation_reversals.json()
    assert body["donation"]["source_event_id"] == "d1"
    assert body["reversals"] == []


def test_http_validation_and_404(client):
    r = client.post(
        "/api/v1/campaigns",
        json={
            "campaign_id": "bad",
            "name": "Bad",
            "total_pool_limit_cents": -1,
            "donor_match_cap_cents": 10,
        },
    )
    assert r.status_code == 422

    r = client.get("/api/v1/campaigns/missing/provisional")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "campaign_not_found"
