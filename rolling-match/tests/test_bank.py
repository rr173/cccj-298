from __future__ import annotations

import pytest

from app.bank import BankError, BankResponseLost, SimulatedBank


@pytest.fixture
def bank(tmp_path):
    return SimulatedBank(tmp_path / "bank.db")


def _submit(bank, number="PAY-1-1", amount=500):
    return bank.submit(
        instruction_number=number,
        payee="Vendor",
        vendor_invoice_number="V-1",
        amount_cents=amount,
    )


def test_submission_is_deduplicated_by_instruction_number(bank):
    first = _submit(bank)
    assert first["duplicate"] is False
    assert first["payment"]["status"] == "pending"

    second = _submit(bank)
    assert second["duplicate"] is True
    assert second["payment"]["instruction_number"] == "PAY-1-1"

    with pytest.raises(BankError) as exc:
        bank.submit(
            instruction_number="PAY-1-1",
            payee="Vendor",
            vendor_invoice_number="V-1",
            amount_cents=999,
        )
    assert exc.value.code == "bank_instruction_conflict"

    accepted = [
        e for e in bank.list_events("PAY-1-1") if e["event_type"] == "accepted"
    ]
    assert len(accepted) == 1


def test_terminal_result_is_idempotent_and_contradictions_rejected(bank):
    _submit(bank)
    ok = bank.confirm(
        "PAY-1-1", outcome="succeeded", result_event_id="evt-1"
    )
    assert ok["payment"]["status"] == "succeeded"

    duplicate = bank.confirm(
        "PAY-1-1", outcome="succeeded", result_event_id="evt-1"
    )
    assert duplicate["duplicate"] is True
    assert duplicate["payment"]["status"] == "succeeded"

    with pytest.raises(BankError) as exc:
        bank.confirm("PAY-1-1", outcome="failed", result_event_id="evt-2")
    assert exc.value.code == "bank_result_conflict"
    assert bank.get("PAY-1-1")["status"] == "succeeded"

    types = [e["event_type"] for e in bank.list_events("PAY-1-1")]
    assert "rejected_conflict" in types
    assert types.count("duplicate_result") == 1


def test_unclear_outcome_is_not_a_failure(bank):
    _submit(bank)
    # No result delivered: still pending, and querying does not change it.
    assert bank.get("PAY-1-1")["status"] == "pending"


def test_lost_submit_response_still_records_payment(bank):
    bank.set_lose_next_submit_response(True)
    with pytest.raises(BankResponseLost):
        _submit(bank)
    stored = bank.get("PAY-1-1")
    assert stored is not None
    assert stored["status"] == "pending"

    # The flag is consumed: the next submission behaves normally.
    other = _submit(bank, number="PAY-1-2")
    assert other["duplicate"] is False
    # Re-submitting the lost one returns the existing payment, not a new one.
    resend = _submit(bank)
    assert resend["duplicate"] is True


def test_result_for_unknown_instruction_is_rejected(bank):
    with pytest.raises(BankError) as exc:
        bank.confirm("PAY-9-9", outcome="succeeded", result_event_id="x")
    assert exc.value.code == "bank_instruction_not_found"


def test_results_of_one_instruction_never_affect_another(bank):
    _submit(bank, number="PAY-1-1")
    _submit(bank, number="PAY-1-2")
    bank.confirm("PAY-1-1", outcome="failed", result_event_id="e1")
    assert bank.get("PAY-1-2")["status"] == "pending"
    bank.confirm("PAY-1-2", outcome="succeeded", result_event_id="e2")
    assert bank.get("PAY-1-1")["status"] == "failed"
