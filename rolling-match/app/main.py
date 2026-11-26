"""FastAPI application for the rolling donation-matching ledger."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .db import init_db, resolve_db_path
from .models import (
    BankLoseResponseSimulation,
    BankResultSimulation,
    BankSubmitSimulation,
    CampaignCreate,
    ClosingCreate,
    ClosingDiscard,
    ClosingSign,
    DonationCreate,
    InvoiceConsentCreate,
    InvoiceRegister,
    PaymentResult,
    PaymentSubmit,
    ProjectCreate,
    ProjectReadyUpdate,
    RefundCreate,
    SettlementCreate,
)
from .bank import BankError, resolve_bank_db_path, SimulatedBank
from .closing import ClosingService
from .reimbursement import ReimbursementService
from .service import LedgerError, LedgerService


def create_app(
    db_path: str | Path | None = None,
    bank_db_path: str | Path | None = None,
) -> FastAPI:
    path = resolve_db_path(db_path)
    init_db(path)
    bank_path = (
        str(bank_db_path)
        if bank_db_path is not None
        else resolve_bank_db_path(path)
    )
    SimulatedBank(bank_path)  # create the standalone bank database/schema

    app = FastAPI(
        title="Open Source Foundation Rolling Match API",
        version="1.2.0",
        description=(
            "Integer-cent donation matching with provisional allocation, "
            "project-level realization, immutable grant history, idempotent "
            "events, order-level refund corrections, joint vendor-invoice "
            "reimbursement through a standalone simulated payment channel, "
            "and periodic sign-off closings reconciled against that bank."
        ),
    )
    app.state.db_path = path
    app.state.bank_db_path = bank_path

    def service() -> LedgerService:
        return LedgerService(app.state.db_path)

    def reimbursement() -> ReimbursementService:
        return ReimbursementService(
            app.state.db_path, app.state.bank_db_path
        )

    def closing() -> ClosingService:
        return ClosingService(app.state.db_path, app.state.bank_db_path)

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

    @app.exception_handler(BankError)
    async def bank_error_handler(
        _request: Request, exc: BankError
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

    # ------------------------------------------------------------------
    # Joint vendor-invoice reimbursement
    # ------------------------------------------------------------------
    @app.post(
        "/api/v1/campaigns/{campaign_id}/invoices",
        tags=["reimbursements"],
        status_code=201,
    )
    def register_invoice(
        campaign_id: str, payload: InvoiceRegister
    ) -> JSONResponse:
        result = reimbursement().register_invoice(
            campaign_id,
            source_event_id=payload.source_event_id,
            payee=payload.payee,
            vendor_invoice_number=payload.vendor_invoice_number,
            shares=[s.model_dump() for s in payload.shares],
        )
        return _command_response(result)

    @app.get(
        "/api/v1/campaigns/{campaign_id}/invoices",
        tags=["reimbursements"],
    )
    def list_invoices(campaign_id: str) -> dict[str, Any]:
        return reimbursement().invoice_overview(campaign_id)

    @app.get(
        "/api/v1/campaigns/{campaign_id}/invoices/{invoice_id}",
        tags=["reimbursements"],
    )
    def get_invoice(campaign_id: str, invoice_id: int) -> dict[str, Any]:
        return reimbursement().get_invoice(campaign_id, invoice_id)

    @app.post(
        "/api/v1/campaigns/{campaign_id}/invoices/{invoice_id}/consents",
        tags=["reimbursements"],
    )
    def set_invoice_consent(
        campaign_id: str, invoice_id: int, payload: InvoiceConsentCreate
    ) -> JSONResponse:
        result = reimbursement().set_invoice_consent(
            campaign_id,
            invoice_id,
            source_event_id=payload.source_event_id,
            project_id=payload.project_id,
            consented=payload.consented,
        )
        return _command_response(result)

    @app.post(
        "/api/v1/campaigns/{campaign_id}/invoices/{invoice_id}/payments",
        tags=["reimbursements"],
        status_code=201,
    )
    def submit_invoice_payment(
        campaign_id: str, invoice_id: int, payload: PaymentSubmit
    ) -> JSONResponse:
        result = reimbursement().submit_payment(
            campaign_id, invoice_id, source_event_id=payload.source_event_id
        )
        return _command_response(result)

    @app.get(
        "/api/v1/campaigns/{campaign_id}/invoices/{invoice_id}/payments",
        tags=["reimbursements"],
    )
    def list_invoice_payments(
        campaign_id: str, invoice_id: int
    ) -> dict[str, Any]:
        return reimbursement().list_invoice_payments(campaign_id, invoice_id)

    @app.get(
        "/api/v1/campaigns/{campaign_id}/payments",
        tags=["reimbursements"],
    )
    def list_payment_instructions(campaign_id: str) -> dict[str, Any]:
        return reimbursement().list_payment_instructions(campaign_id)

    @app.get(
        "/api/v1/campaigns/{campaign_id}/payments/{instruction_number}",
        tags=["reimbursements"],
    )
    def get_payment_instruction(
        campaign_id: str, instruction_number: str
    ) -> dict[str, Any]:
        return reimbursement().get_payment_instruction(
            campaign_id, instruction_number
        )

    @app.post(
        "/api/v1/campaigns/{campaign_id}/payments/{instruction_number}/results",
        tags=["reimbursements"],
    )
    def deliver_payment_result(
        campaign_id: str,
        instruction_number: str,
        payload: PaymentResult,
    ) -> JSONResponse:
        result = reimbursement().deliver_payment_result(
            campaign_id,
            instruction_number,
            outcome=payload.outcome,
            result_event_id=payload.result_event_id,
        )
        return _command_response(result)

    @app.post(
        "/api/v1/campaigns/{campaign_id}/payments/{instruction_number}/sync",
        tags=["reimbursements"],
    )
    def sync_payment_result(
        campaign_id: str, instruction_number: str
    ) -> dict[str, Any]:
        return reimbursement().sync_payment_result(
            campaign_id, instruction_number
        )

    @app.get(
        "/api/v1/campaigns/{campaign_id}/reimbursement-funds",
        tags=["reimbursements"],
    )
    def get_reimbursement_funds(campaign_id: str) -> dict[str, Any]:
        return reimbursement().project_funds(campaign_id)

    # ------------------------------------------------------------------
    # Periodic sign-off statements (closings)
    # ------------------------------------------------------------------
    @app.post(
        "/api/v1/campaigns/{campaign_id}/closings",
        tags=["closings"],
        status_code=201,
    )
    def create_closing(
        campaign_id: str, payload: ClosingCreate
    ) -> JSONResponse:
        result = closing().create_draft(
            campaign_id,
            source_event_id=payload.source_event_id,
            cutoff_at=payload.cutoff_at,
        )
        return _command_response(result)

    @app.get(
        "/api/v1/campaigns/{campaign_id}/closings",
        tags=["closings"],
    )
    def list_closings(campaign_id: str) -> dict[str, Any]:
        return closing().list_closings(campaign_id)

    @app.get(
        "/api/v1/campaigns/{campaign_id}/closings/{closing_id}",
        tags=["closings"],
    )
    def get_closing(campaign_id: str, closing_id: int) -> dict[str, Any]:
        return closing().get_closing(campaign_id, closing_id)

    @app.post(
        "/api/v1/campaigns/{campaign_id}/closings/{closing_id}/sign",
        tags=["closings"],
    )
    def sign_closing(
        campaign_id: str, closing_id: int, payload: ClosingSign
    ) -> JSONResponse:
        result = closing().sign_closing(
            campaign_id,
            closing_id,
            source_event_id=payload.source_event_id,
        )
        return _command_response(result)

    @app.post(
        "/api/v1/campaigns/{campaign_id}/closings/{closing_id}/discard",
        tags=["closings"],
    )
    def discard_closing(
        campaign_id: str, closing_id: int, payload: ClosingDiscard
    ) -> JSONResponse:
        result = closing().discard_closing(
            campaign_id,
            closing_id,
            source_event_id=payload.source_event_id,
        )
        return _command_response(result)

    # ------------------------------------------------------------------
    # Simulated bank channel (local only; no real bank is contacted)
    # ------------------------------------------------------------------
    @app.post("/api/v1/simulated-bank/payments", tags=["simulated-bank"])
    def simulated_bank_submit(
        payload: BankSubmitSimulation,
    ) -> dict[str, Any]:
        return reimbursement().bank.submit(**payload.model_dump())

    @app.get(
        "/api/v1/simulated-bank/payments/{instruction_number}",
        tags=["simulated-bank"],
    )
    def simulated_bank_get(instruction_number: str) -> dict[str, Any]:
        return {
            "payment": reimbursement().bank.get(instruction_number),
            "events": reimbursement().bank.list_events(instruction_number),
        }

    @app.post(
        "/api/v1/simulated-bank/payments/{instruction_number}/results",
        tags=["simulated-bank"],
    )
    def simulated_bank_result(
        instruction_number: str, payload: BankResultSimulation
    ) -> dict[str, Any]:
        return reimbursement().bank.confirm(
            instruction_number,
            outcome=payload.outcome,
            result_event_id=payload.result_event_id,
        )

    @app.post(
        "/api/v1/simulated-bank/lose-next-response",
        tags=["simulated-bank"],
    )
    def simulated_bank_lose_response(
        payload: BankLoseResponseSimulation,
    ) -> dict[str, Any]:
        reimbursement().bank.set_lose_next_submit_response(payload.enabled)
        return {"lose_next_submit_response": payload.enabled}

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
