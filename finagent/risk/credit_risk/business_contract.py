"""Immutable contracts for business credit-risk feature materialization."""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from types import MappingProxyType
from typing import Literal, Mapping, Optional, Tuple, Union

from finagent.risk.credit_risk.contract import CustomerType


FeatureStatus = Literal["reliable", "approximate", "missing", "policy_blocked"]
FeatureSourceType = Literal[
    "user_profile",
    "card_snapshot",
    "account_snapshot",
    "transaction_ledger",
    "formal_statement",
    "repayment_record",
    "credit_application",
]
DiagnosticSeverity = Literal["info", "warning", "error"]
AggregationScope = Literal["customer", "card", "account"]
FeatureValue = Union[str, int, float]

BUSINESS_FEATURE_KEYS: Tuple[str, ...] = (
    "current_credit_limit",
    "gender",
    "education_level",
    "marital_status",
    "verified_age",
    "repayment_status_1",
    "repayment_status_2",
    "repayment_status_3",
    "repayment_status_4",
    "repayment_status_5",
    "repayment_status_6",
    "statement_amount_1",
    "statement_amount_2",
    "statement_amount_3",
    "statement_amount_4",
    "statement_amount_5",
    "statement_amount_6",
    "payment_amount_1",
    "payment_amount_2",
    "payment_amount_3",
    "payment_amount_4",
    "payment_amount_5",
    "payment_amount_6",
)

_FEATURE_KEYS = frozenset(BUSINESS_FEATURE_KEYS)
_STATUS_VALUES = frozenset({"reliable", "approximate", "missing", "policy_blocked"})
_SOURCE_TYPES = frozenset(
    {
        "user_profile",
        "card_snapshot",
        "account_snapshot",
        "transaction_ledger",
        "formal_statement",
        "repayment_record",
        "credit_application",
    }
)
_SEVERITIES = frozenset({"info", "warning", "error"})
_USER_FEATURES = frozenset(
    {"gender", "education_level", "marital_status", "verified_age"}
)
_PERIOD_FEATURES = frozenset(
    feature
    for feature in BUSINESS_FEATURE_KEYS
    if feature.startswith(
        ("repayment_status_", "statement_amount_", "payment_amount_")
    )
)
_MONETARY_FEATURES = frozenset(
    feature
    for feature in BUSINESS_FEATURE_KEYS
    if feature == "current_credit_limit"
    or feature.startswith(("statement_amount_", "payment_amount_"))
)
_CARD_CONTEXT_FEATURES = frozenset(
    feature for feature in BUSINESS_FEATURE_KEYS if feature not in _USER_FEATURES
)
_ACCOUNT_BOUND_SOURCE_TYPES = frozenset(
    {"account_snapshot", "transaction_ledger", "repayment_record"}
)
_HISTORY_FEATURE_GROUPS = {
    index: (
        f"repayment_status_{index}",
        f"statement_amount_{index}",
        f"payment_amount_{index}",
    )
    for index in range(1, 7)
}
_SOURCE_TYPES_BY_FEATURE = {
    **{
        feature: frozenset({"user_profile", "credit_application"})
        for feature in _USER_FEATURES
    },
    "current_credit_limit": frozenset({"card_snapshot", "account_snapshot"}),
    **{
        feature: frozenset({"formal_statement"})
        for feature in BUSINESS_FEATURE_KEYS
        if feature.startswith("repayment_status_")
    },
    **{
        feature: frozenset({"formal_statement"})
        for feature in BUSINESS_FEATURE_KEYS
        if feature.startswith("statement_amount_")
    },
    **{
        feature: frozenset(
            {"repayment_record", "transaction_ledger", "formal_statement"}
        )
        for feature in BUSINESS_FEATURE_KEYS
        if feature.startswith("payment_amount_")
    },
}

_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_MIN_STATEMENT_CYCLE_DAYS = 25
_MAX_STATEMENT_CYCLE_DAYS = 35
_MAX_LATEST_CYCLE_AGE_DAYS = 31

# Project policy, not a regulatory rule. A complete history uses statement
# cycles, not natural months, with a practical monthly-cycle duration.
_SUPPORTED_ISO_4217 = frozenset(
    {
        "AED", "ARS", "AUD", "BRL", "CAD", "CHF", "CLP", "CNY", "COP",
        "CZK", "DKK", "EGP", "EUR", "GBP", "HKD", "HUF", "IDR", "ILS",
        "INR", "ISK", "JPY", "KRW", "KWD", "MAD", "MXN", "MYR", "NOK",
        "NZD", "PHP", "PKR", "PLN", "QAR", "RON", "RUB", "SAR", "SEK",
        "SGD", "THB", "TRY", "TWD", "UAH", "USD", "VND", "ZAR",
    }
)


