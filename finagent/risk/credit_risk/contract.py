"""Read-only contracts for credit-risk feature availability."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Mapping, Tuple


FEATURE_VERSION = "business-credit-features-v1"

UCI_FEATURE_ORDER: Tuple[str, ...] = tuple(
    f"X{index}" for index in range(1, 24)
)

CustomerType = Literal["new", "existing", "unknown"]
AvailabilityStatus = Literal["complete", "partial", "insufficient"]

_CUSTOMER_TYPES = {"new", "existing", "unknown"}
_AVAILABILITY_STATUSES = {"complete", "partial", "insufficient"}


class FeatureContractError(ValueError):
    """Raised when a credit feature contract is internally inconsistent."""


@dataclass(frozen=True)
class FeatureDiagnostic:
    """A non-sensitive, structured reason for unavailable feature data."""

    code: str
    message: str
    feature: str = ""
    count: int = 1

    def __post_init__(self) -> None:
        code = str(self.code).strip()
        message = str(self.message).strip()
        feature = str(self.feature).strip()
        count = int(self.count)
        if not code:
            raise FeatureContractError("diagnostic code must not be empty")
        if not message:
            raise FeatureContractError("diagnostic message must not be empty")
        if count < 1:
            raise FeatureContractError("diagnostic count must be at least 1")
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "feature", feature)
        object.__setattr__(self, "count", count)


def _normalize_feature_tuple(
    values: Tuple[str, ...],
    *,
    field_name: str,
) -> Tuple[str, ...]:
    normalized = tuple(values)
    unknown = sorted(set(normalized) - set(UCI_FEATURE_ORDER))
    if unknown:
        raise FeatureContractError(
            f"{field_name} contains unknown features: {', '.join(unknown)}"
        )
    if len(normalized) != len(set(normalized)):
        raise FeatureContractError(
            f"{field_name} contains duplicate features"
        )
    return normalized


def _validate_partition(
    available: Tuple[str, ...],
    approximate: Tuple[str, ...],
    missing: Tuple[str, ...],
) -> None:
    combined = available + approximate + missing
    if len(combined) != len(set(combined)):
        raise FeatureContractError(
            "Feature categories must not overlap"
        )
    if set(combined) != set(UCI_FEATURE_ORDER):
        absent = sorted(set(UCI_FEATURE_ORDER) - set(combined))
        extra = sorted(set(combined) - set(UCI_FEATURE_ORDER))
        detail = []
        if absent:
            detail.append("absent: " + ", ".join(absent))
        if extra:
            detail.append("unexpected: " + ", ".join(extra))
        raise FeatureContractError(
            "Feature categories must partition all UCI features ("
            + "; ".join(detail)
            + ")"
        )


@dataclass(frozen=True)
class CreditFeatureSnapshot:
    """Business-data availability for the 23 UCI credit-risk features.

    ``available_features`` means a reliable source exists. It does not contain
    feature values and does not imply that the UCI model is usable.
    """

    customer_id: str
    customer_type: CustomerType
    feature_version: str
    available_features: Tuple[str, ...]
    approximate_features: Tuple[str, ...]
    missing_features: Tuple[str, ...]
    source_metadata: Mapping[str, Any] = field(default_factory=dict)
    diagnostics: Tuple[FeatureDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        customer_type = str(self.customer_type).strip().lower()
        if customer_type not in _CUSTOMER_TYPES:
            raise FeatureContractError(
                "customer_type must be new, existing, or unknown"
            )
        object.__setattr__(self, "customer_type", customer_type)

        customer_id = str(self.customer_id).strip()
        if not customer_id:
            raise FeatureContractError("customer_id must not be empty")
        object.__setattr__(self, "customer_id", customer_id)

        feature_version = str(self.feature_version).strip()
        if not feature_version:
            raise FeatureContractError("feature_version must not be empty")
        object.__setattr__(self, "feature_version", feature_version)

        available = _normalize_feature_tuple(
            self.available_features,
            field_name="available_features",
        )
        approximate = _normalize_feature_tuple(
            self.approximate_features,
            field_name="approximate_features",
        )
        missing = _normalize_feature_tuple(
            self.missing_features,
            field_name="missing_features",
        )
        _validate_partition(available, approximate, missing)
        object.__setattr__(self, "available_features", available)
        object.__setattr__(self, "approximate_features", approximate)
        object.__setattr__(self, "missing_features", missing)
        object.__setattr__(self, "source_metadata", dict(self.source_metadata))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))


@dataclass(frozen=True)
class CreditFeatureAvailability:
    """Summary derived from a feature snapshot.

    The three coverage ratios always use all 23 features as the denominator.
    Approximate features are never included in ``reliable_coverage_ratio``.
    """

    status: AvailabilityStatus
    reliable_coverage_ratio: float
    approximate_coverage_ratio: float
    potential_coverage_ratio: float
    available_features: Tuple[str, ...]
    approximate_features: Tuple[str, ...]
    missing_features: Tuple[str, ...]
    warnings: Tuple[str, ...]
    diagnostics: Tuple[FeatureDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        status = str(self.status).strip().lower()
        if status not in _AVAILABILITY_STATUSES:
            raise FeatureContractError(
                "status must be complete, partial, or insufficient"
            )
        object.__setattr__(self, "status", status)

        reliable_ratio = float(self.reliable_coverage_ratio)
        approximate_ratio = float(self.approximate_coverage_ratio)
        potential_ratio = float(self.potential_coverage_ratio)
        for name, ratio in (
            ("reliable_coverage_ratio", reliable_ratio),
            ("approximate_coverage_ratio", approximate_ratio),
            ("potential_coverage_ratio", potential_ratio),
        ):
            if not math.isfinite(ratio) or not 0.0 <= ratio <= 1.0:
                raise FeatureContractError(
                    f"{name} must be between 0.0 and 1.0"
                )
        if not math.isclose(
            potential_ratio,
            reliable_ratio + approximate_ratio,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise FeatureContractError(
                "potential_coverage_ratio must equal reliable plus approximate"
            )
        object.__setattr__(self, "reliable_coverage_ratio", reliable_ratio)
        object.__setattr__(self, "approximate_coverage_ratio", approximate_ratio)
        object.__setattr__(self, "potential_coverage_ratio", potential_ratio)

        available = _normalize_feature_tuple(
            self.available_features,
            field_name="available_features",
        )
        approximate = _normalize_feature_tuple(
            self.approximate_features,
            field_name="approximate_features",
        )
        missing = _normalize_feature_tuple(
            self.missing_features,
            field_name="missing_features",
        )
        _validate_partition(available, approximate, missing)
        object.__setattr__(self, "available_features", available)
        object.__setattr__(self, "approximate_features", approximate)
        object.__setattr__(self, "missing_features", missing)
        object.__setattr__(self, "warnings", tuple(str(item) for item in self.warnings))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
