"""Adapter from Business Credit Contract snapshots to shadow assessments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Tuple

from finagent.risk.credit_risk.audit import CreditRiskAuditDiagnostic
from finagent.risk.credit_risk.business_contract import (
    BusinessCreditFeatureSnapshot,
    MaterializationDiagnostic,
)


BusinessShadowStatus = Literal[
    "insufficient_data",
    "ready_not_called",
]


@dataclass(frozen=True)
class BusinessShadowAssessment:
    """Sanitized, model-free view of a business feature snapshot."""

    status: BusinessShadowStatus
    feature_version: str
    reliable_features: Tuple[str, ...]
    approximate_features: Tuple[str, ...]
    missing_features: Tuple[str, ...]
    policy_blocked_features: Tuple[str, ...]
    reliable_coverage_ratio: Optional[float]
    approximate_coverage_ratio: Optional[float]
    potential_coverage_ratio: Optional[float]
    six_month_coverage_complete: bool
    six_month_reliable_complete: bool
    statement_cycle_ids: Tuple[str, ...]
    diagnostics: Tuple[CreditRiskAuditDiagnostic, ...]


def adapt_business_credit_snapshot(
    snapshot: BusinessCreditFeatureSnapshot,
) -> BusinessShadowAssessment:
    """Map a validated business snapshot without changing feature statuses."""
    if not isinstance(snapshot, BusinessCreditFeatureSnapshot):
        raise TypeError("snapshot must be a BusinessCreditFeatureSnapshot")
    coverage = snapshot.coverage_summary
    sufficient = (
        not coverage.missing_features
        and not coverage.approximate_features
        and not coverage.policy_blocked_features
        and coverage.six_month_reliable_complete
    )
    diagnostics = []
    for diagnostic in snapshot.diagnostics:
        if not isinstance(diagnostic, MaterializationDiagnostic):
            diagnostics.append(
                CreditRiskAuditDiagnostic(
                    code="invalid_diagnostic",
                    severity="warning",
                )
            )
            continue
        diagnostics.append(
            CreditRiskAuditDiagnostic(
                code=diagnostic.code,
                feature=diagnostic.feature_key,
                count=diagnostic.count,
                severity=diagnostic.severity,
                statement_cycle_id=diagnostic.statement_cycle_id,
            )
        )
    return BusinessShadowAssessment(
        status="ready_not_called" if sufficient else "insufficient_data",
        feature_version=snapshot.feature_schema_version,
        reliable_features=tuple(coverage.reliable_features),
        approximate_features=tuple(coverage.approximate_features),
        missing_features=tuple(coverage.missing_features),
        policy_blocked_features=tuple(coverage.policy_blocked_features),
        reliable_coverage_ratio=coverage.reliable_coverage_ratio,
        approximate_coverage_ratio=coverage.approximate_coverage_ratio,
        potential_coverage_ratio=coverage.potential_coverage_ratio,
        six_month_coverage_complete=coverage.six_month_coverage_complete,
        six_month_reliable_complete=coverage.six_month_reliable_complete,
        statement_cycle_ids=tuple(
            cycle.statement_cycle_id for cycle in coverage.statement_cycles
        ),
        diagnostics=tuple(diagnostics),
    )