class BusinessFeatureContractError(ValueError):
    """Raised when a business feature contract is internally inconsistent."""


def _validate_code(value: object, field_name: str, *, required: bool = True) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise BusinessFeatureContractError(f"{field_name} must not be empty")
    if text and not _CODE_PATTERN.fullmatch(text):
        raise BusinessFeatureContractError(
            f"{field_name} must use the internal code character set"
        )
    return text


def _validate_uuid_ref(
    value: object,
    field_name: str,
    prefix: str,
) -> str:
    text = str(value or "").strip()
    expected_prefix = f"{prefix}_"
    if not text.startswith(expected_prefix):
        raise BusinessFeatureContractError(
            f"{field_name} must use the {expected_prefix}<uuid> format"
        )
    try:
        parsed = uuid.UUID(text[len(expected_prefix):])
    except (ValueError, AttributeError) as exc:
        raise BusinessFeatureContractError(
            f"{field_name} must use the {expected_prefix}<uuid> format"
        ) from exc
    return f"{expected_prefix}{str(parsed)}"


def _normalize_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise BusinessFeatureContractError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise BusinessFeatureContractError(
            f"{field_name} must be timezone-aware"
        )
    return value.astimezone(timezone.utc)


def _validate_date_range(
    period_start: Optional[date],
    period_end: Optional[date],
    field_name: str,
) -> None:
    if (period_start is None) != (period_end is None):
        raise BusinessFeatureContractError(
            f"{field_name} requires both period_start and period_end"
        )
    if period_start is not None and period_end is not None:
        if period_start > period_end:
            raise BusinessFeatureContractError(
                f"{field_name} period_start must not be after period_end"
            )


def _validate_feature_value(value: object) -> Optional[FeatureValue]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise BusinessFeatureContractError(
            "feature value must be str, int, float, or None"
        )
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            raise BusinessFeatureContractError("string feature value must not be empty")
        return normalized
    number = float(value)
    if not math.isfinite(number):
        raise BusinessFeatureContractError(
            "numeric feature value must be finite"
        )
    return value


def _history_slot(feature_key: str) -> Optional[int]:
    match = re.fullmatch(
        r"(?:repayment_status|statement_amount|payment_amount)_([1-6])",
        feature_key,
    )
    return int(match.group(1)) if match else None


@dataclass(frozen=True)
class StatementCyclePolicy:
    """Project-level statement-cycle duration policy."""

    min_days: int = _MIN_STATEMENT_CYCLE_DAYS
    max_days: int = _MAX_STATEMENT_CYCLE_DAYS

    def __post_init__(self) -> None:
        if self.min_days <= 0 or self.max_days < self.min_days:
            raise BusinessFeatureContractError("invalid statement cycle policy")

    def validate_duration(self, days: int) -> None:
        if not self.min_days <= days <= self.max_days:
            raise BusinessFeatureContractError(
                "statement cycle duration is outside the project policy"
            )


DEFAULT_STATEMENT_CYCLE_POLICY = StatementCyclePolicy()


@dataclass(frozen=True)
class StatementCycle:
    """A uniquely identified, non-overlapping statement billing cycle."""

    statement_cycle_id: str
    period_start: date
    period_end: date

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "statement_cycle_id",
            _validate_uuid_ref(
                self.statement_cycle_id,
                "statement_cycle_id",
                "CYCLE",
            ),
        )
        _validate_date_range(self.period_start, self.period_end, "statement cycle")
        DEFAULT_STATEMENT_CYCLE_POLICY.validate_duration(
            (self.period_end - self.period_start).days + 1
        )


