"""Non-user-facing integration of Business Materializer and Shadow Service."""

from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Optional, Sequence, Tuple

from finagent.data.models import Account, Card, Transaction, User
from finagent.risk.credit_risk.audit import CreditRiskAuditRecord
from finagent.risk.credit_risk.run import (
    CreditRiskShadowRun,
    SHADOW_RUN_SCHEMA_VERSION,
    ShadowPersistenceFailure,
    ShadowRunSkip,
)
from finagent.risk.credit_risk.run_repository import CreditShadowRunRepository
from finagent.risk.credit_risk.service import CreditRiskService


_MODEL_CONFIG_NAME = "credit-risk-balanced-logistic"
_FEATURE_VERSION = "business_credit_features_v1"
_LOGGER = logging.getLogger(__name__)


class _ShadowRunFlight:
    """In-flight state for one application/request identity."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.error: Optional[BaseException] = None


class BusinessCreditShadowIntegration:
    """Run background-only Shadow evaluations without exposing transport data."""

    @classmethod
    def from_env(
        cls,
        *,
        service: Optional[CreditRiskService] = None,
        run_repository: Optional[CreditShadowRunRepository] = None,
    ) -> "BusinessCreditShadowIntegration":
        return cls(
            mode=os.getenv("FINAGENT_CREDIT_SHADOW_MODE", "off"),
            service=service,
            run_repository=run_repository,
        )

    def __init__(
        self,
        mode: Optional[str] = None,
        *,
        service: Optional[CreditRiskService] = None,
        run_repository: Optional[CreditShadowRunRepository] = None,
        max_audit_records: int = 100,
    ) -> None:
        configured = (
            mode
            if mode is not None
            else os.getenv("FINAGENT_CREDIT_SHADOW_MODE", "off")
        )
        normalized = str(configured).strip().lower()
        if normalized == "on":
            normalized = "shadow"
        if normalized not in {"off", "shadow"}:
            raise ValueError("credit shadow mode must be off or shadow")
        if max_audit_records < 1:
            raise ValueError("max_audit_records must be positive")
        self.mode = normalized
        self._service = service or CreditRiskService(mode=normalized)
        self._run_repository = run_repository
        self._max_audit_records = int(max_audit_records)
        self._audits: list[CreditRiskAuditRecord] = []
        self._runs: dict[tuple[str, str], CreditRiskShadowRun] = {}
        self._skips: list[ShadowRunSkip] = []
        self._inflight: dict[tuple[str, str], _ShadowRunFlight] = {}
        self._pending_persistence: dict[
            tuple[str, str],
            CreditRiskShadowRun,
        ] = {}
        self._persistence_failures: list[ShadowPersistenceFailure] = []
        self._lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return self.mode == "shadow"

    def observe_credit_application(
        self,
        *,
        user: User,
        cards: Sequence[Card],
        accounts: Sequence[Account],
        transactions: Sequence[Transaction],
        application_id: str = "",
        request_id: str = "",
        as_of: Optional[datetime] = None,
    ) -> Optional[CreditRiskShadowRun]:
        """Observe one application; this method never changes business state."""
        if not self.enabled:
            return None
        normalized_application_id = str(application_id or "").strip()
        normalized_request_id = str(request_id or "").strip()
        missing_ids = []
        if not normalized_application_id:
            missing_ids.append("application_id")
        if not normalized_request_id:
            missing_ids.append("request_id")
        if missing_ids:
            self._append_skip(ShadowRunSkip(
                application_id=normalized_application_id,
                request_id=normalized_request_id,
                reason=f"missing_{'_and_'.join(missing_ids)}",
                recorded_at_utc=datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
            ))
            return None
        run_key = (normalized_application_id, normalized_request_id)
        owner, flight, existing = self._claim_run(run_key)
        if existing is not None:
            return existing
        if not owner:
            return self._await_run(run_key, flight)
        try:
            persisted = self._load_persisted_run(run_key)
            if persisted is not None:
                return self._append_run(
                    persisted,
                    queue_for_persistence=False,
                )
            shadow_run_id = f"SHADOW-{uuid.uuid4().hex}"
            created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            observed_at = as_of
            if observed_at is None:
                observed_at = datetime.now(timezone.utc)
            elif observed_at.tzinfo is None or observed_at.utcoffset() is None:
                # DataStore.Clock is intentionally timezone-naive in the demo.
                # Treat it as UTC only at this isolated integration boundary.
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            else:
                observed_at = observed_at.astimezone(timezone.utc)
            try:
                result = self._service.evaluate_business_shadow(
                    user=user,
                    cards=cards,
                    accounts=accounts,
                    transactions=transactions,
                    customer_type="unknown",
                    as_of=observed_at,
                )
            except Exception:
                return self._record_failure_run(
                    shadow_run_id=shadow_run_id,
                    application_id=normalized_application_id,
                    request_id=normalized_request_id,
                    created_at=created_at,
                )
            audit = replace(
                result.audit,
                shadow_run_id=shadow_run_id,
                application_id=normalized_application_id,
                request_id=normalized_request_id,
                schema_version=SHADOW_RUN_SCHEMA_VERSION,
            )
            run = self._run_from_audit(
                audit,
                created_at=created_at,
                customer_ref=(
                    result.snapshot.customer_ref
                    if result.snapshot is not None else ""
                ),
            )
            return self._append_run(run)
        except Exception as exc:
            with self._lock:
                flight.error = exc
            raise
        finally:
            self._release_run_claim(run_key, flight)

    def recent_audits(self) -> Tuple[CreditRiskAuditRecord, ...]:
        """Return in-memory audits for internal inspection only."""
        with self._lock:
            return tuple(self._audits)

    def recent_runs(self) -> Tuple[CreditRiskShadowRun, ...]:
        """Return keyed, in-memory run identities for stage-1 verification."""
        with self._lock:
            return tuple(self._runs.values())

    def recent_skips(self) -> Tuple[ShadowRunSkip, ...]:
        """Return explicit reasons for runs intentionally not created."""
        with self._lock:
            return tuple(self._skips)

    def recent_persistence_failures(
        self,
    ) -> Tuple[ShadowPersistenceFailure, ...]:
        """Return sanitized diagnostics for failed shadow persistence."""
        with self._lock:
            return tuple(self._persistence_failures)

    def pending_persistence_count(self) -> int:
        with self._lock:
            return len(self._pending_persistence)

    def record_failure(
        self,
        *,
        application_id: str = "",
        request_id: str = "",
    ) -> Optional[CreditRiskShadowRun]:
        """Record a sanitized integration failure without affecting business flow."""
        if not self.enabled:
            return None
        normalized_application_id = str(application_id or "").strip()
        normalized_request_id = str(request_id or "").strip()
        if not normalized_application_id or not normalized_request_id:
            missing = []
            if not normalized_application_id:
                missing.append("application_id")
            if not normalized_request_id:
                missing.append("request_id")
            self._append_skip(ShadowRunSkip(
                application_id=normalized_application_id,
                request_id=normalized_request_id,
                reason=f"missing_{'_and_'.join(missing)}",
                recorded_at_utc=datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
            ))
            return None
        run_key = (normalized_application_id, normalized_request_id)
        with self._lock:
            existing = self._runs.get(run_key)
            if existing is not None:
                return existing
        return self._record_failure_run(
            shadow_run_id=f"SHADOW-{uuid.uuid4().hex}",
            application_id=normalized_application_id,
            request_id=normalized_request_id,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def _append_run(
        self,
        run: CreditRiskShadowRun,
        *,
        queue_for_persistence: bool = True,
    ) -> CreditRiskShadowRun:
        with self._lock:
            existing = self._runs.get(run.run_key)
            if existing is not None:
                return existing
            self._runs[run.run_key] = run
            self._audits.append(run.audit)
            if self._run_repository is not None and queue_for_persistence:
                self._pending_persistence[run.run_key] = run
            if len(self._audits) > self._max_audit_records:
                del self._audits[: len(self._audits) - self._max_audit_records]
            while len(self._runs) > self._max_audit_records:
                oldest_key = next(iter(self._runs))
                self._runs.pop(oldest_key, None)
        return run

    def _load_persisted_run(
        self,
        run_key: tuple[str, str],
    ) -> Optional[CreditRiskShadowRun]:
        if self._run_repository is None:
            return None
        try:
            return self._run_repository.get_by_identity(*run_key)
        except Exception as exc:
            # Lookup is an optimization; the post-session flush remains the
            # authoritative persistence attempt and will surface failures.
            _LOGGER.warning(
                "credit shadow preflight lookup failed: %s",
                type(exc).__name__,
            )
            return None

    def flush_pending(self) -> Tuple[CreditRiskShadowRun, ...]:
        """Persist queued runs after the formal session has committed."""
        if self._run_repository is None:
            return ()
        with self._lock:
            pending = tuple(self._pending_persistence.items())
        persisted = []
        for run_key, run in pending:
            try:
                result = self._run_repository.save(run)
            except Exception as exc:
                failure = ShadowPersistenceFailure(
                    shadow_run_id=run.shadow_run_id,
                    application_id=run.application_id,
                    request_id=run.request_id,
                    reason="shadow_persistence_error",
                    error_type=type(exc).__name__,
                    recorded_at_utc=datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                )
                with self._lock:
                    self._persistence_failures.append(failure)
                    if len(self._persistence_failures) > self._max_audit_records:
                        del self._persistence_failures[
                            : len(self._persistence_failures)
                            - self._max_audit_records
                        ]
                    current = self._pending_persistence.get(run_key)
                    if current is run:
                        self._pending_persistence.pop(run_key, None)
                _LOGGER.error(
                    "credit shadow persistence failed run=%s error=%s",
                    run.shadow_run_id,
                    type(exc).__name__,
                )
                continue
            canonical = result.run
            with self._lock:
                self._runs[run_key] = canonical
                for index, audit in enumerate(self._audits):
                    if audit.shadow_run_id == run.shadow_run_id:
                        self._audits[index] = canonical.audit
                        break
                current = self._pending_persistence.get(run_key)
                if current is run:
                    self._pending_persistence.pop(run_key, None)
            persisted.append(canonical)
        return tuple(persisted)

    def discard_pending_persistence(
        self,
        *,
        reason: str,
        error_type: str,
    ) -> None:
        """Drop queued runs when the formal session was not committed."""
        with self._lock:
            pending = tuple(self._pending_persistence.values())
            self._pending_persistence.clear()
            for run in pending:
                self._persistence_failures.append(ShadowPersistenceFailure(
                    shadow_run_id=run.shadow_run_id,
                    application_id=run.application_id,
                    request_id=run.request_id,
                    reason=reason,
                    error_type=error_type,
                    recorded_at_utc=datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                ))
            if len(self._persistence_failures) > self._max_audit_records:
                del self._persistence_failures[
                    : len(self._persistence_failures) - self._max_audit_records
                ]

    def _claim_run(
        self,
        run_key: tuple[str, str],
    ) -> tuple[
        bool,
        _ShadowRunFlight,
        Optional[CreditRiskShadowRun],
    ]:
        with self._lock:
            existing = self._runs.get(run_key)
            if existing is not None:
                return False, _ShadowRunFlight(), existing
            flight = self._inflight.get(run_key)
            if flight is not None:
                return False, flight, None
            flight = _ShadowRunFlight()
            self._inflight[run_key] = flight
            return True, flight, None

    def _await_run(
        self,
        run_key: tuple[str, str],
        flight: _ShadowRunFlight,
    ) -> Optional[CreditRiskShadowRun]:
        flight.done.wait()
        with self._lock:
            existing = self._runs.get(run_key)
            error = flight.error
        if existing is not None:
            return existing
        if error is not None:
            raise RuntimeError("shadow run evaluation failed") from error
        return None

    def _release_run_claim(
        self,
        run_key: tuple[str, str],
        flight: _ShadowRunFlight,
    ) -> None:
        with self._lock:
            if self._inflight.get(run_key) is flight:
                self._inflight.pop(run_key, None)
            flight.done.set()

    def _append_skip(self, skip: ShadowRunSkip) -> None:
        with self._lock:
            self._skips.append(skip)
            if len(self._skips) > self._max_audit_records:
                del self._skips[: len(self._skips) - self._max_audit_records]

    def _record_failure_run(
        self,
        *,
        shadow_run_id: str,
        application_id: str,
        request_id: str,
        created_at: str,
    ) -> CreditRiskShadowRun:
        audit = self._failure_audit(
            shadow_run_id=shadow_run_id,
            application_id=application_id,
            request_id=request_id,
        )
        return self._append_run(
            self._run_from_audit(audit, created_at=created_at)
        )

    @staticmethod
    def _run_from_audit(
        audit: CreditRiskAuditRecord,
        *,
        created_at: str,
        customer_ref: str = "",
    ) -> CreditRiskShadowRun:
        return CreditRiskShadowRun(
            shadow_run_id=str(audit.shadow_run_id or ""),
            application_id=str(audit.application_id or ""),
            request_id=str(audit.request_id or ""),
            status=audit.status,
            model_status=audit.model_result_status,
            reliable_coverage=audit.reliable_coverage_ratio,
            approximate_coverage=audit.approximate_coverage_ratio,
            potential_coverage=audit.potential_coverage_ratio,
            reliable_feature_count=audit.reliable_feature_count,
            approximate_feature_count=audit.approximate_feature_count,
            missing_feature_count=audit.missing_feature_count,
            policy_blocked_count=audit.policy_blocked_feature_count,
            history_complete=audit.six_month_reliable_complete,
            created_at=created_at,
            completed_at=audit.evaluated_at_utc,
            schema_version=str(
                audit.schema_version or SHADOW_RUN_SCHEMA_VERSION
            ),
            feature_version=audit.feature_version,
            error_code=audit.error_code,
            audit=audit,
            customer_ref=customer_ref,
            missing_features=audit.missing_features,
            approximate_features=audit.approximate_features,
            policy_blocked_features=audit.policy_blocked_features,
            diagnostics=audit.diagnostics,
        )

    def _failure_audit(
        self,
        *,
        shadow_run_id: str,
        application_id: str,
        request_id: str,
    ) -> CreditRiskAuditRecord:
        return CreditRiskAuditRecord(
            mode=self.mode,
            status="shadow_error",
            evaluated_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            feature_version=_FEATURE_VERSION,
            model_name=_MODEL_CONFIG_NAME,
            model_version=None,
            artifact_sha256=None,
            reliable_coverage_ratio=None,
            approximate_coverage_ratio=None,
            potential_coverage_ratio=None,
            missing_features=(),
            approximate_features=(),
            diagnostics=(),
            model_result_status="not_called",
            error_code="credit_shadow_integration_exception",
            shadow_run_id=shadow_run_id,
            application_id=application_id,
            request_id=request_id,
            schema_version=SHADOW_RUN_SCHEMA_VERSION,
            reliable_feature_count=0,
            approximate_feature_count=0,
            missing_feature_count=0,
            policy_blocked_feature_count=0,
            six_month_coverage_complete=None,
            six_month_reliable_complete=None,
            statement_cycle_ids=(),
        )


__all__ = ["BusinessCreditShadowIntegration"]
