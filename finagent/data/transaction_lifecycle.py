"""Helpers for realistic transaction lifecycle metadata."""
from __future__ import annotations

from datetime import timedelta
from typing import Iterable, Optional

from finagent.data.models import Transaction
from finagent.data.store import Clock


def enrich_transaction(
    txn: Transaction,
    clock: Clock,
    *,
    expected_minutes: int = 0,
    risk_notes: Optional[Iterable[str]] = None,
    lifecycle_status: str = "completed",
    metadata: Optional[dict] = None,
) -> Transaction:
    submitted = clock.iso()
    txn.submitted_at = txn.submitted_at or submitted
    txn.lifecycle_status = lifecycle_status
    if lifecycle_status == "completed":
        txn.completed_at = txn.completed_at or clock.iso()
    if expected_minutes > 0:
        txn.expected_arrival_at = (
            clock.now() + timedelta(minutes=expected_minutes)
        ).isoformat(timespec="seconds")
    else:
        txn.expected_arrival_at = txn.completed_at
    txn.bank_reference = txn.bank_reference or f"BANK-{txn.txn_id[-10:].upper()}"
    txn.risk_notes = list(risk_notes or [])
    txn.metadata.update(metadata or {})
    return txn


def transaction_payload(txn: Transaction) -> dict:
    return {
        "txn_id": txn.txn_id,
        "user_id": txn.user_id,
        "account_id": txn.account_id,
        "timestamp": txn.timestamp,
        "amount": txn.amount,
        "merchant": txn.merchant,
        "category": txn.category,
        "channel": txn.channel,
        "counterparty": txn.counterparty,
        "note": txn.note,
        "status": txn.status,
        "lifecycle_status": txn.lifecycle_status,
        "submitted_at": txn.submitted_at or txn.timestamp,
        "completed_at": txn.completed_at or (
            txn.timestamp if txn.lifecycle_status == "completed" else ""
        ),
        "expected_arrival_at": txn.expected_arrival_at,
        "bank_reference": txn.bank_reference,
        "failure_reason": txn.failure_reason,
        "risk_notes": txn.risk_notes,
        "metadata": txn.metadata,
        "source": txn.source,
        "is_live": txn.is_live,
        "is_demo": txn.is_demo,
    }