@dataclass(frozen=True)
class FeatureSource:
    """Non-sensitive provenance descriptor for one business feature.

    Ownership, currency and completeness flags are assertions supplied by a
    trusted upstream verifier. This object does not independently prove them.
    """

    source_type: FeatureSourceType
    source_schema_version: str
    observed_at: datetime
    source_record_ref: str
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    customer_id_matches: bool = False
    account_id_matches: bool = False
    card_id_matches: bool = False
    currency_verified: bool = False
    currency_code: Optional[str] = None
    data_complete: bool = False
    source_batch_ref: Optional[str] = None
    statement_cycle_id: Optional[str] = None

    def __post_init__(self) -> None:
        source_type = str(self.source_type).strip().lower()
        if source_type not in _SOURCE_TYPES:
            raise BusinessFeatureContractError("invalid source_type")
        object.__setattr__(self, "source_type", source_type)
        object.__setattr__(
            self,
            "source_schema_version",
            _validate_code(self.source_schema_version, "source_schema_version"),
        )
        object.__setattr__(
            self,
            "observed_at",
            _normalize_utc(self.observed_at, "observed_at"),
        )
        object.__setattr__(
            self,
            "source_record_ref",
            _validate_uuid_ref(
                self.source_record_ref,
                "source_record_ref",
                "SRC",
            ),
        )
        _validate_date_range(self.period_start, self.period_end, "source")
        if self.source_batch_ref is not None:
            object.__setattr__(
                self,
                "source_batch_ref",
                _validate_uuid_ref(self.source_batch_ref, "source_batch_ref", "UPL"),
            )
        if self.statement_cycle_id is not None:
            object.__setattr__(
                self,
                "statement_cycle_id",
                _validate_uuid_ref(
                    self.statement_cycle_id,
                    "statement_cycle_id",
                    "CYCLE",
                ),
            )
            if self.period_start is None or self.period_end is None:
                raise BusinessFeatureContractError(
                    "statement_cycle_id requires source period dates"
                )
        if self.currency_code is not None:
            currency_code = str(self.currency_code).strip().upper()
            if (
                not _CURRENCY_PATTERN.fullmatch(currency_code)
                or currency_code not in _SUPPORTED_ISO_4217
            ):
                raise BusinessFeatureContractError(
                    "currency_code must be a supported ISO 4217 code"
                )
            if not self.currency_verified:
                raise BusinessFeatureContractError(
                    "currency_code requires currency_verified=True"
                )
            object.__setattr__(self, "currency_code", currency_code)


@dataclass(frozen=True)
class MaterializationDiagnostic:
    """Structured diagnostic without free-text or raw business content."""

    code: str
    feature_key: str = ""
    severity: DiagnosticSeverity = "warning"
    period: Optional[str] = None
    count: int = 1
    source_type: Optional[FeatureSourceType] = None
    source_ref: Optional[str] = None
    statement_cycle_id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _validate_code(self.code, "diagnostic code"))
        feature_key = str(self.feature_key or "").strip()
        if feature_key and feature_key not in _FEATURE_KEYS:
            raise BusinessFeatureContractError("invalid diagnostic feature_key")
        object.__setattr__(self, "feature_key", feature_key)
        severity = str(self.severity).strip().lower()
        if severity not in _SEVERITIES:
            raise BusinessFeatureContractError("invalid diagnostic severity")
        object.__setattr__(self, "severity", severity)
        if self.period is not None and not _PERIOD_PATTERN.fullmatch(str(self.period)):
            raise BusinessFeatureContractError(
                "diagnostic period must use YYYY-MM"
            )
        count = int(self.count)
        if count < 1:
            raise BusinessFeatureContractError("diagnostic count must be positive")
        object.__setattr__(self, "count", count)
        if self.source_type is not None:
            source_type = str(self.source_type).strip().lower()
            if source_type not in _SOURCE_TYPES:
                raise BusinessFeatureContractError("invalid diagnostic source_type")
            object.__setattr__(self, "source_type", source_type)
        if self.source_ref is not None:
            object.__setattr__(
                self,
                "source_ref",
                _validate_uuid_ref(self.source_ref, "source_ref", "SRC"),
            )
        if self.statement_cycle_id is not None:
            object.__setattr__(
                self,
                "statement_cycle_id",
                _validate_uuid_ref(
                    self.statement_cycle_id,
                    "statement_cycle_id",
                    "CYCLE",
                ),
            )
        if self.period is not None and self.statement_cycle_id is not None:
            raise BusinessFeatureContractError(
                "diagnostic must not use both period and statement_cycle_id"
            )
        if not feature_key and (
            self.period is not None
            or self.source_type is not None
            or self.source_ref is not None
            or self.statement_cycle_id is not None
        ):
            raise BusinessFeatureContractError(
                "global diagnostics require empty feature, period, source, and cycle"
            )


