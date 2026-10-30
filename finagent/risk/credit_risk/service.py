"""Read-only credit-risk shadow evaluation service."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Optional, Sequence

from finagent.data.models import Account, Card, Transaction, User
from finagent.risk.credit_risk.audit import (
    CreditRiskAuditDiagnostic,
    CreditRiskAuditRecord,
)
from finagent.risk.credit_risk.business_contract import (
    BusinessCreditFeatureSnapshot,
    BusinessFeatureContractError,
)
from finagent.risk.credit_risk.business_shadow_adapter import (
    BusinessShadowAssessment,
    adapt_business_credit_snapshot,
)
from finagent.risk.credit_risk.contract import (
    FEATURE_VERSION,
    CreditFeatureAvailability,
    CreditFeatureSnapshot,
    CustomerType,
    FeatureDiagnostic,
)
from finagent.risk.credit_risk.feature_builder import (
    build_credit_feature_snapshot,
    check_credit_feature_availability,
)
from finagent.risk.credit_risk.materializer import (
    BusinessFeatureMaterializer,
    MaterializerInputError,
)


_SUPPORTED_MODES = {"off", "shadow"}
# Configured research model identifier only. v0.1 never loads or calls it.
_RESEARCH_MODEL_CONFIG_NAME = "credit-risk-balanced-logistic"


@dataclass(frozen=True)
class CreditRiskShadowResult:
    """In-memory result of one shadow evaluation."""

    status: str
    snapshot: Optional[CreditFeatureSnapshot]
    availability: Optional[CreditFeatureAvailability]
    audit: CreditRiskAuditRecord
    rule_impact: str = field(default="none", init=False)


@dataclass(frozen=True)
class BusinessCreditShadowResult:
    """Read-only business snapshot shadow result."""

    status: str
    snapshot: Optional[BusinessCreditFeatureSnapshot]
    assessment: Optional[BusinessShadowAssessment]
    audit: CreditRiskAuditRecord
    rule_impact: str = field(default="none", init=False)


class CreditRiskService:
    """Run read-only feature availability checks without model inference."""

    def __init__(
        self,
        mode: str = "off",
        *,
        feature_builder: Callable[..., CreditFeatureSnapshot] = (
            build_credit_feature_snapshot
        ),
        availability_checker: Callable[
            [CreditFeatureSnapshot],
            CreditFeatureAvailability,
        ] = check_credit_feature_availability,
        now_provider: Optional[Callable[[], datetime]] = None,
        business_materializer: Optional[BusinessFeatureMaterializer] = None,
    ) -> None:
        normalized_mode = str(mode).strip().lower()
        if normalized_mode not in _SUPPORTED_MODES:
            raise ValueError("mode must be off or shadow")
        self.mode = normalized_mode
        self._feature_builder = feature_builder
        self._availability_checker = availability_checker
        self._now_provider = now_provider
        self._business_materializer = (
            business_materializer or BusinessFeatureMaterializer()
        )

    def evaluate_shadow(
        self,
        *,
        user: Optional[User],
        cards: Sequence[Card] = (),
        accounts: Sequence[Account] = (),
        transactions: Sequence[Transaction] = (),
        customer_type: Optional[CustomerType] = None,
        as_of: date | None = None,
    ) -> CreditRiskShadowResult:
        """Evaluate credit feature availability without model inference."""
        if self.mode == "off":
            return self._result(
                status="off",
                snapshot=None,
                availability=None,
                error_code="",
            )

        if user is None or not str(getattr(user, "user_id", "") or "").strip():
            return self._result(
                status="invalid_input",
                snapshot=None,
                availability=None,
                error_code="invalid_user_input",
            )
        if cards is None or accounts is None or transactions is None:
            return self._result(
                status="invalid_input",
                snapshot=None,
                availability=None,
                error_code="invalid_feature_input",
            )

        try:
            snapshot = self._feature_builder(
                user,
                cards=cards,
                accounts=accounts,
                transactions=transactions,
                customer_type=customer_type,
                as_of=as_of,
            )
        except (TypeError, ValueError, KeyError):
            return self._result(
                status="invalid_input",
                snapshot=None,
                availability=None,
                error_code="feature_builder_invalid_input",
            )
        except Exception:
            return self._result(
                status="shadow_error",
                snapshot=None,
                availability=None,
                error_code="feature_builder_exception",
            )

        if not isinstance(snapshot, CreditFeatureSnapshot):
            return self._result(
                status="shadow_error",
                snapshot=None,
                availability=None,
                error_code="feature_builder_invalid_output",
            )

        try:
            availability = self._availability_checker(snapshot)
        except Exception:
            return self._result(
                status="shadow_error",
                snapshot=snapshot,
                availability=None,
                error_code="availability_checker_exception",
            )

        if not isinstance(availability, CreditFeatureAvailability):
            return self._result(
                status="shadow_error",
                snapshot=snapshot,
                availability=None,
                error_code="availability_checker_invalid_output",
            )

        if (
            availability.status != "complete"
            or snapshot.approximate_features
            or snapshot.missing_features
        ):
            status = "insufficient_data"
            error_code = "feature_contract_incomplete"
        else:
            # v0.1 deliberately has no model adapter. Complete availability
            # still must not trigger inference without a trusted materializer.
            status = "model_not_called"
            error_code = "model_execution_disabled_v0_1"

        return self._result(
            status=status,
            snapshot=snapshot,
            availability=availability,
            error_code=error_code,
        )

    def _result(
        self,
        *,
        status: str,
        snapshot: Optional[CreditFeatureSnapshot],
        availability: Optional[CreditFeatureAvailability],
        error_code: str,
    ) -> CreditRiskShadowResult:
        feature_version = (
            snapshot.feature_version if snapshot is not None else FEATURE_VERSION
        )
        audit = CreditRiskAuditRecord(
            mode=self.mode,
            status=status,
            evaluated_at_utc=self._utc_now(),
            feature_version=feature_version,
            model_name=_RESEARCH_MODEL_CONFIG_NAME,
            model_version=None,
            artifact_sha256=None,
            reliable_coverage_ratio=(
                availability.reliable_coverage_ratio
                if availability is not None else None
            ),
            approximate_coverage_ratio=(
                availability.approximate_coverage_ratio
                if availability is not None else None
            ),
            potential_coverage_ratio=(
                availability.potential_coverage_ratio
                if availability is not None else None
            ),
            missing_features=(
                snapshot.missing_features if snapshot is not None else ()
            ),
            approximate_features=(
                snapshot.approximate_features if snapshot is not None else ()
            ),
            diagnostics=self._audit_diagnostics(snapshot),
            model_result_status="not_called",
            error_code=error_code,
        )
        return CreditRiskShadowResult(
            status=status,
            snapshot=snapshot,
            availability=availability,
            audit=audit,
        )

    def evaluate_business_shadow(
        self,
        *,
        user: Optional[User],
        cards: Sequence[Card] = (),
        accounts: Sequence[Account] = (),
        transactions: Sequence[Transaction] = (),
        customer_type: Optional[CustomerType] = None,
        as_of: Optional[datetime] = None,
    ) -> BusinessCreditShadowResult:
        """Evaluate a business feature snapshot without model inference."""
        if self.mode == "off":
            return self._business_result(
                status="off",
                snapshot=None,
                assessment=None,
                error_code="",
            )
        if user is None or not str(getattr(user, "user_id", "") or "").strip():
            return self._business_result(
                status="invalid_input",
                snapshot=None,
                assessment=None,
                error_code="invalid_user_input",
            )
        if cards is None or accounts is None or transactions is None:
            return self._business_result(
                status="invalid_input",
                snapshot=None,
                assessment=None,
                error_code="invalid_business_feature_input",
            )

        try:
            snapshot = self._business_materializer.materialize(
                user,
                cards=cards,
                accounts=accounts,
                transactions=transactions,
                as_of=as_of,
                customer_type=customer_type or "unknown",
            )
        except MaterializerInputError:
            return self._business_result(
                status="invalid_input",
                snapshot=None,
                assessment=None,
                error_code="materializer_invalid_input",
            )
        except BusinessFeatureContractError:
            return self._business_result(
                status="shadow_error",
                snapshot=None,
                assessment=None,
                error_code="business_contract_validation_error",
            )
        except Exception:
            return self._business_result(
                status="shadow_error",
                snapshot=None,
                assessment=None,
                error_code="business_materializer_exception",
            )

        if not isinstance(snapshot, BusinessCreditFeatureSnapshot):
            return self._business_result(
                status="shadow_error",
                snapshot=None,
                assessment=None,
                error_code="business_materializer_invalid_output",
            )
        try:
            assessment = adapt_business_credit_snapshot(snapshot)
        except Exception:
            return self._business_result(
                status="shadow_error",
                snapshot=snapshot,
                assessment=None,
                error_code="business_snapshot_adapter_error",
            )

        if assessment.status == "ready_not_called":
            return self._business_result(
                status="model_not_called",
                snapshot=snapshot,
                assessment=assessment,
                error_code="model_execution_disabled_v0_1",
            )
        return self._business_result(
            status="insufficient_data",
            snapshot=snapshot,
            assessment=assessment,
            error_code="business_feature_contract_incomplete",
        )

    def _business_result(
        self,
        *,
        status: str,
        snapshot: Optional[BusinessCreditFeatureSnapshot],
        assessment: Optional[BusinessShadowAssessment],
        error_code: str,
    ) -> BusinessCreditShadowResult:
        audit = CreditRiskAuditRecord(
            mode=self.mode,
            status=status,
            evaluated_at_utc=self._utc_now(),
            feature_version=(
                snapshot.feature_schema_version
                if snapshot is not None else "business_credit_features_v1"
            ),
            model_name=_RESEARCH_MODEL_CONFIG_NAME,
            model_version=None,
            artifact_sha256=None,
            reliable_coverage_ratio=(
                assessment.reliable_coverage_ratio
                if assessment is not None else None
            ),
            approximate_coverage_ratio=(
                assessment.approximate_coverage_ratio
                if assessment is not None else None
            ),
            potential_coverage_ratio=(
                assessment.potential_coverage_ratio
                if assessment is not None else None
            ),
            missing_features=(
                assessment.missing_features if assessment is not None else ()
            ),
            approximate_features=(
                assessment.approximate_features if assessment is not None else ()
            ),
            diagnostics=(
                assessment.diagnostics if assessment is not None else ()
            ),
            model_result_status="not_called",
            error_code=error_code,
            reliable_features=(
                assessment.reliable_features if assessment is not None else ()
            ),
            policy_blocked_features=(
                assessment.policy_blocked_features
                if assessment is not None else ()
            ),
            reliable_feature_count=(
                len(assessment.reliable_features)
                if assessment is not None else 0
            ),
            approximate_feature_count=(
                len(assessment.approximate_features)
                if assessment is not None else 0
            ),
            missing_feature_count=(
                len(assessment.missing_features)
                if assessment is not None else 0
            ),
            policy_blocked_feature_count=(
                len(assessment.policy_blocked_features)
                if assessment is not None else 0
            ),
            six_month_coverage_complete=(
                assessment.six_month_coverage_complete
                if assessment is not None else None
            ),
            six_month_reliable_complete=(
                assessment.six_month_reliable_complete
                if assessment is not None else None
            ),
            statement_cycle_ids=(
                assessment.statement_cycle_ids if assessment is not None else ()
            ),
        )
        return BusinessCreditShadowResult(
            status=status,
            snapshot=snapshot,
            assessment=assessment,
            audit=audit,
        )

    @staticmethod
    def _audit_diagnostics(
        snapshot: Optional[CreditFeatureSnapshot],
    ) -> tuple[CreditRiskAuditDiagnostic, ...]:
        if snapshot is None:
            return ()
        sanitized = []
        for diagnostic in snapshot.diagnostics:
            if not isinstance(diagnostic, FeatureDiagnostic):
                sanitized.append(
                    CreditRiskAuditDiagnostic(
                        code="invalid_diagnostic",
                        feature="",
                        count=1,
                    )
                )
                continue
            sanitized.append(
                CreditRiskAuditDiagnostic(
                    code=diagnostic.code,
                    feature=diagnostic.feature,
                    count=diagnostic.count,
                )
            )
        return tuple(sanitized)

    def _utc_now(self) -> str:
        now = self._now_provider() if self._now_provider else datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc).isoformat(timespec="seconds")
