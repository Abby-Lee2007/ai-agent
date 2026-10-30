"""Tests for the business credit feature contract v0.3."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, datetime, timezone
from types import MappingProxyType

import pytest

from finagent.risk.credit_risk.business_contract import (
    BUSINESS_FEATURE_KEYS,
    BusinessCreditFeatureSnapshot,
    BusinessFeatureContractError,
    FeatureAvailability,
    FeatureSource,
    MaterializationDiagnostic,
    MaterializedFeatureValue,
    StatementCycle,
)


OBSERVED_AT = datetime(2026, 10, 30, 8, 0, tzinfo=timezone.utc)
AS_OF = datetime(2026, 10, 30, 8, 30, tzinfo=timezone.utc)
CUSTOMER_REF = "CUST_00000000-0000-0000-0000-000000000001"
RUN_REF = "RUN_00000000-0000-0000-0000-000000000002"
BATCH_REF = "UPL_00000000-0000-0000-0000-000000000003"
SOURCE_REF = "SRC_00000000-0000-0000-0000-000000000010"
CARD_REF = "CARD_00000000-0000-0000-0000-000000000020"
ACCOUNT_REF = "ACCT_00000000-0000-0000-0000-000000000030"

CYCLE_DATES = (
    (date(2026, 5, 1), date(2026, 5, 31)),
    (date(2026, 6, 1), date(2026, 6, 30)),
    (date(2026, 7, 1), date(2026, 7, 31)),
    (date(2026, 8, 1), date(2026, 8, 31)),
    (date(2026, 9, 1), date(2026, 9, 30)),
    (date(2026, 10, 1), date(2026, 10, 28)),
)


def ref(prefix: str, number: int) -> str:
    return f"{prefix}_00000000-0000-0000-0000-{number:012d}"


def cycle_id(index: int) -> str:
    return ref("CYCLE", 100 + index)


def make_cycles(
    indexes: tuple[int, ...] = (1, 2, 3, 4, 5, 6),
) -> tuple[StatementCycle, ...]:
    return tuple(
        StatementCycle(
            statement_cycle_id=cycle_id(index),
            period_start=CYCLE_DATES[index - 1][0],
            period_end=CYCLE_DATES[index - 1][1],
        )
        for index in indexes
    )


def make_source(
    *,
    source_type: str = "user_profile",
    source_record_ref: str = SOURCE_REF,
    customer_id_matches: bool = True,
    account_id_matches: bool = False,
    card_id_matches: bool = False,
    currency_code: str | None = None,
    data_complete: bool = True,
    observed_at: datetime = OBSERVED_AT,
    period_start: date | None = None,
    period_end: date | None = None,
    statement_cycle_id: str | None = None,
) -> FeatureSource:
    return FeatureSource(
        source_type=source_type,
        source_schema_version="schema_v3",
        observed_at=observed_at,
        source_record_ref=source_record_ref,
        period_start=period_start,
        period_end=period_end,
        customer_id_matches=customer_id_matches,
        account_id_matches=account_id_matches,
        card_id_matches=card_id_matches,
        currency_verified=currency_code is not None,
        currency_code=currency_code,
        data_complete=data_complete,
        source_batch_ref=BATCH_REF,
        statement_cycle_id=statement_cycle_id,
    )


def make_feature(
    feature_key: str,
    *,
    status: str = "missing",
    value: object = None,
    sources: tuple[FeatureSource, ...] = (),
    completeness: float | None = None,
    approximation_method: str | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
    statement_cycle_id: str | None = None,
    unit: str | None = None,
    aggregation_scope: str = "customer",
    card_ref: str | None = None,
    account_ref: str | None = None,
    aggregation_strategy: str | None = None,
) -> MaterializedFeatureValue:
    if feature_key.startswith(
        ("repayment_status_", "statement_amount_", "payment_amount_")
    ):
        period_start = period_start or CYCLE_DATES[-1][0]
        period_end = period_end or CYCLE_DATES[-1][1]
        statement_cycle_id = statement_cycle_id or cycle_id(6)
    if unit is None:
        unit = (
            "CNY"
            if feature_key == "current_credit_limit"
            or feature_key.startswith(("statement_amount_", "payment_amount_"))
            else "category"
        )
    return MaterializedFeatureValue(
        feature_key=feature_key,
        value=value,
        unit=unit,
        status=status,
        availability_reason="contract_test",
        sources=sources,
        aggregation_scope=aggregation_scope,
        card_ref=card_ref,
        account_ref=account_ref,
        aggregation_strategy=aggregation_strategy,
        period_start=period_start,
        period_end=period_end,
        completeness=completeness,
        approximation_method=approximation_method,
        statement_cycle_id=statement_cycle_id,
    )


def make_availability(
    status_by_feature: dict[str, str] | None = None,
    *,
    calculate_coverage: bool = False,
    statement_cycles: tuple[StatementCycle, ...] = (),
    completeness_by_period: dict[str, float | None] | None = None,
    coverage_complete: bool = False,
    reliable_complete: bool = False,
) -> FeatureAvailability:
    status_by_feature = status_by_feature or {}
    categories = {
        "reliable": [],
        "approximate": [],
        "missing": [],
        "policy_blocked": [],
    }
    for feature_key in BUSINESS_FEATURE_KEYS:
        categories[status_by_feature.get(feature_key, "missing")].append(feature_key)
    total = len(BUSINESS_FEATURE_KEYS)
    ratios = (None, None, None)
    if calculate_coverage:
        reliable = len(categories["reliable"]) / total
        approximate = len(categories["approximate"]) / total
        ratios = (reliable, approximate, reliable + approximate)
    return FeatureAvailability(
        reliable_features=tuple(categories["reliable"]),
        approximate_features=tuple(categories["approximate"]),
        missing_features=tuple(categories["missing"]),
        policy_blocked_features=tuple(categories["policy_blocked"]),
        reliable_coverage_ratio=ratios[0],
        approximate_coverage_ratio=ratios[1],
        potential_coverage_ratio=ratios[2],
        six_month_coverage_complete=coverage_complete,
        six_month_reliable_complete=reliable_complete,
        statement_cycles=statement_cycles,
        completeness_by_period=completeness_by_period or {},
    )


def make_snapshot(
    features: dict[str, MaterializedFeatureValue] | None = None,
    *,
    availability: FeatureAvailability | None = None,
    diagnostics: tuple[MaterializationDiagnostic, ...] = (),
    source_manifest: tuple[FeatureSource, ...] | None = None,
    as_of: datetime = AS_OF,
) -> BusinessCreditFeatureSnapshot:
    features = features or {}
    if availability is None:
        availability = make_availability(
            {key: value.status for key, value in features.items()}
        )
    if source_manifest is None:
        manifest: dict[tuple[str, str], FeatureSource] = {}
        for item in features.values():
            for source in item.sources:
                manifest[(source.source_type, source.source_record_ref)] = source
        source_manifest = tuple(manifest.values())
    return BusinessCreditFeatureSnapshot(
        customer_ref=CUSTOMER_REF,
        customer_type="existing",
        as_of=as_of,
        feature_schema_version="business_features_v3",
        features=features,
        coverage_summary=availability,
        source_manifest=source_manifest,
        diagnostics=diagnostics,
        materialization_run_ref=RUN_REF,
    )


def history_source(
    feature_key: str,
    cycle: StatementCycle,
    index: int,
    *,
    approximate: bool = False,
) -> FeatureSource:
    if feature_key.startswith("payment_amount_"):
        return make_source(
            source_type="repayment_record",
            source_record_ref=ref("SRC", 200 + index),
            account_id_matches=True,
            card_id_matches=True,
            currency_code="CNY",
            data_complete=not approximate,
            period_start=cycle.period_start,
            period_end=cycle.period_end,
            statement_cycle_id=cycle.statement_cycle_id,
        )
    return make_source(
        source_type="formal_statement",
        source_record_ref=ref("SRC", 100 + index),
        card_id_matches=True,
        currency_code=(
            "CNY" if feature_key.startswith("statement_amount_") else None
        ),
        data_complete=not approximate,
        period_start=cycle.period_start,
        period_end=cycle.period_end,
        statement_cycle_id=cycle.statement_cycle_id,
    )


def make_complete_history(
    *,
    status: str = "reliable",
    cycles: tuple[StatementCycle, ...] | None = None,
    completeness: float | None = None,
    coverage_complete: bool = True,
    reliable_complete: bool | None = None,
) -> tuple[dict[str, MaterializedFeatureValue], FeatureAvailability]:
    cycles = tuple(cycles or make_cycles())
    ordered_cycles = tuple(sorted(cycles, key=lambda cycle: cycle.period_start))
    if completeness is None:
        completeness = 1.0 if status == "reliable" else 0.8
    features: dict[str, MaterializedFeatureValue] = {}
    statuses: dict[str, str] = {}
    for index in range(1, 7):
        cycle = ordered_cycles[index - 1]
        repayment_key = f"repayment_status_{index}"
        statement_key = f"statement_amount_{index}"
        payment_key = f"payment_amount_{index}"
        formal_source = history_source(
            statement_key,
            cycle,
            index,
            approximate=status == "approximate",
        )
        feature_sources = {
            repayment_key: formal_source,
            statement_key: formal_source,
            payment_key: history_source(
                payment_key,
                cycle,
                index,
                approximate=status == "approximate",
            ),
        }
        for feature_key, source in feature_sources.items():
            features[feature_key] = make_feature(
                feature_key,
                status=status,
                value="current"
                if feature_key.startswith("repayment_status_")
                else 100.0,
                sources=(source,),
                completeness=completeness,
                approximation_method=(
                    "statement_cycle_proxy" if status == "approximate" else None
                ),
                period_start=cycle.period_start,
                period_end=cycle.period_end,
                statement_cycle_id=cycle.statement_cycle_id,
                aggregation_scope="card",
                card_ref=CARD_REF,
            )
            statuses[feature_key] = status
    if reliable_complete is None:
        reliable_complete = status == "reliable" and coverage_complete
    availability = make_availability(
        statuses,
        statement_cycles=cycles,
        completeness_by_period={
            cycle.statement_cycle_id: completeness for cycle in ordered_cycles
        },
        coverage_complete=coverage_complete,
        reliable_complete=reliable_complete,
    )
    return features, availability


def test_transaction_ledger_requires_account_ownership() -> None:
    cycle = make_cycles()[0]
    source = make_source(
        source_type="transaction_ledger",
        account_id_matches=False,
        card_id_matches=True,
        currency_code="CNY",
        period_start=cycle.period_start,
        period_end=cycle.period_end,
        statement_cycle_id=cycle.statement_cycle_id,
    )
    with pytest.raises(BusinessFeatureContractError, match="account-bound"):
        make_feature(
            "payment_amount_1",
            status="reliable",
            value=100.0,
            sources=(source,),
            completeness=1.0,
            period_start=cycle.period_start,
            period_end=cycle.period_end,
            statement_cycle_id=cycle.statement_cycle_id,
            aggregation_scope="card",
            card_ref=CARD_REF,
        )


def test_repayment_record_requires_account_ownership() -> None:
    cycle = make_cycles()[0]
    source = make_source(
        source_type="repayment_record",
        account_id_matches=False,
        card_id_matches=True,
        currency_code="CNY",
        period_start=cycle.period_start,
        period_end=cycle.period_end,
        statement_cycle_id=cycle.statement_cycle_id,
    )
    with pytest.raises(BusinessFeatureContractError, match="account-bound"):
        make_feature(
            "payment_amount_1",
            status="reliable",
            value=100.0,
            sources=(source,),
            completeness=1.0,
            period_start=cycle.period_start,
            period_end=cycle.period_end,
            statement_cycle_id=cycle.statement_cycle_id,
            aggregation_scope="card",
            card_ref=CARD_REF,
        )


def test_account_snapshot_requires_account_ownership() -> None:
    source = make_source(
        source_type="account_snapshot",
        account_id_matches=False,
        card_id_matches=True,
        currency_code="CNY",
    )
    with pytest.raises(BusinessFeatureContractError, match="account-bound"):
        make_feature(
            "current_credit_limit",
            status="reliable",
            value=50_000.0,
            sources=(source,),
            completeness=1.0,
            aggregation_scope="card",
            card_ref=CARD_REF,
        )


def test_all_approximate_history_is_coverage_complete_not_reliable_complete() -> None:
    features, availability = make_complete_history(status="approximate")
    snapshot = make_snapshot(features, availability=availability)

    assert snapshot.coverage_summary.six_month_coverage_complete is True
    assert snapshot.coverage_summary.six_month_reliable_complete is False


def test_all_reliable_history_is_reliable_complete() -> None:
    features, availability = make_complete_history(status="reliable")
    snapshot = make_snapshot(features, availability=availability)

    assert snapshot.coverage_summary.six_month_coverage_complete is True
    assert snapshot.coverage_summary.six_month_reliable_complete is True


def test_reliable_complete_rejects_approximate_feature() -> None:
    features, availability = make_complete_history(
        status="approximate",
        completeness=1.0,
        reliable_complete=True,
    )
    with pytest.raises(BusinessFeatureContractError, match="history feature"):
        make_snapshot(features, availability=availability)


def test_feature_completeness_below_one_conflicts_with_period_completeness() -> None:
    features, availability = make_complete_history(status="approximate")
    feature = features["payment_amount_1"]
    features["payment_amount_1"] = replace(feature, completeness=0.5)
    with pytest.raises(BusinessFeatureContractError, match="period completeness"):
        make_snapshot(features, availability=availability)


def test_one_day_statement_cycle_is_rejected() -> None:
    with pytest.raises(BusinessFeatureContractError, match="duration"):
        StatementCycle(
            statement_cycle_id=cycle_id(1),
            period_start=date(2026, 10, 1),
            period_end=date(2026, 10, 1),
        )


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2026, 1, 1), date(2026, 1, 25)),
        (date(2026, 1, 1), date(2026, 2, 4)),
    ],
)
def test_statement_cycle_duration_boundaries_pass(start: date, end: date) -> None:
    cycle = StatementCycle(
        statement_cycle_id=cycle_id(1),
        period_start=start,
        period_end=end,
    )
    assert (cycle.period_end - cycle.period_start).days + 1 in {25, 35}


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2026, 1, 1), date(2026, 1, 24)),
        (date(2026, 1, 1), date(2026, 2, 5)),
    ],
)
def test_statement_cycle_duration_outside_policy_is_rejected(
    start: date,
    end: date,
) -> None:
    with pytest.raises(BusinessFeatureContractError, match="duration"):
        StatementCycle(
            statement_cycle_id=cycle_id(1),
            period_start=start,
            period_end=end,
        )


def test_same_source_identity_is_rejected_even_if_metadata_differs() -> None:
    first = make_source()
    second = replace(
        first,
        source_schema_version="schema_v4",
        observed_at=datetime(2026, 10, 30, 7, 0, tzinfo=timezone.utc),
    )
    with pytest.raises(BusinessFeatureContractError, match="duplicate feature sources"):
        make_feature(
            "gender",
            status="reliable",
            value="female",
            sources=(first, second),
            completeness=1.0,
        )


def test_card_aggregation_scope_requires_card_ref() -> None:
    with pytest.raises(BusinessFeatureContractError, match="card scope"):
        make_feature(
            "gender",
            status="reliable",
            value="female",
            sources=(make_source(),),
            completeness=1.0,
            aggregation_scope="card",
        )


def test_customer_multi_source_aggregation_requires_strategy() -> None:
    first = make_source(
        source_type="account_snapshot",
        source_record_ref=ref("SRC", 301),
        account_id_matches=True,
        card_id_matches=True,
        currency_code="CNY",
    )
    second = make_source(
        source_type="account_snapshot",
        source_record_ref=ref("SRC", 302),
        account_id_matches=True,
        card_id_matches=True,
        currency_code="CNY",
    )
    with pytest.raises(BusinessFeatureContractError, match="requires a strategy"):
        make_feature(
            "current_credit_limit",
            status="reliable",
            value=100_000.0,
            sources=(first, second),
            completeness=1.0,
        )


def test_customer_multi_source_aggregation_with_strategy_passes() -> None:
    first = make_source(
        source_type="account_snapshot",
        source_record_ref=ref("SRC", 301),
        account_id_matches=True,
        card_id_matches=True,
        currency_code="CNY",
    )
    second = make_source(
        source_type="account_snapshot",
        source_record_ref=ref("SRC", 302),
        account_id_matches=True,
        card_id_matches=True,
        currency_code="CNY",
    )
    feature = make_feature(
        "current_credit_limit",
        status="reliable",
        value=100_000.0,
        sources=(first, second),
        completeness=1.0,
        aggregation_strategy="sum_card_limits",
    )
    assert feature.aggregation_strategy == "sum_card_limits"


def test_mixed_currency_sources_are_rejected() -> None:
    cycle = make_cycles()[0]
    cny = make_source(
        source_type="repayment_record",
        source_record_ref=ref("SRC", 401),
        account_id_matches=True,
        card_id_matches=True,
        currency_code="CNY",
        period_start=cycle.period_start,
        period_end=cycle.period_end,
        statement_cycle_id=cycle.statement_cycle_id,
    )
    usd = make_source(
        source_type="repayment_record",
        source_record_ref=ref("SRC", 402),
        account_id_matches=True,
        card_id_matches=True,
        currency_code="USD",
        period_start=cycle.period_start,
        period_end=cycle.period_end,
        statement_cycle_id=cycle.statement_cycle_id,
    )
    with pytest.raises(BusinessFeatureContractError, match="one currency"):
        make_feature(
            "payment_amount_1",
            status="approximate",
            value=100.0,
            sources=(cny, usd),
            completeness=0.8,
            approximation_method="monthly_proxy",
            period_start=cycle.period_start,
            period_end=cycle.period_end,
            statement_cycle_id=cycle.statement_cycle_id,
            aggregation_scope="card",
            card_ref=CARD_REF,
            aggregation_strategy="sum_payments",
        )


def test_invalid_iso_currency_code_is_rejected() -> None:
    with pytest.raises(BusinessFeatureContractError, match="ISO 4217"):
        make_source(currency_code="ABC")


def test_diagnostic_source_ref_and_cycle_must_exist() -> None:
    _features, availability = make_complete_history()
    source_diag = MaterializationDiagnostic(
        code="source_issue",
        feature_key="statement_amount_1",
        source_type="formal_statement",
        source_ref=ref("SRC", 999),
    )
    cycle_diag = MaterializationDiagnostic(
        code="cycle_issue",
        feature_key="statement_amount_1",
        statement_cycle_id=ref("CYCLE", 999),
    )
    with pytest.raises(BusinessFeatureContractError, match="source_ref"):
        make_snapshot((make_complete_history()[0]), availability=availability, diagnostics=(source_diag,))
    with pytest.raises(BusinessFeatureContractError, match="statement_cycle_id"):
        make_snapshot((make_complete_history()[0]), availability=availability, diagnostics=(cycle_diag,))


def test_global_diagnostic_is_strictly_empty() -> None:
    with pytest.raises(BusinessFeatureContractError, match="global diagnostics"):
        MaterializationDiagnostic(
            code="global_error",
            source_ref=SOURCE_REF,
        )


def test_future_observed_at_is_rejected() -> None:
    source = make_source(
        observed_at=datetime(2026, 10, 31, 8, 0, tzinfo=timezone.utc)
    )
    feature = make_feature(
        "gender",
        status="reliable",
        value="female",
        sources=(source,),
        completeness=1.0,
    )
    with pytest.raises(BusinessFeatureContractError, match="observed_at"):
        make_snapshot({"gender": feature})


def test_future_feature_period_is_rejected() -> None:
    cycle = StatementCycle(
        statement_cycle_id=cycle_id(6),
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
    )
    source = history_source("statement_amount_1", cycle, 1)
    feature = make_feature(
        "statement_amount_1",
        status="reliable",
        value=100.0,
        sources=(source,),
        completeness=1.0,
        period_start=cycle.period_start,
        period_end=cycle.period_end,
        statement_cycle_id=cycle.statement_cycle_id,
        aggregation_scope="card",
        card_ref=CARD_REF,
    )
    with pytest.raises(BusinessFeatureContractError, match="later than snapshot as_of"):
        make_snapshot({"statement_amount_1": feature})


def test_future_cycle_is_rejected_even_without_completion() -> None:
    cycle = StatementCycle(
        statement_cycle_id=cycle_id(6),
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
    )
    availability = make_availability(statement_cycles=(cycle,))
    with pytest.raises(BusinessFeatureContractError, match="must not end after"):
        make_snapshot({}, availability=availability)


def test_coverage_and_reliable_completion_flags_are_ordered() -> None:
    with pytest.raises(BusinessFeatureContractError, match="requires coverage"):
        make_availability(reliable_complete=True)


def test_reliable_completion_rejects_low_period_completeness() -> None:
    cycles = make_cycles()
    with pytest.raises(BusinessFeatureContractError, match="complete periods"):
        make_availability(
            statement_cycles=cycles,
            completeness_by_period={
                cycle.statement_cycle_id: 0.9 for cycle in cycles
            },
            coverage_complete=True,
            reliable_complete=True,
        )


def test_reliable_and_approximate_coverage_require_values() -> None:
    with pytest.raises(BusinessFeatureContractError, match="reliable coverage"):
        make_snapshot(
            {},
            availability=make_availability({"gender": "reliable"}),
        )
    with pytest.raises(BusinessFeatureContractError, match="approximate coverage"):
        make_snapshot(
            {},
            availability=make_availability({"gender": "approximate"}),
        )


def test_missing_and_policy_blocked_values_may_be_absent() -> None:
    snapshot = make_snapshot(
        {},
        availability=make_availability(
            {
                "education_level": "missing",
                "gender": "policy_blocked",
            }
        ),
    )
    assert dict(snapshot.features) == {}


def test_complete_coverage_requires_all_three_history_groups() -> None:
    features, _availability = make_complete_history()
    for index in range(1, 7):
        features.pop(f"payment_amount_{index}")
    statuses = {key: "reliable" for key in features}
    availability = make_availability(
        statuses,
        statement_cycles=make_cycles(),
        completeness_by_period={
            cycle.statement_cycle_id: 1.0 for cycle in make_cycles()
        },
        coverage_complete=True,
    )

    with pytest.raises(BusinessFeatureContractError, match="every history feature"):
        make_snapshot(features, availability=availability)


def test_missing_statement_cycle_is_rejected() -> None:
    cycles = make_cycles()[:5]
    with pytest.raises(BusinessFeatureContractError, match="exactly six"):
        make_availability(
            statement_cycles=cycles,
            completeness_by_period={
                cycle.statement_cycle_id: 1.0 for cycle in cycles
            },
            coverage_complete=True,
        )


def test_duplicate_statement_cycle_identity_is_rejected() -> None:
    cycles = make_cycles()
    with pytest.raises(BusinessFeatureContractError, match="must be unique"):
        make_availability(statement_cycles=cycles + (cycles[0],))


def test_overlapping_statement_cycles_are_rejected() -> None:
    cycles = list(make_cycles())
    cycles[1] = StatementCycle(
        statement_cycle_id=cycles[1].statement_cycle_id,
        period_start=cycles[0].period_end,
        period_end=cycles[1].period_end,
    )
    with pytest.raises(BusinessFeatureContractError, match="must not overlap"):
        make_availability(statement_cycles=tuple(cycles))


def test_card_and_account_aggregation_scopes_pass_with_refs() -> None:
    card_source = make_source(
        source_type="card_snapshot",
        source_record_ref=ref("SRC", 501),
        card_id_matches=True,
        currency_code="CNY",
    )
    card_feature = make_feature(
        "current_credit_limit",
        status="reliable",
        value=50_000.0,
        sources=(card_source,),
        completeness=1.0,
        aggregation_scope="card",
        card_ref=CARD_REF,
    )
    account_source = make_source(
        source_type="account_snapshot",
        source_record_ref=ref("SRC", 502),
        account_id_matches=True,
        card_id_matches=True,
        currency_code="CNY",
    )
    account_feature = make_feature(
        "current_credit_limit",
        status="reliable",
        value=50_000.0,
        sources=(account_source,),
        completeness=1.0,
        aggregation_scope="account",
        account_ref=ACCOUNT_REF,
    )

    assert card_feature.card_ref == CARD_REF
    assert account_feature.account_ref == ACCOUNT_REF


def test_account_scope_requires_account_ref() -> None:
    source = make_source(
        source_type="account_snapshot",
        account_id_matches=True,
        card_id_matches=True,
        currency_code="CNY",
    )
    with pytest.raises(BusinessFeatureContractError, match="account scope"):
        make_feature(
            "current_credit_limit",
            status="reliable",
            value=50_000.0,
            sources=(source,),
            completeness=1.0,
            aggregation_scope="account",
        )


def test_valid_diagnostic_source_and_cycle_references_pass() -> None:
    features, availability = make_complete_history()
    source = features["statement_amount_1"].sources[0]
    diagnostic = MaterializationDiagnostic(
        code="manual_review",
        feature_key="statement_amount_1",
        source_type="formal_statement",
        source_ref=source.source_record_ref,
        statement_cycle_id=source.statement_cycle_id,
    )
    snapshot = make_snapshot(
        features,
        availability=availability,
        diagnostics=(diagnostic,),
    )
    assert snapshot.diagnostics == (diagnostic,)


def test_future_source_period_is_rejected() -> None:
    source = make_source(
        period_start=date(2026, 10, 29),
        period_end=date(2026, 10, 31),
    )
    feature = make_feature(
        "gender",
        status="reliable",
        value="female",
        sources=(source,),
        completeness=1.0,
    )
    with pytest.raises(BusinessFeatureContractError, match="source period_end"):
        make_snapshot({"gender": feature})


def test_customer_match_defaults_to_false() -> None:
    source = FeatureSource(
        source_type="user_profile",
        source_schema_version="schema_v3",
        observed_at=OBSERVED_AT,
        source_record_ref=SOURCE_REF,
    )
    assert source.customer_id_matches is False
    with pytest.raises(BusinessFeatureContractError, match="explicitly match"):
        make_feature(
            "gender",
            status="reliable",
            value="female",
            sources=(source,),
            completeness=1.0,
        )


def test_phone_or_filename_cannot_be_used_as_internal_ref() -> None:
    with pytest.raises(BusinessFeatureContractError, match="source_record_ref"):
        FeatureSource(
            source_type="user_profile",
            source_schema_version="schema_v3",
            observed_at=OBSERVED_AT,
            source_record_ref="SRC_13800138000",
        )
    with pytest.raises(BusinessFeatureContractError, match="source_batch_ref"):
        FeatureSource(
            source_type="user_profile",
            source_schema_version="schema_v3",
            observed_at=OBSERVED_AT,
            source_record_ref=SOURCE_REF,
            source_batch_ref="UPL_bank_statement_2026.pdf",
        )


def test_input_mappings_do_not_leak_mutable_state() -> None:
    feature = make_feature("education_level", status="missing")
    input_features = {"education_level": feature}
    cycles = make_cycles()
    input_completeness = {
        cycle.statement_cycle_id: 0.5 for cycle in cycles
    }
    availability = make_availability(
        statement_cycles=cycles,
        completeness_by_period=input_completeness,
    )
    snapshot = make_snapshot(input_features, availability=availability)

    input_features.clear()
    input_completeness.clear()
    assert "education_level" in snapshot.features
    assert len(availability.completeness_by_period) == 6


def test_diagnostic_has_no_free_text_message() -> None:
    diagnostic = MaterializationDiagnostic(
        code="missing_statement",
        feature_key="statement_amount_1",
    )
    assert not hasattr(diagnostic, "message")
    with pytest.raises(TypeError):
        MaterializationDiagnostic(
            code="missing_statement",
            message="raw text",  # type: ignore[call-arg]
        )


def test_uncomputed_and_zero_coverage_are_distinct() -> None:
    uncomputed = make_availability(calculate_coverage=False)
    zero = make_availability(calculate_coverage=True)
    assert uncomputed.reliable_coverage_ratio is None
    assert zero.reliable_coverage_ratio == 0.0


def test_snapshot_features_mapping_is_immutable() -> None:
    feature = make_feature("education_level", status="missing")
    snapshot = make_snapshot({"education_level": feature})
    assert isinstance(snapshot.features, MappingProxyType)
    with pytest.raises(TypeError):
        snapshot.features["gender"] = feature


def test_duplicate_source_identity_in_manifest_is_rejected() -> None:
    source = make_source()
    second = replace(source, source_schema_version="schema_v4")
    feature = make_feature(
        "gender",
        status="reliable",
        value="female",
        sources=(source,),
        completeness=1.0,
    )
    with pytest.raises(BusinessFeatureContractError, match="duplicate sources"):
        make_snapshot(
            {"gender": feature},
            source_manifest=(source, second),
        )


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_feature_values_are_rejected(value: float) -> None:
    with pytest.raises(BusinessFeatureContractError, match="finite"):
        make_feature(
            "verified_age",
            status="reliable",
            value=value,
            sources=(make_source(),),
            completeness=1.0,
        )


def test_invalid_status_and_source_type_are_rejected() -> None:
    with pytest.raises(BusinessFeatureContractError, match="invalid feature status"):
        make_feature(
            "gender",
            status="trusted",
            value="female",
            sources=(make_source(),),
            completeness=1.0,
        )
    with pytest.raises(BusinessFeatureContractError, match="invalid source_type"):
        make_source(source_type="raw_file")


def test_business_feature_names_are_complete_and_unique() -> None:
    assert len(BUSINESS_FEATURE_KEYS) == 23
    assert len(set(BUSINESS_FEATURE_KEYS)) == 23
    for index in range(1, 7):
        assert f"repayment_status_{index}" in BUSINESS_FEATURE_KEYS
        assert f"statement_amount_{index}" in BUSINESS_FEATURE_KEYS
        assert f"payment_amount_{index}" in BUSINESS_FEATURE_KEYS
    assert not any(feature.startswith("X") for feature in BUSINESS_FEATURE_KEYS)