@dataclass(frozen=True)
class MaterializedFeatureValue:
    """One validated business feature value and its provenance.

    For static profile fields, ``completeness`` represents evidence completeness
    at the snapshot time. For historical fields, it represents completeness for
    that specific statement cycle. Overall six-cycle completeness is represented
    separately by ``FeatureAvailability.six_month_history_complete``.
    """

    feature_key: str
    value: Optional[FeatureValue]
    unit: str
    status: FeatureStatus
    availability_reason: str
    sources: Tuple[FeatureSource, ...]
    aggregation_scope: AggregationScope = "customer"
    card_ref: Optional[str] = None
    account_ref: Optional[str] = None
    aggregation_strategy: Optional[str] = None
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    completeness: Optional[float] = None
    approximation_method: Optional[str] = None
    statement_cycle_id: Optional[str] = None

    def __post_init__(self) -> None:
        feature_key = str(self.feature_key).strip()
        if feature_key not in _FEATURE_KEYS:
            raise BusinessFeatureContractError("invalid business feature_key")
        object.__setattr__(self, "feature_key", feature_key)

        status = str(self.status).strip().lower()
        if status not in _STATUS_VALUES:
            raise BusinessFeatureContractError("invalid feature status")
        object.__setattr__(self, "status", status)

        value = _validate_feature_value(self.value)
        object.__setattr__(self, "value", value)
        unit = str(self.unit or "").strip()
        reason = _validate_code(
            self.availability_reason,
            "availability_reason",
            required=status in {"missing", "policy_blocked"},
        )
        object.__setattr__(self, "unit", unit)
        object.__setattr__(self, "availability_reason", reason)

        sources = tuple(self.sources)
        if any(not isinstance(source, FeatureSource) for source in sources):
            raise BusinessFeatureContractError(
                "sources must contain FeatureSource objects"
            )
        source_identities = [
            (source.source_type, source.source_record_ref)
            for source in sources
        ]
        if len(source_identities) != len(set(source_identities)):
            raise BusinessFeatureContractError("duplicate feature sources are not allowed")
        object.__setattr__(self, "sources", sources)
        scope = str(self.aggregation_scope).strip().lower()
        if scope not in {"customer", "card", "account"}:
            raise BusinessFeatureContractError("invalid aggregation_scope")
        object.__setattr__(self, "aggregation_scope", scope)
        card_ref = self.card_ref
        account_ref = self.account_ref
        if card_ref is not None:
            card_ref = _validate_uuid_ref(card_ref, "card_ref", "CARD")
        if account_ref is not None:
            account_ref = _validate_uuid_ref(account_ref, "account_ref", "ACCT")
        object.__setattr__(self, "card_ref", card_ref)
        object.__setattr__(self, "account_ref", account_ref)
        aggregation_strategy = self.aggregation_strategy
        if aggregation_strategy is not None:
            aggregation_strategy = _validate_code(
                aggregation_strategy,
                "aggregation_strategy",
            )
        object.__setattr__(self, "aggregation_strategy", aggregation_strategy)
        if scope == "customer":
            if card_ref is not None or account_ref is not None:
                raise BusinessFeatureContractError(
                    "customer scope must not define card_ref or account_ref"
                )
            if len(sources) > 1 and aggregation_strategy is None:
                raise BusinessFeatureContractError(
                    "customer-level multi-source aggregation requires a strategy"
                )
        elif scope == "card":
            if card_ref is None or account_ref is not None:
                raise BusinessFeatureContractError(
                    "card scope requires card_ref only"
                )
        else:
            if account_ref is None or card_ref is not None:
                raise BusinessFeatureContractError(
                    "account scope requires account_ref only"
                )
        _validate_date_range(self.period_start, self.period_end, "feature")
        cycle_id = self.statement_cycle_id
        if cycle_id is not None:
            cycle_id = _validate_uuid_ref(
                cycle_id,
                "statement_cycle_id",
                "CYCLE",
            )
        object.__setattr__(self, "statement_cycle_id", cycle_id)
        if feature_key in _PERIOD_FEATURES:
            if self.period_start is None or self.period_end is None:
                raise BusinessFeatureContractError(
                    f"{feature_key} requires an explicit period"
                )
            if cycle_id is None:
                raise BusinessFeatureContractError(
                    f"{feature_key} requires statement_cycle_id"
                )
        elif cycle_id is not None:
            raise BusinessFeatureContractError(
                "statement_cycle_id is only valid for historical features"
            )
        if self.completeness is not None:
            completeness = float(self.completeness)
            if not math.isfinite(completeness) or not 0.0 <= completeness <= 1.0:
                raise BusinessFeatureContractError(
                    "completeness must be between 0.0 and 1.0"
                )
            object.__setattr__(self, "completeness", completeness)

        approximation_method = self.approximation_method
        if approximation_method is not None:
            approximation_method = _validate_code(
                approximation_method,
                "approximation_method",
            )
        object.__setattr__(self, "approximation_method", approximation_method)

        if status in {"missing", "policy_blocked"}:
            if value is not None:
                raise BusinessFeatureContractError(
                    f"{status} feature value must be None"
                )
            if self.completeness is not None:
                raise BusinessFeatureContractError(
                    f"{status} feature completeness must be None"
                )
            if approximation_method is not None:
                raise BusinessFeatureContractError(
                    f"{status} feature must not define approximation_method"
                )
            return

        if value is None:
            raise BusinessFeatureContractError(
                f"{status} feature requires a value"
            )
        if not unit:
            raise BusinessFeatureContractError(
                f"{status} feature requires a unit"
            )
        if not sources:
            raise BusinessFeatureContractError(
                f"{status} feature requires at least one source"
            )
        if not all(source.customer_id_matches for source in sources):
            raise BusinessFeatureContractError(
                "feature sources must explicitly match the customer"
            )
        allowed_sources = _SOURCE_TYPES_BY_FEATURE[feature_key]
        if any(source.source_type not in allowed_sources for source in sources):
            raise BusinessFeatureContractError(
                "feature contains an incompatible source_type"
            )
        if feature_key in _CARD_CONTEXT_FEATURES and not all(
            source.card_id_matches for source in sources
        ):
            raise BusinessFeatureContractError(
                "card-related features require card ownership verification"
            )
        if any(
            source.source_type in _ACCOUNT_BOUND_SOURCE_TYPES
            and not source.account_id_matches
            for source in sources
        ):
            raise BusinessFeatureContractError(
                "account-bound sources require account ownership verification"
            )
        if scope == "card" and not all(
            source.card_id_matches for source in sources
        ):
            raise BusinessFeatureContractError(
                "card aggregation scope requires card ownership verification"
            )
        if scope == "account" and not all(
            source.account_id_matches for source in sources
        ):
            raise BusinessFeatureContractError(
                "account aggregation scope requires account ownership verification"
            )
        if feature_key in _PERIOD_FEATURES:
            for source in sources:
                if (
                    source.statement_cycle_id != cycle_id
                    or source.period_start != self.period_start
                    or source.period_end != self.period_end
                ):
                    raise BusinessFeatureContractError(
                        "historical source period must match the feature cycle"
                    )

        if feature_key in _MONETARY_FEATURES:
            currencies = [source.currency_code for source in sources]
            if any(currency is None for currency in currencies):
                raise BusinessFeatureContractError(
                    "monetary feature sources require currency_code"
                )
            if len(set(currencies)) != 1:
                raise BusinessFeatureContractError(
                    "monetary feature sources must use one currency"
                )
            currency = str(currencies[0])
            if unit.upper() != currency:
                raise BusinessFeatureContractError(
                    "monetary feature unit must match source currency"
                )

        if status == "reliable":
            if self.completeness != 1.0:
                raise BusinessFeatureContractError(
                    "reliable feature requires completeness 1.0"
                )
            if not all(source.data_complete for source in sources):
                raise BusinessFeatureContractError(
                    "reliable feature requires complete sources"
                )
            if approximation_method is not None:
                raise BusinessFeatureContractError(
                    "reliable feature must not define approximation_method"
                )
        elif status == "approximate":
            if self.completeness is None:
                raise BusinessFeatureContractError(
                    "approximate feature requires completeness"
                )
            if approximation_method is None:
                raise BusinessFeatureContractError(
                    "approximate feature requires approximation_method"
                )


