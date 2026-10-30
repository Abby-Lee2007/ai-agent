"""Immutable identity and lifecycle objects for credit-risk shadow runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from finagent.risk.credit_risk.audit import (
    CreditRiskAuditDiagnostic,
    CreditRiskAuditRecord,
)


SHADOW_RUN_SCHEMA_VERSION = "shadow_run_v1"


@dataclass(frozen=True)
class CreditRiskShadowRun:
    """One traceable, model-free shadow evaluation."""

    shadow_run_id: str
    application_id: str
    request_id: str
    status: str
    model_status: str
    reliable_coverage: Optional[float]
    approximate_coverage: Optional[float]
    potential_coverage: Optional[float]
    reliable_feature_count: int
    approximate_feature_count: int
    missing_feature_count: int
    policy_blocked_count: int
    history_complete: Optional[bool]
    created_at: str
    completed_at: str
    schema_version: str
    feature_version: str
    error_code: str
    audit: CreditRiskAuditRecord
    customer_ref: str = ""
    missing_features: Tuple[str, ...] = ()
    approximate_features: Tuple[str, ...] = ()
    policy_blocked_features: Tuple[str, ...] = ()
    diagnostics: Tuple[CreditRiskAuditDiagnostic, ...] = ()
    rule_impact: str = field(default="none", init=False)

    def __post_init__(self) -> None:
        if not str(self.shadow_run_id or "").strip():
            raise ValueError("shadow_run_id must not be empty")
        if not str(self.application_id or "").strip():
            raise ValueError("application_id must not be empty")
        if not str(self.request_id or "").strip():
            raise ValueError("request_id must not be empty")
        if self.audit.shadow_run_id != self.shadow_run_id:
            raise ValueError("audit shadow_run_id does not match run")
        if self.audit.application_id != self.application_id:
            raise ValueError("audit application_id does not match run")
        if self.audit.request_id != self.request_id:
            raise ValueError("audit request_id does not match run")
        if self.audit.status != self.status:
            raise ValueError("audit status does not match run")
        if self.audit.model_result_status != self.model_status:
            raise ValueError("audit model status does not match run")
        object.__setattr__(self, "missing_features", tuple(self.missing_features))
        object.__setattr__(
            self,
            "approximate_features",
            tuple(self.approximate_features),
        )
        object.__setattr__(
            self,
            "policy_blocked_features",
            tuple(self.policy_blocked_features),
        )
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        object.__setattr__(self, "rule_impact", "none")

    @property
    def run_key(self) -> Tuple[str, str]:
        """Logical idempotency key for one application and formal request."""
        return self.application_id, self.request_id

    def to_persistence_payload(self) -> Dict[str, Any]:
        """Serialize only the already-sanitized shadow-run contract."""
        return {
            "shadow_run_id": self.shadow_run_id,
            "application_id": self.application_id,
            "request_id": self.request_id,
            "customer_ref": self.customer_ref,
            "status": self.status,
            "model_status": self.model_status,
            "reliable_coverage": self.reliable_coverage,
            "approximate_coverage": self.approximate_coverage,
            "potential_coverage": self.potential_coverage,
            "reliable_feature_count": self.reliable_feature_count,
            "approximate_feature_count": self.approximate_feature_count,
            "missing_feature_count": self.missing_feature_count,
            "policy_blocked_count": self.policy_blocked_count,
            "history_complete": self.history_complete,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "schema_version": self.schema_version,
            "feature_version": self.feature_version,
            "error_code": self.error_code,
            "rule_impact": self.rule_impact,
            "missing_features": list(self.missing_features),
            "approximate_features": list(self.approximate_features),
            "policy_blocked_features": list(self.policy_blocked_features),
            "diagnostics": [
                {
                    "code": item.code,
                    "feature": item.feature,
                    "count": item.count,
                    "severity": item.severity,
                    "statement_cycle_id": item.statement_cycle_id,
                }
                for item in self.diagnostics
            ],
            "audit": {
                "mode": self.audit.mode,
                "status": self.audit.status,
                "evaluated_at_utc": self.audit.evaluated_at_utc,
                "feature_version": self.audit.feature_version,
                "model_name": self.audit.model_name,
                "model_version": self.audit.model_version,
                "artifact_sha256": self.audit.artifact_sha256,
                "reliable_coverage_ratio": self.audit.reliable_coverage_ratio,
                "approximate_coverage_ratio": self.audit.approximate_coverage_ratio,
                "potential_coverage_ratio": self.audit.potential_coverage_ratio,
                "missing_features": list(self.audit.missing_features),
                "approximate_features": list(self.audit.approximate_features),
                "diagnostics": [
                    {
                        "code": item.code,
                        "feature": item.feature,
                        "count": item.count,
                        "severity": item.severity,
                        "statement_cycle_id": item.statement_cycle_id,
                    }
                    for item in self.audit.diagnostics
                ],
                "model_result_status": self.audit.model_result_status,
                "error_code": self.audit.error_code,
                "shadow_run_id": self.audit.shadow_run_id,
                "application_id": self.audit.application_id,
                "request_id": self.audit.request_id,
                "schema_version": self.audit.schema_version,
                "reliable_features": list(self.audit.reliable_features),
                "policy_blocked_features": list(
                    self.audit.policy_blocked_features
                ),
                "reliable_feature_count": self.audit.reliable_feature_count,
                "approximate_feature_count": self.audit.approximate_feature_count,
                "missing_feature_count": self.audit.missing_feature_count,
                "policy_blocked_feature_count": (
                    self.audit.policy_blocked_feature_count
                ),
                "six_month_coverage_complete": (
                    self.audit.six_month_coverage_complete
                ),
                "six_month_reliable_complete": (
                    self.audit.six_month_reliable_complete
                ),
                "statement_cycle_ids": list(self.audit.statement_cycle_ids),
                "rule_impact": self.audit.rule_impact,
            },
        }

    @classmethod
    def from_persistence_payload(
        cls,
        payload: Dict[str, Any],
    ) -> "CreditRiskShadowRun":
        audit_data = payload.get("audit") or {}
        diagnostics = tuple(
            CreditRiskAuditDiagnostic(
                code=str(item.get("code") or "unknown"),
                feature=str(item.get("feature") or ""),
                count=int(item.get("count") or 0),
                severity=str(item.get("severity") or "warning"),
                statement_cycle_id=item.get("statement_cycle_id"),
            )
            for item in audit_data.get("diagnostics") or []
            if isinstance(item, dict)
        )
        audit = CreditRiskAuditRecord(
            mode=str(audit_data.get("mode") or ""),
            status=str(audit_data.get("status") or ""),
            evaluated_at_utc=str(audit_data.get("evaluated_at_utc") or ""),
            feature_version=str(audit_data.get("feature_version") or ""),
            model_name=str(audit_data.get("model_name") or ""),
            model_version=audit_data.get("model_version"),
            artifact_sha256=audit_data.get("artifact_sha256"),
            reliable_coverage_ratio=audit_data.get("reliable_coverage_ratio"),
            approximate_coverage_ratio=audit_data.get("approximate_coverage_ratio"),
            potential_coverage_ratio=audit_data.get("potential_coverage_ratio"),
            missing_features=tuple(audit_data.get("missing_features") or ()),
            approximate_features=tuple(
                audit_data.get("approximate_features") or ()
            ),
            diagnostics=diagnostics,
            model_result_status=str(
                audit_data.get("model_result_status") or ""
            ),
            error_code=str(audit_data.get("error_code") or ""),
            shadow_run_id=audit_data.get("shadow_run_id"),
            application_id=audit_data.get("application_id"),
            request_id=audit_data.get("request_id"),
            schema_version=audit_data.get("schema_version"),
            reliable_features=tuple(audit_data.get("reliable_features") or ()),
            policy_blocked_features=tuple(
                audit_data.get("policy_blocked_features") or ()
            ),
            reliable_feature_count=int(
                audit_data.get("reliable_feature_count") or 0
            ),
            approximate_feature_count=int(
                audit_data.get("approximate_feature_count") or 0
            ),
            missing_feature_count=int(
                audit_data.get("missing_feature_count") or 0
            ),
            policy_blocked_feature_count=int(
                audit_data.get("policy_blocked_feature_count") or 0
            ),
            six_month_coverage_complete=audit_data.get(
                "six_month_coverage_complete"
            ),
            six_month_reliable_complete=audit_data.get(
                "six_month_reliable_complete"
            ),
            statement_cycle_ids=tuple(
                audit_data.get("statement_cycle_ids") or ()
            ),
        )
        run_diagnostics = tuple(
            CreditRiskAuditDiagnostic(
                code=str(item.get("code") or "unknown"),
                feature=str(item.get("feature") or ""),
                count=int(item.get("count") or 0),
                severity=str(item.get("severity") or "warning"),
                statement_cycle_id=item.get("statement_cycle_id"),
            )
            for item in payload.get("diagnostics") or []
            if isinstance(item, dict)
        )
        return cls(
            shadow_run_id=str(payload.get("shadow_run_id") or ""),
            application_id=str(payload.get("application_id") or ""),
            request_id=str(payload.get("request_id") or ""),
            status=str(payload.get("status") or ""),
            model_status=str(payload.get("model_status") or ""),
            reliable_coverage=payload.get("reliable_coverage"),
            approximate_coverage=payload.get("approximate_coverage"),
            potential_coverage=payload.get("potential_coverage"),
            reliable_feature_count=int(
                payload.get("reliable_feature_count") or 0
            ),
            approximate_feature_count=int(
                payload.get("approximate_feature_count") or 0
            ),
            missing_feature_count=int(
                payload.get("missing_feature_count") or 0
            ),
            policy_blocked_count=int(payload.get("policy_blocked_count") or 0),
            history_complete=payload.get("history_complete"),
            created_at=str(payload.get("created_at") or ""),
            completed_at=str(payload.get("completed_at") or ""),
            schema_version=str(payload.get("schema_version") or ""),
            feature_version=str(payload.get("feature_version") or ""),
            error_code=str(payload.get("error_code") or ""),
            audit=audit,
            customer_ref=str(payload.get("customer_ref") or ""),
            missing_features=tuple(payload.get("missing_features") or ()),
            approximate_features=tuple(
                payload.get("approximate_features") or ()
            ),
            policy_blocked_features=tuple(
                payload.get("policy_blocked_features") or ()
            ),
            diagnostics=run_diagnostics,
        )


@dataclass(frozen=True)
class ShadowRunSkip:
    """Explicit, non-sensitive reason for not creating a shadow run."""

    application_id: str
    request_id: str
    reason: str
    recorded_at_utc: str
    schema_version: str = SHADOW_RUN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not str(self.reason or "").strip():
            raise ValueError("skip reason must not be empty")


@dataclass(frozen=True)
class ShadowPersistenceFailure:
    """Sanitized diagnostic for a shadow run not written to SQLite."""

    shadow_run_id: str
    application_id: str
    request_id: str
    reason: str
    error_type: str
    recorded_at_utc: str
    schema_version: str = SHADOW_RUN_SCHEMA_VERSION


__all__ = [
    "CreditRiskShadowRun",
    "SHADOW_RUN_SCHEMA_VERSION",
    "ShadowPersistenceFailure",
    "ShadowRunSkip",
]
