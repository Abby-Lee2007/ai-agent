"""Immutable, non-sensitive audit records for credit-risk shadow evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple


@dataclass(frozen=True)
class CreditRiskAuditDiagnostic:
    """Whitelisted diagnostic fields safe for audit storage."""

    code: str
    feature: str = ""
    count: int = 1
    severity: str = "warning"
    statement_cycle_id: Optional[str] = None


@dataclass(frozen=True)
class CreditRiskAuditRecord:
    """In-memory audit record for the v0.1 shadow service.

    The record intentionally excludes source objects and source_metadata. It
    must never contain phone numbers, card numbers, transaction text, merchant
    names, or identity information.
    """

    mode: str
    status: str
    evaluated_at_utc: str
    feature_version: str
    model_name: str
    model_version: Optional[str]
    artifact_sha256: Optional[str]
    reliable_coverage_ratio: Optional[float]
    approximate_coverage_ratio: Optional[float]
    potential_coverage_ratio: Optional[float]
    missing_features: Tuple[str, ...]
    approximate_features: Tuple[str, ...]
    diagnostics: Tuple[CreditRiskAuditDiagnostic, ...]
    model_result_status: str
    error_code: str
    shadow_run_id: Optional[str] = None
    application_id: Optional[str] = None
    request_id: Optional[str] = None
    schema_version: Optional[str] = None
    reliable_features: Tuple[str, ...] = ()
    policy_blocked_features: Tuple[str, ...] = ()
    reliable_feature_count: int = 0
    approximate_feature_count: int = 0
    missing_feature_count: int = 0
    policy_blocked_feature_count: int = 0
    six_month_coverage_complete: Optional[bool] = None
    six_month_reliable_complete: Optional[bool] = None
    statement_cycle_ids: Tuple[str, ...] = ()
    rule_impact: str = field(default="none", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "missing_features", tuple(self.missing_features))
        object.__setattr__(
            self,
            "approximate_features",
            tuple(self.approximate_features),
        )
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        object.__setattr__(self, "reliable_features", tuple(self.reliable_features))
        object.__setattr__(
            self,
            "policy_blocked_features",
            tuple(self.policy_blocked_features),
        )
        object.__setattr__(self, "statement_cycle_ids", tuple(self.statement_cycle_ids))