@dataclass(frozen=True)
class FeatureAvailability:
    """Status partition and optional coverage metrics for business features.

    This object validates statement-cycle structure but does not own an
    ``as_of`` value. A standalone instance has therefore not passed Snapshot's
    future-date checks and must not be treated as time-validated on its own.
    """

    reliable_features: Tuple[str, ...]
    approximate_features: Tuple[str, ...]
    missing_features: Tuple[str, ...]
    policy_blocked_features: Tuple[str, ...]
    reliable_coverage_ratio: Optional[float]
    approximate_coverage_ratio: Optional[float]
    potential_coverage_ratio: Optional[float]
    six_month_coverage_complete: bool
    six_month_reliable_complete: bool
    statement_cycles: Tuple[StatementCycle, ...] = ()
    completeness_by_period: Mapping[str, Optional[float]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        categories = {
            "reliable_features": tuple(self.reliable_features),
            "approximate_features": tuple(self.approximate_features),
            "missing_features": tuple(self.missing_features),
            "policy_blocked_features": tuple(self.policy_blocked_features),
        }
        combined = []
        for field_name, values in categories.items():
            for feature in values:
                if feature not in _FEATURE_KEYS:
                    raise BusinessFeatureContractError(
                        f"{field_name} contains an invalid feature"
                    )
                combined.append(feature)
            object.__setattr__(self, field_name, values)
        if len(combined) != len(set(combined)):
            raise BusinessFeatureContractError(
                "feature availability categories must not overlap"
            )
        if set(combined) != _FEATURE_KEYS:
            raise BusinessFeatureContractError(
                "feature availability categories must partition all business features"
            )

        ratios = (
            self.reliable_coverage_ratio,
            self.approximate_coverage_ratio,
            self.potential_coverage_ratio,
        )
        if any(ratio is None for ratio in ratios) and not all(
            ratio is None for ratio in ratios
        ):
            raise BusinessFeatureContractError(
                "coverage ratios must be all None or all calculated"
            )
        if all(ratio is not None for ratio in ratios):
            reliable = float(self.reliable_coverage_ratio)
            approximate = float(self.approximate_coverage_ratio)
            potential = float(self.potential_coverage_ratio)
            for name, ratio in (
                ("reliable_coverage_ratio", reliable),
                ("approximate_coverage_ratio", approximate),
                ("potential_coverage_ratio", potential),
            ):
                if not math.isfinite(ratio) or not 0.0 <= ratio <= 1.0:
                    raise BusinessFeatureContractError(
                        f"{name} must be between 0.0 and 1.0"
                    )
            expected_total = len(BUSINESS_FEATURE_KEYS)
            if not math.isclose(
                reliable,
                len(categories["reliable_features"]) / expected_total,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise BusinessFeatureContractError(
                    "reliable_coverage_ratio does not match reliable features"
                )
            if not math.isclose(
                approximate,
                len(categories["approximate_features"]) / expected_total,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise BusinessFeatureContractError(
                    "approximate_coverage_ratio does not match approximate features"
                )
            if not math.isclose(
                potential,
                reliable + approximate,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise BusinessFeatureContractError(
                    "potential_coverage_ratio must equal reliable plus approximate"
                )
            object.__setattr__(self, "reliable_coverage_ratio", reliable)
            object.__setattr__(self, "approximate_coverage_ratio", approximate)
            object.__setattr__(self, "potential_coverage_ratio", potential)

        cycles = tuple(self.statement_cycles)
        if any(not isinstance(cycle, StatementCycle) for cycle in cycles):
            raise BusinessFeatureContractError(
                "statement_cycles must contain StatementCycle objects"
            )
        if len({cycle.statement_cycle_id for cycle in cycles}) != len(cycles):
            raise BusinessFeatureContractError(
                "statement_cycle_id values must be unique"
            )
        if len({(cycle.period_start, cycle.period_end) for cycle in cycles}) != len(cycles):
            raise BusinessFeatureContractError(
                "statement cycle date ranges must be unique"
            )
        sorted_cycles = tuple(sorted(cycles, key=lambda cycle: cycle.period_start))
        for previous, current in zip(sorted_cycles, sorted_cycles[1:]):
            if current.period_start <= previous.period_end:
                raise BusinessFeatureContractError(
                    "statement cycles must not overlap"
                )
        if (
            self.six_month_reliable_complete
            and not self.six_month_coverage_complete
        ):
            raise BusinessFeatureContractError(
                "reliable completion requires coverage completion"
            )
        if self.six_month_coverage_complete:
            if len(sorted_cycles) != 6:
                raise BusinessFeatureContractError(
                    "six_month_coverage_complete requires exactly six statement cycles"
                )
            for previous, current in zip(sorted_cycles, sorted_cycles[1:]):
                if current.period_start != previous.period_end + timedelta(days=1):
                    raise BusinessFeatureContractError(
                        "coverage completion requires contiguous statement cycles"
                    )
        object.__setattr__(self, "statement_cycles", sorted_cycles)

        normalized_periods: dict[str, Optional[float]] = {}
        for cycle_id, completeness in self.completeness_by_period.items():
            normalized_cycle_id = _validate_uuid_ref(
                cycle_id,
                "completeness_by_period key",
                "CYCLE",
            )
            if normalized_cycle_id not in {
                cycle.statement_cycle_id for cycle in sorted_cycles
            }:
                raise BusinessFeatureContractError(
                    "completeness_by_period key must reference a declared cycle"
                )
            if completeness is None:
                normalized_periods[normalized_cycle_id] = None
                continue
            completeness_value = float(completeness)
            if (
                not math.isfinite(completeness_value)
                or not 0.0 <= completeness_value <= 1.0
            ):
                raise BusinessFeatureContractError(
                    "period completeness must be between 0.0 and 1.0"
                )
            normalized_periods[normalized_cycle_id] = completeness_value
        if self.six_month_coverage_complete:
            expected_ids = {
                cycle.statement_cycle_id for cycle in sorted_cycles
            }
            if set(normalized_periods) != expected_ids:
                raise BusinessFeatureContractError(
                    "coverage completion requires completeness for every cycle"
                )
        if self.six_month_reliable_complete:
            if any(value != 1.0 for value in normalized_periods.values()):
                raise BusinessFeatureContractError(
                    "reliable completion requires complete periods"
                )
        object.__setattr__(
            self,
            "completeness_by_period",
            MappingProxyType(normalized_periods),
        )


@dataclass(frozen=True)
class BusinessCreditFeatureSnapshot:
    """One read-only business credit feature materialization result."""

    customer_ref: str
    customer_type: CustomerType
    as_of: datetime
    feature_schema_version: str
    features: Mapping[str, MaterializedFeatureValue]
    coverage_summary: FeatureAvailability
    source_manifest: Tuple[FeatureSource, ...]
    diagnostics: Tuple[MaterializationDiagnostic, ...]
    materialization_run_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "customer_ref",
            _validate_uuid_ref(self.customer_ref, "customer_ref", "CUST"),
        )
        customer_type = str(self.customer_type).strip().lower()
        if customer_type not in {"new", "existing", "unknown"}:
            raise BusinessFeatureContractError("invalid customer_type")
        object.__setattr__(self, "customer_type", customer_type)
        as_of = _normalize_utc(self.as_of, "as_of")
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(
            self,
            "feature_schema_version",
            _validate_code(
                self.feature_schema_version,
                "feature_schema_version",
            ),
        )
        object.__setattr__(
            self,
            "materialization_run_ref",
            _validate_uuid_ref(
                self.materialization_run_ref,
                "materialization_run_ref",
                "RUN",
            ),
        )

        features: dict[str, MaterializedFeatureValue] = {}
        for key, item in self.features.items():
            feature_key = str(key).strip()
            if feature_key not in _FEATURE_KEYS:
                raise BusinessFeatureContractError("unknown business feature key")
            if not isinstance(item, MaterializedFeatureValue):
                raise BusinessFeatureContractError(
                    "features values must be MaterializedFeatureValue"
                )
            if item.feature_key != feature_key:
                raise BusinessFeatureContractError(
                    "feature mapping key must match feature_key"
                )
            features[feature_key] = item
        object.__setattr__(self, "features", MappingProxyType(features))

        coverage = self.coverage_summary
        if not isinstance(coverage, FeatureAvailability):
            raise BusinessFeatureContractError(
                "coverage_summary must be FeatureAvailability"
            )
        expected_statuses = {
            "reliable_features": "reliable",
            "approximate_features": "approximate",
            "missing_features": "missing",
            "policy_blocked_features": "policy_blocked",
        }
        for field_name, expected_status in expected_statuses.items():
            for feature_key in getattr(coverage, field_name):
                item = features.get(feature_key)
                if item is None:
                    if expected_status in {"reliable", "approximate"}:
                        raise BusinessFeatureContractError(
                            f"{expected_status} coverage requires a feature value"
                        )
                    continue
                if item.status != expected_status:
                    raise BusinessFeatureContractError(
                        "coverage_summary status does not match feature value"
                    )
        for feature_key, item in features.items():
            if feature_key not in getattr(coverage, f"{item.status}_features"):
                raise BusinessFeatureContractError(
                    "feature status is missing from coverage_summary"
                )

        source_manifest = tuple(self.source_manifest)
        if any(not isinstance(source, FeatureSource) for source in source_manifest):
            raise BusinessFeatureContractError(
                "source_manifest must contain FeatureSource objects"
            )
        manifest_identities = [
            (source.source_type, source.source_record_ref)
            for source in source_manifest
        ]
        if len(manifest_identities) != len(set(manifest_identities)):
            raise BusinessFeatureContractError(
                "source_manifest must not contain duplicate sources"
            )
        manifest_set = set(source_manifest)
        as_of_date = as_of.date()
        for source in source_manifest:
            if source.observed_at > as_of:
                raise BusinessFeatureContractError(
                    "source observed_at must not be later than snapshot as_of"
                )
            if source.period_end is not None and source.period_end > as_of_date:
                raise BusinessFeatureContractError(
                    "source period_end must not be later than snapshot as_of"
                )
        for item in features.values():
            for source in item.sources:
                if source not in manifest_set:
                    raise BusinessFeatureContractError(
                        "feature sources must be present in source_manifest"
                    )
            if item.period_end is not None and item.period_end > as_of_date:
                raise BusinessFeatureContractError(
                    "feature period_end must not be later than snapshot as_of"
                )
        object.__setattr__(self, "source_manifest", source_manifest)

        for cycle in coverage.statement_cycles:
            if cycle.period_end > as_of_date:
                raise BusinessFeatureContractError(
                    "statement cycles must not end after snapshot as_of"
                )

        cycles_by_id = {
            cycle.statement_cycle_id: cycle
            for cycle in coverage.statement_cycles
        }
        slot_cycles: dict[int, str] = {}
        cycle_slots: dict[str, int] = {}
        for feature_key, item in features.items():
            if feature_key not in _PERIOD_FEATURES:
                continue
            slot = _history_slot(feature_key)
            cycle = cycles_by_id.get(item.statement_cycle_id or "")
            if cycle is None:
                raise BusinessFeatureContractError(
                    "historical feature must reference a declared statement cycle"
                )
            if (
                item.period_start != cycle.period_start
                or item.period_end != cycle.period_end
            ):
                raise BusinessFeatureContractError(
                    "historical feature period must match its statement cycle"
                )
            previous_cycle = slot_cycles.setdefault(slot, cycle.statement_cycle_id)
            if previous_cycle != cycle.statement_cycle_id:
                raise BusinessFeatureContractError(
                    "history feature groups must share one statement cycle"
                )
            previous_slot = cycle_slots.setdefault(cycle.statement_cycle_id, slot)
            if previous_slot != slot:
                raise BusinessFeatureContractError(
                    "one statement cycle cannot serve multiple history slots"
                )

        if coverage.six_month_coverage_complete:
            if len(coverage.statement_cycles) != 6:
                raise BusinessFeatureContractError(
                    "coverage completion requires six statement cycles"
                )
            latest_end = max(cycle.period_end for cycle in coverage.statement_cycles)
            if (as_of_date - latest_end).days > _MAX_LATEST_CYCLE_AGE_DAYS:
                raise BusinessFeatureContractError(
                    "latest statement cycle is not aligned with snapshot as_of"
                )
            for index, (repayment_key, statement_key, payment_key) in (
                _HISTORY_FEATURE_GROUPS.items()
            ):
                cycle = coverage.statement_cycles[index - 1]
                group_completeness = []
                for feature_key in (repayment_key, statement_key, payment_key):
                    item = features.get(feature_key)
                    allowed_statuses = (
                        {"reliable"}
                        if coverage.six_month_reliable_complete
                        else {"reliable", "approximate"}
                    )
                    if item is None or item.status not in allowed_statuses:
                        raise BusinessFeatureContractError(
                            "history completion requires every history feature"
                        )
                    if (
                        item.statement_cycle_id != cycle.statement_cycle_id
                        or item.period_start != cycle.period_start
                        or item.period_end != cycle.period_end
                    ):
                        raise BusinessFeatureContractError(
                            "history features must use their assigned statement cycle"
                        )
                    if item.completeness is None:
                        raise BusinessFeatureContractError(
                            "history features require completeness"
                        )
                    group_completeness.append(item.completeness)
                period_completeness = coverage.completeness_by_period.get(
                    cycle.statement_cycle_id
                )
                expected_completeness = min(group_completeness)
                if period_completeness is None or not math.isclose(
                    period_completeness,
                    expected_completeness,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                ):
                    raise BusinessFeatureContractError(
                        "period completeness must match its history features"
                    )
                if coverage.six_month_reliable_complete and any(
                    value != 1.0 for value in group_completeness
                ):
                    raise BusinessFeatureContractError(
                        "reliable completion requires history completeness 1.0"
                    )

        diagnostics = tuple(self.diagnostics)
        if any(
            not isinstance(diagnostic, MaterializationDiagnostic)
            for diagnostic in diagnostics
        ):
            raise BusinessFeatureContractError(
                "diagnostics must contain MaterializationDiagnostic objects"
            )
        manifest_source_types = {source.source_type for source in source_manifest}
        manifest_source_refs = {
            source.source_record_ref for source in source_manifest
        }
        declared_cycle_ids = {
            cycle.statement_cycle_id for cycle in coverage.statement_cycles
        }
        cycle_months = {
            cycle.period_end.strftime("%Y-%m")
            for cycle in coverage.statement_cycles
        }
        for diagnostic in diagnostics:
            if diagnostic.feature_key and diagnostic.feature_key not in _FEATURE_KEYS:
                raise BusinessFeatureContractError(
                    "diagnostic feature_key must be a known business feature"
                )
            if (
                diagnostic.source_type is not None
                and diagnostic.source_type not in manifest_source_types
            ):
                raise BusinessFeatureContractError(
                    "diagnostic source_type must exist in source_manifest"
                )
            if (
                diagnostic.source_ref is not None
                and diagnostic.source_ref not in manifest_source_refs
            ):
                raise BusinessFeatureContractError(
                    "diagnostic source_ref must exist in source_manifest"
                )
            if (
                diagnostic.statement_cycle_id is not None
                and diagnostic.statement_cycle_id not in declared_cycle_ids
            ):
                raise BusinessFeatureContractError(
                    "diagnostic statement_cycle_id must be declared"
                )
            if diagnostic.period is not None and diagnostic.period not in cycle_months:
                raise BusinessFeatureContractError(
                    "diagnostic period must match a declared statement cycle"
                )
        object.__setattr__(self, "diagnostics", diagnostics)
