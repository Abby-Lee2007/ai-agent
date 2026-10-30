"""Tests for the v0.1 credit-risk shadow service."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict
from datetime import date, datetime, timezone
from unittest.mock import Mock

import pytest

import ml.credit_risk.predict as predict_module
from finagent.data.models import Account, Card, Transaction, User
from finagent.risk.credit_risk.audit import (
    CreditRiskAuditDiagnostic,
    CreditRiskAuditRecord,
)
from finagent.risk.credit_risk.contract import (
    FEATURE_VERSION,
    UCI_FEATURE_ORDER,
    CreditFeatureAvailability,
    CreditFeatureSnapshot,
    FeatureDiagnostic,
)
from finagent.risk.credit_risk.service import CreditRiskService


NOW = datetime(2026, 10, 30, 8, 0, tzinfo=timezone.utc)
AS_OF = date(2026, 10, 30)


def make_user() -> User:
    return User(
        user_id="u_test",
        name="Test User",
        phone="13800138000",
        gender="male",
        age=35,
        verified=True,
    )


def make_card() -> Card:
    return Card(
        card_id="CARD_SECRET_001",
        user_id="u_test",
        kind="credit",
        account_id="ACC_CREDIT",
        status="active",
        credit_limit=50_000.0,
        statement_amount=1_200.0,
        statement_date="2026-10-05",
    )


def make_accounts() -> list[Account]:
    return [
        Account(
            account_id="ACC_CREDIT",
            user_id="u_test",
            kind="credit",
            name="Credit account",
            tail="0001",
            balance=1_200.0,
            credit_limit=50_000.0,
            status="active",
        ),
        Account(
            account_id="ACC_CASH",
            user_id="u_test",
            kind="current",
            name="Payment account",
            tail="0002",
            balance=20_000.0,
            status="active",
        ),
    ]


def make_transaction() -> Transaction:
    return Transaction(
        txn_id="TXN_SECRET_001",
        user_id="u_test",
        account_id="ACC_CASH",
        timestamp="2026-10-20T12:00:00",
        amount=-500.0,
        merchant="Sensitive merchant name",
        category="信用卡还款",
        status="success",
        lifecycle_status="completed",
        tags=["credit_card_repayment"],
        metadata={"credit_card": "CARD_SECRET_001"},
    )


def make_snapshot(
    *,
    complete: bool = False,
) -> CreditFeatureSnapshot:
    if complete:
        available = UCI_FEATURE_ORDER
        approximate: tuple[str, ...] = ()
        missing: tuple[str, ...] = ()
    else:
        available = ("X1", "X2", "X5", "X12")
        approximate = ("X18",)
        missing = tuple(
            feature
            for feature in UCI_FEATURE_ORDER
            if feature not in available and feature not in approximate
        )
    return CreditFeatureSnapshot(
        customer_id="u_test",
        customer_type="existing",
        feature_version=FEATURE_VERSION,
        available_features=available,
        approximate_features=approximate,
        missing_features=missing,
        source_metadata={
            "phone": "13800138000",
            "card_id": "CARD_SECRET_001",
            "merchant": "Sensitive merchant name",
        },
        diagnostics=(
            FeatureDiagnostic(
                code="statement_date_expired",
                message="A statement date was stale.",
                feature="X12",
            ),
        ),
    )


def make_availability(
    snapshot: CreditFeatureSnapshot,
    *,
    complete: bool = False,
) -> CreditFeatureAvailability:
    total = len(UCI_FEATURE_ORDER)
    reliable = 1.0 if complete else len(snapshot.available_features) / total
    approximate = 0.0 if complete else len(snapshot.approximate_features) / total
    return CreditFeatureAvailability(
        status="complete" if complete else "partial",
        reliable_coverage_ratio=reliable,
        approximate_coverage_ratio=approximate,
        potential_coverage_ratio=reliable + approximate,
        available_features=snapshot.available_features,
        approximate_features=snapshot.approximate_features,
        missing_features=snapshot.missing_features,
        warnings=(
            "Approximate features are not reliable approval inputs.",
        ),
        diagnostics=snapshot.diagnostics,
    )


def make_service(
    *,
    mode: str = "shadow",
    snapshot: CreditFeatureSnapshot | None = None,
    availability: CreditFeatureAvailability | None = None,
) -> tuple[CreditRiskService, Mock, Mock]:
    snapshot = snapshot or make_snapshot()
    availability = availability or make_availability(snapshot)
    builder = Mock(return_value=snapshot)
    checker = Mock(return_value=availability)
    service = CreditRiskService(
        mode=mode,
        feature_builder=builder,
        availability_checker=checker,
        now_provider=lambda: NOW,
    )
    return service, builder, checker


def evaluate(
    service: CreditRiskService,
    *,
    user: User | None = None,
    cards: list[Card] | None = None,
    accounts: list[Account] | None = None,
    transactions: list[Transaction] | None = None,
):
    return service.evaluate_shadow(
        user=make_user() if user is None else user,
        cards=[make_card()] if cards is None else cards,
        accounts=make_accounts() if accounts is None else accounts,
        transactions=[make_transaction()] if transactions is None else transactions,
        customer_type="existing",
        as_of=AS_OF,
    )


def test_off_mode_does_not_call_feature_builder_or_checker() -> None:
    service, builder, checker = make_service(mode="off")

    result = service.evaluate_shadow(user=None)

    assert result.status == "off"
    assert result.audit.status == "off"
    assert result.audit.model_result_status == "not_called"
    assert result.audit.reliable_coverage_ratio is None
    assert result.audit.approximate_coverage_ratio is None
    assert result.audit.potential_coverage_ratio is None
    assert result.audit.model_version is None
    assert result.audit.artifact_sha256 is None
    assert result.rule_impact == "none"
    assert result.audit.rule_impact == "none"
    builder.assert_not_called()
    checker.assert_not_called()


def test_shadow_calls_feature_builder_and_availability_checker() -> None:
    snapshot = make_snapshot()
    availability = make_availability(snapshot)
    service, builder, checker = make_service(
        snapshot=snapshot,
        availability=availability,
    )

    result = evaluate(service)

    builder.assert_called_once()
    checker.assert_called_once_with(snapshot)
    assert result.status == "insufficient_data"
    assert result.snapshot is snapshot
    assert result.availability is availability


def test_shadow_does_not_load_or_call_model(monkeypatch) -> None:
    called = []

    def fail_if_called(*args, **kwargs):
        called.append((args, kwargs))
        raise AssertionError("model must not be called")

    monkeypatch.setattr(
        predict_module,
        "load_model_artifact",
        fail_if_called,
    )
    monkeypatch.setattr(
        predict_module,
        "predict_one",
        fail_if_called,
    )
    service, _, _ = make_service()

    result = evaluate(service)

    assert result.status == "insufficient_data"
    assert result.audit.model_result_status == "not_called"
    assert called == []


def test_incomplete_features_return_insufficient_data() -> None:
    service, _, _ = make_service()

    result = evaluate(service)

    assert result.status == "insufficient_data"
    assert result.audit.error_code == "feature_contract_incomplete"
    assert result.audit.model_result_status == "not_called"


def test_complete_features_still_do_not_call_model() -> None:
    snapshot = make_snapshot(complete=True)
    availability = make_availability(snapshot, complete=True)
    service, _, _ = make_service(
        snapshot=snapshot,
        availability=availability,
    )

    result = evaluate(service)

    assert result.status == "model_not_called"
    assert result.audit.error_code == "model_execution_disabled_v0_1"
    assert result.audit.model_result_status == "not_called"


def test_coverage_ratios_are_passed_to_audit() -> None:
    snapshot = make_snapshot()
    availability = make_availability(snapshot)
    service, _, _ = make_service(
        snapshot=snapshot,
        availability=availability,
    )

    result = evaluate(service)

    assert result.audit.reliable_coverage_ratio == (
        availability.reliable_coverage_ratio
    )
    assert result.audit.approximate_coverage_ratio == (
        availability.approximate_coverage_ratio
    )
    assert result.audit.potential_coverage_ratio == (
        availability.potential_coverage_ratio
    )


def test_diagnostics_and_missing_features_are_passed_to_audit() -> None:
    snapshot = make_snapshot()
    service, _, _ = make_service(snapshot=snapshot)

    result = evaluate(service)

    assert result.audit.missing_features == snapshot.missing_features
    assert result.audit.approximate_features == snapshot.approximate_features
    assert result.audit.diagnostics == (
        CreditRiskAuditDiagnostic(
            code="statement_date_expired",
            feature="X12",
            count=1,
        ),
    )
    assert not hasattr(result.audit.diagnostics[0], "message")


def test_model_result_status_is_always_not_called() -> None:
    for mode in ("off", "shadow"):
        service, _, _ = make_service(mode=mode)
        result = service.evaluate_shadow(user=None) if mode == "off" else evaluate(
            service
        )
        assert result.audit.model_result_status == "not_called"
        assert result.audit.model_version is None
        assert result.audit.artifact_sha256 is None


def test_rule_impact_is_fixed_to_none_and_audit_is_immutable() -> None:
    service, _, _ = make_service()
    result = evaluate(service)

    assert result.rule_impact == "none"
    assert result.audit.rule_impact == "none"
    with pytest.raises(FrozenInstanceError):
        result.audit.status = "completed"
    with pytest.raises(FrozenInstanceError):
        result.audit.rule_impact = "changed"


def test_audit_excludes_sensitive_source_values() -> None:
    service, _, _ = make_service()
    result = evaluate(service)
    serialized = repr(asdict(result.audit))

    for sensitive in (
        "13800138000",
        "CARD_SECRET_001",
        "TXN_SECRET_001",
        "Sensitive merchant name",
    ):
        assert sensitive not in serialized


def test_audit_whitelists_diagnostics_without_free_text() -> None:
    snapshot = make_snapshot()
    diagnostic = FeatureDiagnostic(
        code="safe_code",
        message=(
            "phone 13800138000 card CARD_SECRET_001 "
            "txn TXN_SECRET_001 merchant Sensitive merchant name"
        ),
        feature="X18-X23",
        count=2,
    )
    snapshot = CreditFeatureSnapshot(
        customer_id=snapshot.customer_id,
        customer_type=snapshot.customer_type,
        feature_version=snapshot.feature_version,
        available_features=snapshot.available_features,
        approximate_features=snapshot.approximate_features,
        missing_features=snapshot.missing_features,
        source_metadata=snapshot.source_metadata,
        diagnostics=(diagnostic,),
    )
    availability = make_availability(snapshot)
    service, _, _ = make_service(snapshot=snapshot, availability=availability)

    result = evaluate(service)
    serialized = repr(asdict(result.audit))

    assert result.audit.diagnostics == (
        CreditRiskAuditDiagnostic(
            code="safe_code",
            feature="X18-X23",
            count=2,
        ),
    )
    for sensitive in (
        "13800138000",
        "CARD_SECRET_001",
        "TXN_SECRET_001",
        "Sensitive merchant name",
    ):
        assert sensitive not in serialized


def test_feature_builder_value_error_returns_invalid_input() -> None:
    builder = Mock(side_effect=ValueError("invalid customer type"))
    checker = Mock()
    service = CreditRiskService(
        mode="shadow",
        feature_builder=builder,
        availability_checker=checker,
        now_provider=lambda: NOW,
    )

    result = evaluate(service)

    assert result.status == "invalid_input"
    assert result.audit.error_code == "feature_builder_invalid_input"
    assert result.audit.model_result_status == "not_called"
    assert result.audit.reliable_coverage_ratio is None
    assert result.audit.approximate_coverage_ratio is None
    assert result.audit.potential_coverage_ratio is None
    checker.assert_not_called()


def test_feature_builder_unexpected_error_returns_safe_failure() -> None:
    builder = Mock(side_effect=RuntimeError("unexpected"))
    checker = Mock()
    service = CreditRiskService(
        mode="shadow",
        feature_builder=builder,
        availability_checker=checker,
        now_provider=lambda: NOW,
    )

    result = evaluate(service)

    assert result.status == "shadow_error"
    assert result.audit.error_code == "feature_builder_exception"
    assert result.audit.model_result_status == "not_called"
    assert result.audit.reliable_coverage_ratio is None
    assert result.audit.approximate_coverage_ratio is None
    assert result.audit.potential_coverage_ratio is None
    assert result.audit.rule_impact == "none"
    checker.assert_not_called()


def test_availability_checker_error_returns_safe_failure() -> None:
    snapshot = make_snapshot()
    builder = Mock(return_value=snapshot)
    checker = Mock(side_effect=RuntimeError("checker failed"))
    service = CreditRiskService(
        mode="shadow",
        feature_builder=builder,
        availability_checker=checker,
        now_provider=lambda: NOW,
    )

    result = evaluate(service)

    assert result.status == "shadow_error"
    assert result.audit.error_code == "availability_checker_exception"
    assert result.snapshot is snapshot
    assert result.audit.model_result_status == "not_called"
    assert result.audit.reliable_coverage_ratio is None
    assert result.audit.approximate_coverage_ratio is None
    assert result.audit.potential_coverage_ratio is None


def test_zero_coverage_is_preserved_as_real_zero() -> None:
    snapshot = CreditFeatureSnapshot(
        customer_id="u_test",
        customer_type="unknown",
        feature_version=FEATURE_VERSION,
        available_features=(),
        approximate_features=(),
        missing_features=UCI_FEATURE_ORDER,
        source_metadata={},
        diagnostics=(),
    )
    availability = CreditFeatureAvailability(
        status="insufficient",
        reliable_coverage_ratio=0.0,
        approximate_coverage_ratio=0.0,
        potential_coverage_ratio=0.0,
        available_features=(),
        approximate_features=(),
        missing_features=UCI_FEATURE_ORDER,
        warnings=(),
        diagnostics=(),
    )
    service, _, _ = make_service(
        snapshot=snapshot,
        availability=availability,
    )

    result = evaluate(service)

    assert result.status == "insufficient_data"
    assert result.audit.reliable_coverage_ratio == 0.0
    assert result.audit.approximate_coverage_ratio == 0.0
    assert result.audit.potential_coverage_ratio == 0.0


@pytest.mark.parametrize("invalid_output", [None, object()])
def test_invalid_builder_output_returns_safe_failure(invalid_output) -> None:
    builder = Mock(return_value=invalid_output)
    checker = Mock()
    service = CreditRiskService(
        mode="shadow",
        feature_builder=builder,
        availability_checker=checker,
        now_provider=lambda: NOW,
    )

    result = evaluate(service)

    assert result.status == "shadow_error"
    assert result.audit.error_code == "feature_builder_invalid_output"
    assert result.audit.reliable_coverage_ratio is None
    assert result.audit.approximate_coverage_ratio is None
    assert result.audit.potential_coverage_ratio is None
    checker.assert_not_called()


@pytest.mark.parametrize("invalid_output", [None, object()])
def test_invalid_checker_output_returns_safe_failure(invalid_output) -> None:
    snapshot = make_snapshot()
    builder = Mock(return_value=snapshot)
    checker = Mock(return_value=invalid_output)
    service = CreditRiskService(
        mode="shadow",
        feature_builder=builder,
        availability_checker=checker,
        now_provider=lambda: NOW,
    )

    result = evaluate(service)

    assert result.status == "shadow_error"
    assert result.audit.error_code == "availability_checker_invalid_output"
    assert result.snapshot is snapshot
    assert result.audit.reliable_coverage_ratio is None
    assert result.audit.approximate_coverage_ratio is None
    assert result.audit.potential_coverage_ratio is None


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_base_exceptions_are_not_captured(exception_type) -> None:
    builder = Mock(side_effect=exception_type())
    service = CreditRiskService(
        mode="shadow",
        feature_builder=builder,
        availability_checker=Mock(),
        now_provider=lambda: NOW,
    )

    with pytest.raises(exception_type):
        evaluate(service)


def test_invalid_mode_is_rejected_explicitly() -> None:
    with pytest.raises(ValueError, match="mode must be off or shadow"):
        CreditRiskService(mode="enforce")


def test_invalid_user_input_is_rejected_without_builder_call() -> None:
    service, builder, checker = make_service()

    result = service.evaluate_shadow(user=None)

    assert result.status == "invalid_input"
    assert result.audit.error_code == "invalid_user_input"
    assert result.audit.model_result_status == "not_called"
    assert result.audit.reliable_coverage_ratio is None
    assert result.audit.approximate_coverage_ratio is None
    assert result.audit.potential_coverage_ratio is None
    builder.assert_not_called()
    checker.assert_not_called()


def test_invalid_sequence_input_is_rejected_without_builder_call() -> None:
    service, builder, checker = make_service()

    result = service.evaluate_shadow(
        user=make_user(),
        cards=None,
    )

    assert result.status == "invalid_input"
    assert result.audit.error_code == "invalid_feature_input"
    builder.assert_not_called()
    checker.assert_not_called()


def test_service_does_not_modify_input_objects() -> None:
    user = make_user()
    cards = [make_card()]
    accounts = make_accounts()
    transactions = [make_transaction()]
    before = (
        asdict(user),
        [asdict(card) for card in cards],
        [asdict(account) for account in accounts],
        [asdict(txn) for txn in transactions],
    )
    service, _, _ = make_service()

    evaluate(
        service,
        user=user,
        cards=cards,
        accounts=accounts,
        transactions=transactions,
    )

    after = (
        asdict(user),
        [asdict(card) for card in cards],
        [asdict(account) for account in accounts],
        [asdict(txn) for txn in transactions],
    )
    assert after == before


def test_exception_path_does_not_modify_input_objects() -> None:
    user = make_user()
    cards = [make_card()]
    accounts = make_accounts()
    transactions = [make_transaction()]
    before = (
        asdict(user),
        [asdict(card) for card in cards],
        [asdict(account) for account in accounts],
        [asdict(txn) for txn in transactions],
    )
    service = CreditRiskService(
        mode="shadow",
        feature_builder=Mock(side_effect=RuntimeError("unexpected")),
        availability_checker=Mock(),
        now_provider=lambda: NOW,
    )

    result = evaluate(
        service,
        user=user,
        cards=cards,
        accounts=accounts,
        transactions=transactions,
    )

    after = (
        asdict(user),
        [asdict(card) for card in cards],
        [asdict(account) for account in accounts],
        [asdict(txn) for txn in transactions],
    )
    assert result.status == "shadow_error"
    assert after == before


def test_audit_record_cannot_have_rule_impact_overridden() -> None:
    with pytest.raises(TypeError):
        CreditRiskAuditRecord(
            mode="off",
            status="off",
            evaluated_at_utc=NOW.isoformat(),
            feature_version=FEATURE_VERSION,
            model_name="credit-risk-balanced-logistic",
            model_version=None,
            artifact_sha256=None,
            reliable_coverage_ratio=None,
            approximate_coverage_ratio=None,
            potential_coverage_ratio=None,
            missing_features=(),
            approximate_features=(),
            diagnostics=(),
            model_result_status="not_called",
            error_code="",
            rule_impact="changed",
        )
