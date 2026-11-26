"""FastAPI application for the rolling donation-matching ledger."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .db import init_db, resolve_db_path
from .models import (
    CampaignCreate,
    DonationCreate,
    ProjectCreate,
    ProjectReadyUpdate,
    RefundCreate,
    SettlementCreate,
)
from .service import LedgerError, LedgerService


def create_app(db_path: str | Path | None = None) -> FastAPI:
    path = resolve_db_path(db_path)
    init_db(path)

    app = FastAPI(
        title="Open Source Foundation Rolling Match API",
        version="1.0.0",
        description=(
            "Integer-cent donation matching with provisional allocation, "
            "project-level realization, immutable grant history, idempotent "
            "events, and order-level refund corrections."
        ),
    )
    app.state.db_path = path

    def service() -> LedgerService:
        return LedgerService(app.state.db_path)

    @app.exception_handler(LedgerError)
    async def ledger_error_handler(
        _request: Request, exc: LedgerError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    @app.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Campaigns and projects
    # ------------------------------------------------------------------
    @app.post("/api/v1/campaigns", tags=["campaigns"], status_code=201)
    def create_campaign(payload: CampaignCreate) -> JSONResponse:
        result = service().create_campaign(**payload.model_dump())
        return _command_response(result)

    @app.get(
        "/api/v1/campaigns/{campaign_id}",
        tags=["campaigns"],
    )
    def get_campaign(campaign_id: str) -> dict[str, Any]:
        return service().get_campaign(campaign_id)

    @app.post(
        "/api/v1/campaigns/{campaign_id}/projects",
        tags=["projects"],
        status_code=201,
    )
    def create_project(
        campaign_id: str, payload: ProjectCreate
    ) -> JSONResponse:
        result = service().create_project(
            campaign_id,
            project_id=payload.project_id,
            name=payload.name,
            is_ready=payload.is_ready,
        )
        return _command_response(result)

    @app.get(
        "/api/v1/campaigns/{campaign_id}/projects",
        tags=["projects"],
    )
    def list_projects(campaign_id: str) -> list[dict[str, Any]]:
        return service().list_projects(campaign_id)

    @app.patch(
        "/api/v1/campaigns/{campaign_id}/projects/{project_id}/ready",
        tags=["projects"],
    )
    def set_project_ready(
        campaign_id: str,
        project_id: str,
        payload: ProjectReadyUpdate,
    ) -> dict[str, Any]:
        return service().set_project_ready(
            campaign_id, project_id, payload.is_ready
        )

    # ------------------------------------------------------------------
    # Donations and refunds
    # ------------------------------------------------------------------
    @app.post(
        "/api/v1/campaigns/{campaign_id}/donations",
        tags=["donations"],
        status_code=201,
    )
    def create_donation(
        campaign_id: str, payload: DonationCreate
    ) -> JSONResponse:
        result = service().create_donation(
            campaign_id, **payload.model_dump()
        )
        return _command_response(result)

    @app.get(
        "/api/v1/campaigns/{campaign_id}/donations",
        tags=["donations"],
    )
    def list_donations(campaign_id: str) -> list[dict[str, Any]]:
        return service().list_donations(campaign_id)

    @app.post(
        "/api/v1/campaigns/{campaign_id}/refunds",
        tags=["refunds"],
        status_code=201,
    )
    def create_refund(
        campaign_id: str, payload: RefundCreate
    ) -> JSONResponse:
        result = service().create_refund(
            campaign_id, **payload.model_dump()
        )
        return _command_response(result)

    # ------------------------------------------------------------------
    # Provisional allocation, grants, and reversals
    # ------------------------------------------------------------------
    @app.get(
        "/api/v1/campaigns/{campaign_id}/provisional",
        tags=["allocation"],
    )
    def get_provisional(campaign_id: str) -> dict[str, Any]:
        return service().provisional(campaign_id)

    @app.post(
        "/api/v1/campaigns/{campaign_id}/projects/{project_id}/settlements",
        tags=["settlements"],
        status_code=201,
    )
    def settle_project(
        campaign_id: str,
        project_id: str,
        payload: SettlementCreate,
    ) -> JSONResponse:
        result = service().settle_project(
            campaign_id,
            project_id,
            source_event_id=payload.source_event_id,
        )
        return _command_response(result)

    @app.get(
        "/api/v1/campaigns/{campaign_id}/projects/{project_id}/grants",
        tags=["grants"],
    )
    def list_project_grants(
        campaign_id: str, project_id: str
    ) -> dict[str, Any]:
        return service().list_project_grants(campaign_id, project_id)

    @app.get(
        "/api/v1/campaigns/{campaign_id}/reversals",
        tags=["reversals"],
    )
    def list_all_reversals(campaign_id: str) -> dict[str, Any]:
        return service().list_reversals(campaign_id)

    @app.get(
        "/api/v1/campaigns/{campaign_id}/donations/{donation_source_event_id}/reversals",
        tags=["reversals"],
    )
    def list_donation_reversals(
        campaign_id: str, donation_source_event_id: str
    ) -> dict[str, Any]:
        return service().list_reversals(
            campaign_id, donation_source_event_id
        )

    return app


def _command_response(result: Any) -> JSONResponse:
    headers: dict[str, str] = {}
    if getattr(result, "replayed", False):
        headers["Idempotency-Replayed"] = "true"
    return JSONResponse(
        status_code=result.status_code,
        content=result.body,
        headers=headers,
    )


app = create_app()
