"""Pydantic request schemas."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

NonEmptyId = Annotated[str, Field(min_length=1, max_length=120)]
NonEmptyName = Annotated[str, Field(min_length=1, max_length=200)]
CentsNonNegative = Annotated[int, Field(ge=0, le=10**15)]
CentsPositive = Annotated[int, Field(gt=0, le=10**15)]


class CampaignCreate(BaseModel):
    campaign_id: NonEmptyId
    name: NonEmptyName
    total_pool_limit_cents: CentsNonNegative
    donor_match_cap_cents: CentsNonNegative


class ProjectCreate(BaseModel):
    project_id: NonEmptyId
    name: NonEmptyName
    is_ready: bool = True


class ProjectReadyUpdate(BaseModel):
    is_ready: bool


class DonationCreate(BaseModel):
    source_event_id: NonEmptyId
    donor_id: NonEmptyId
    project_id: NonEmptyId
    amount_cents: CentsPositive
    note: str | None = Field(default=None, max_length=500)


class RefundCreate(BaseModel):
    source_event_id: NonEmptyId
    donation_source_event_id: NonEmptyId
    amount_cents: CentsPositive
    reason: str | None = Field(default=None, max_length=500)


class SettlementCreate(BaseModel):
    source_event_id: NonEmptyId


class InvoiceShareCreate(BaseModel):
    project_id: NonEmptyId
    share_cents: CentsPositive


class InvoiceRegister(BaseModel):
    source_event_id: NonEmptyId
    payee: NonEmptyName
    vendor_invoice_number: NonEmptyId
    shares: list[InvoiceShareCreate] = Field(min_length=1)


class InvoiceConsentCreate(BaseModel):
    source_event_id: NonEmptyId
    project_id: NonEmptyId
    consented: bool


class PaymentSubmit(BaseModel):
    source_event_id: NonEmptyId


class PaymentResult(BaseModel):
    outcome: Literal["succeeded", "failed"]
    result_event_id: NonEmptyId


class BankSubmitSimulation(BaseModel):
    instruction_number: NonEmptyId
    payee: NonEmptyName
    vendor_invoice_number: NonEmptyId
    amount_cents: CentsPositive


class BankResultSimulation(BaseModel):
    outcome: Literal["succeeded", "failed"]
    result_event_id: NonEmptyId


class BankLoseResponseSimulation(BaseModel):
    enabled: bool = True


class ClosingCreate(BaseModel):
    source_event_id: NonEmptyId
    cutoff_at: str | None = Field(default=None, max_length=40)


class ClosingSign(BaseModel):
    source_event_id: NonEmptyId


class ClosingDiscard(BaseModel):
    source_event_id: NonEmptyId
