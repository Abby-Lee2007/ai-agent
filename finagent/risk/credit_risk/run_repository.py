"""Domain repository for sanitized credit-risk shadow runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Tuple

from finagent.risk.credit_risk.run import CreditRiskShadowRun


@dataclass(frozen=True)
class ShadowRunWriteResult:
    """Canonical run returned by an idempotent SQLite insert."""

    run: CreditRiskShadowRun
    inserted: bool


class CreditShadowRunRepository:
    """Serialize domain runs through the existing SQLiteRepository."""

    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def save(self, run: CreditRiskShadowRun) -> ShadowRunWriteResult:
        if not isinstance(run, CreditRiskShadowRun):
            raise TypeError("run must be a CreditRiskShadowRun")
        inserted, payload = self._repository.save_credit_shadow_run(
            run.to_persistence_payload()
        )
        canonical = CreditRiskShadowRun.from_persistence_payload(payload)
        if canonical.run_key != run.run_key:
            raise RuntimeError("shadow_run_id conflicts with another request")
        return ShadowRunWriteResult(run=canonical, inserted=inserted)

    def get(self, shadow_run_id: str) -> Optional[CreditRiskShadowRun]:
        payload = self._repository.load_credit_shadow_run(shadow_run_id)
        return (
            CreditRiskShadowRun.from_persistence_payload(payload)
            if payload is not None else None
        )

    def get_by_identity(
        self,
        application_id: str,
        request_id: str,
    ) -> Optional[CreditRiskShadowRun]:
        payload = self._repository.load_credit_shadow_run_by_identity(
            application_id,
            request_id,
        )
        return (
            CreditRiskShadowRun.from_persistence_payload(payload)
            if payload is not None else None
        )

    def list_by_application(
        self,
        application_id: str,
    ) -> Tuple[CreditRiskShadowRun, ...]:
        return tuple(
            CreditRiskShadowRun.from_persistence_payload(payload)
            for payload in self._repository.list_credit_shadow_runs_by_application(
                application_id
            )
        )

    def list_by_request(
        self,
        request_id: str,
    ) -> Tuple[CreditRiskShadowRun, ...]:
        return tuple(
            CreditRiskShadowRun.from_persistence_payload(payload)
            for payload in self._repository.list_credit_shadow_runs_by_request(
                request_id
            )
        )


__all__ = ["CreditShadowRunRepository", "ShadowRunWriteResult"]
