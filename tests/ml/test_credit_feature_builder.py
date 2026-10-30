"""Tests for read-only business credit-risk feature availability."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date

import pytest

from finagent.data.models import Account, Card, Transaction, User
from finagent.risk.credit_risk.contract import UCI_FEATURE_ORDER
from finagent.risk.credit_risk.feature_builder import (
    build_credit_feature_snapshot,
    check_credit_feature_availability,
)


AS_OF = date(2026, 10, 30)


def make_user(user_id: str = "u_test") -> User:
    return User(
        user_id=user_id,
        name="Test User",
        phone="13800138000",
        gender="male",
        age=35,
        verified=True,
    )


def make_credit_card(
    *,
    card_id: str = "CARD_TEST",
    user_id: str = "u_test",
    kind: str = "credit",
    status: str = "active",
    statement_date: str = "2026-10-05",
) -> Card:
    return Card(
        card_id=card_id,
        user_id=user_id,
        kind=kind,
        account_id="ACC_CREDIT",
        status=status,
        credit_limit=50_000.0 if kind == "credit" else 0.0,
        used=1_200.0 if kind == "credit" else 0.0,
        balance=0.0,
        issued_at="2024-01-01T00:00:00",
        statement_amount=1_200.0 if kind == "credit" else 0.0,
        statement_date=statement_date if kind == "credit" else "",
        due_date="2026-10-23",
        minimum_payment=120.0 if kind == "credit" else 0.0,
    )


def make_credit_account(
    *,
    user_id: str = "u_test",
    currency: str = "CNY",
) -> Account:
    return Account(
        account_id="ACC_CREDIT",
        user_id=user_id,
        kind="credit",
        name="Test credit account",
        tail="0001",
        balance=1_200.0,
        currency=currency,
        credit_limit=50_000.0,
        status="active",
    )


def make_payment_account(
    *,
    user_id: str = "u_test",
    currency: str = "CNY",
) -> Account:
    return Account(
        account_id="ACC_CASH",
        user_id=user_id,
        kind="current",
        name="Test payment account",
        tail="0002",
        balance=20_000.0,
        currency=currency,
        status="active",
    )


def make_repayment_transaction(
    *,
    user_id: str = "u_test",
    status: str = "success",
    lifecycle_status: str = "completed",
    timestamp: str = "2026-10-20T12:00:00",
    amount: object = -500.0,
    credit_card: object = "CARD_TEST",
) -> Transaction:
    metadata = (
        {"credit_card": credit_card}
        if credit_card is not None
        else {}
    )
    return Transaction(
        txn_id="TXN_TEST",
        user_id=user_id,
        account_id="ACC_CASH",
        timestamp=timestamp,
        amount=amount,
        merchant="Credit card repayment",
        category="信用卡还款",
        status=status,
        lifecycle_status=lifecycle_status,
        tags=["credit_card_repayment", "card:CARD_TEST"],
        metadata=metadata,
    )


def make_valid_snapshot(customer_type: str = "existing"):
    return build_credit_feature_snapshot(
        make_user(),
        cards=[make_credit_card()],
        accounts=[make_credit_account(), make_payment_account()],
        transactions=[make_repayment_transaction()],
        customer_type=customer_type,
        as_of=AS_OF,
    )


def diagnostic_codes(snapshot) -> set[str]:
    return {diagnostic.code for diagnostic in snapshot.diagnostics}


def snapshot_state(
    user: User,
    cards: list[Card],
    accounts: list[Account],
    transactions: list[Transaction],
) -> tuple[dict, list[dict], list[dict], list[dict]]:
    return (
        asdict(user),
        [asdict(card) for card in cards],
        [asdict(account) for account in accounts],
        [asdict(txn) for txn in transactions],
    )


def test_new_customer_lacks_historical_credit_card_features() -> None:
    snapshot = build_credit_feature_snapshot(
        make_user(),
        customer_type="new",
        as_of=AS_OF,
    )

    assert snapshot.customer_type == "new"
    assert "X1" in snapshot.missing_features
    assert "X12" in snapshot.missing_features
    assert {"X18", "X19", "X20", "X21", "X22", "X23"}.issubset(
        snapshot.missing_features
    )


def test_existing_customer_with_credit_limit_and_statement() -> None:
    snapshot = build_credit_feature_snapshot(
        make_user(),
        cards=[make_credit_card()],
        accounts=[make_credit_account()],
        customer_type="existing",
        as_of=AS_OF,
    )

    assert snapshot.customer_type == "existing"
    assert "X1" in snapshot.available_features
    assert "X12" in snapshot.available_features


@pytest.mark.parametrize("customer_type", ["new", "existing", "unknown"])
def test_repayment_status_features_are_always_missing(
    customer_type: str,
) -> None:
    snapshot = build_credit_feature_snapshot(
        make_user(),
        cards=[make_credit_card()],
        accounts=[make_credit_account(), make_payment_account()],
        transactions=[make_repayment_transaction()],
        customer_type=customer_type,
        as_of=AS_OF,
    )

    assert {"X6", "X7", "X8", "X9", "X10", "X11"}.issubset(
        snapshot.missing_features
    )


def test_reliable_and_approximate_features_remain_separate() -> None:
    snapshot = make_valid_snapshot()

    assert "X1" in snapshot.available_features
    assert "X18" in snapshot.approximate_features
    assert "X18" not in snapshot.available_features
    assert set(snapshot.available_features).isdisjoint(
        snapshot.approximate_features
    )


def test_coverage_ratios_have_distinct_denominators_and_numerators() -> None:
    snapshot = make_valid_snapshot()
    availability = check_credit_feature_availability(snapshot)
    total = len(UCI_FEATURE_ORDER)

    assert availability.reliable_coverage_ratio == (
        len(snapshot.available_features) / total
    )
    assert availability.approximate_coverage_ratio == (
        len(snapshot.approximate_features) / total
    )
    assert availability.potential_coverage_ratio == pytest.approx(
        availability.reliable_coverage_ratio
        + availability.approximate_coverage_ratio
    )
    assert availability.potential_coverage_ratio > (
        availability.reliable_coverage_ratio
    )


def test_missing_features_are_not_filled_with_zero() -> None:
    snapshot = make_valid_snapshot()
    combined = (
        snapshot.available_features
        + snapshot.approximate_features
        + snapshot.missing_features
    )

    assert len(combined) == len(UCI_FEATURE_ORDER)
    assert set(combined) == set(UCI_FEATURE_ORDER)
    assert "X3" in snapshot.missing_features
    assert "X4" in snapshot.missing_features
    assert "X13" in snapshot.missing_features
    assert "X17" in snapshot.missing_features


def test_customer_type_default_and_explicit_sources_are_different() -> None:
    default_snapshot = build_credit_feature_snapshot(
        make_user(),
        cards=[make_credit_card()],
        as_of=AS_OF,
    )
    explicit_snapshot = build_credit_feature_snapshot(
        make_user(),
        cards=[make_credit_card()],
        customer_type="unknown",
        as_of=AS_OF,
    )

    assert default_snapshot.customer_type == "unknown"
    assert explicit_snapshot.customer_type == "unknown"
    assert default_snapshot.source_metadata["customer_type_source"] == (
        "default_unknown"
    )
    assert explicit_snapshot.source_metadata["customer_type_source"] == (
        "explicit_argument"
    )


@pytest.mark.parametrize(
    ("status", "lifecycle_status"),
    [
        ("failed", "completed"),
        ("success", "reversed"),
        ("success", "processing"),
    ],
)
def test_non_successful_or_incomplete_repayment_is_not_approximate(
    status: str,
    lifecycle_status: str,
) -> None:
    snapshot = build_credit_feature_snapshot(
        make_user(),
        cards=[make_credit_card()],
        accounts=[make_credit_account(), make_payment_account()],
        transactions=[
            make_repayment_transaction(
                status=status,
                lifecycle_status=lifecycle_status,
            )
        ],
        customer_type="existing",
        as_of=AS_OF,
    )

    assert "X18" in snapshot.missing_features
    assert "repayment_transaction_status_invalid" in diagnostic_codes(snapshot)


def test_missing_credit_card_link_has_diagnostic() -> None:
    snapshot = build_credit_feature_snapshot(
        make_user(),
        cards=[make_credit_card()],
        accounts=[make_credit_account(), make_payment_account()],
        transactions=[make_repayment_transaction(credit_card=None)],
        customer_type="existing",
        as_of=AS_OF,
    )

    assert "X18" in snapshot.missing_features
    assert "repayment_card_link_missing" in diagnostic_codes(snapshot)


def test_nonexistent_linked_card_has_diagnostic() -> None:
    snapshot = build_credit_feature_snapshot(
        make_user(),
        cards=[make_credit_card()],
        accounts=[make_credit_account(), make_payment_account()],
        transactions=[make_repayment_transaction(credit_card="CARD_UNKNOWN")],
        customer_type="existing",
        as_of=AS_OF,
    )

    assert "X18" in snapshot.missing_features
    assert "repayment_card_not_found" in diagnostic_codes(snapshot)


def test_other_user_card_has_diagnostic() -> None:
    snapshot = build_credit_feature_snapshot(
        make_user(),
        cards=[
            make_credit_card(
                card_id="CARD_OTHER",
                user_id="u_other",
            )
        ],
        accounts=[make_credit_account(), make_payment_account()],
        transactions=[make_repayment_transaction(credit_card="CARD_OTHER")],
        customer_type="existing",
        as_of=AS_OF,
    )

    assert "X18" in snapshot.missing_features
    assert "repayment_card_other_user" in diagnostic_codes(snapshot)
    assert "card_other_user" in diagnostic_codes(snapshot)


def test_non_credit_card_has_diagnostic() -> None:
    snapshot = build_credit_feature_snapshot(
        make_user(),
        cards=[
            make_credit_card(
                card_id="CARD_DEBIT",
                kind="debit",
            )
        ],
        accounts=[make_credit_account(), make_payment_account()],
        transactions=[make_repayment_transaction(credit_card="CARD_DEBIT")],
        customer_type="existing",
        as_of=AS_OF,
    )

    assert "X18" in snapshot.missing_features
    assert "repayment_card_type_mismatch" in diagnostic_codes(snapshot)


def test_repayment_outside_six_month_window_has_diagnostic() -> None:
    snapshot = build_credit_feature_snapshot(
        make_user(),
        cards=[make_credit_card()],
        accounts=[make_credit_account(), make_payment_account()],
        transactions=[
            make_repayment_transaction(timestamp="2025-01-01T12:00:00")
        ],
        customer_type="existing",
        as_of=AS_OF,
    )

    assert "X18" in snapshot.missing_features
    assert "repayment_transaction_out_of_window" in diagnostic_codes(snapshot)


def test_invalid_transaction_timestamp_has_diagnostic() -> None:
    snapshot = build_credit_feature_snapshot(
        make_user(),
        cards=[make_credit_card()],
        accounts=[make_credit_account(), make_payment_account()],
        transactions=[make_repayment_transaction(timestamp="not-a-date")],
        customer_type="existing",
        as_of=AS_OF,
    )

    assert "X18" in snapshot.missing_features
    assert "invalid_transaction_timestamp" in diagnostic_codes(snapshot)


@pytest.mark.parametrize(
    ("amount", "code"),
    [
        (100.0, "repayment_direction_invalid"),
        (float("nan"), "repayment_amount_invalid"),
    ],
)
def test_invalid_repayment_amount_or_direction_has_diagnostic(
    amount: float,
    code: str,
) -> None:
    snapshot = build_credit_feature_snapshot(
        make_user(),
        cards=[make_credit_card()],
        accounts=[make_credit_account(), make_payment_account()],
        transactions=[make_repayment_transaction(amount=amount)],
        customer_type="existing",
        as_of=AS_OF,
    )

    assert "X18" in snapshot.missing_features
    assert code in diagnostic_codes(snapshot)


def test_unverified_currency_has_diagnostic() -> None:
    snapshot = build_credit_feature_snapshot(
        make_user(),
        cards=[make_credit_card()],
        accounts=[make_credit_account()],
        transactions=[make_repayment_transaction()],
        customer_type="existing",
        as_of=AS_OF,
    )

    assert "X18" in snapshot.missing_features
    assert "repayment_currency_unverified" in diagnostic_codes(snapshot)


def test_mismatched_currency_has_diagnostic() -> None:
    snapshot = build_credit_feature_snapshot(
        make_user(),
        cards=[make_credit_card()],
        accounts=[
            make_credit_account(currency="CNY"),
            make_payment_account(currency="USD"),
        ],
        transactions=[make_repayment_transaction()],
        customer_type="existing",
        as_of=AS_OF,
    )

    assert "X18" in snapshot.missing_features
    assert "repayment_currency_mismatch" in diagnostic_codes(snapshot)


@pytest.mark.parametrize(
    ("statement_date", "code"),
    [
        ("not-a-date", "statement_date_invalid"),
        ("2026-07-01", "statement_date_expired"),
        ("2026-11-05", "statement_date_future"),
    ],
)
def test_invalid_statement_date_is_not_reliable(
    statement_date: str,
    code: str,
) -> None:
    snapshot = build_credit_feature_snapshot(
        make_user(),
        cards=[make_credit_card(statement_date=statement_date)],
        accounts=[make_credit_account()],
        customer_type="existing",
        as_of=AS_OF,
    )

    assert "X12" in snapshot.missing_features
    assert code in diagnostic_codes(snapshot)


def test_existing_customer_without_repayment_keeps_x18_to_x23_missing() -> None:
    snapshot = build_credit_feature_snapshot(
        make_user(),
        cards=[make_credit_card()],
        accounts=[make_credit_account(), make_payment_account()],
        customer_type="existing",
        as_of=AS_OF,
    )

    assert {"X18", "X19", "X20", "X21", "X22", "X23"}.issubset(
        snapshot.missing_features
    )
    assert not snapshot.approximate_features


def test_partial_repayment_history_is_not_complete_six_month_history() -> None:
    snapshot = make_valid_snapshot()
    availability = check_credit_feature_availability(snapshot)

    assert "X18" in snapshot.approximate_features
    assert {"X19", "X20", "X21", "X22", "X23"}.issubset(
        snapshot.missing_features
    )
    assert snapshot.source_metadata["history_completeness"]["claimed_complete"] is False
    assert any(
        "do not prove complete six-month" in warning
        for warning in availability.warnings
    )


def test_availability_summary_keeps_approximate_data_distinct() -> None:
    availability = check_credit_feature_availability(make_valid_snapshot())

    assert availability.status == "partial"
    assert availability.reliable_coverage_ratio > 0
    assert availability.approximate_coverage_ratio > 0
    assert "X18" in availability.approximate_features
    assert "X6" in availability.missing_features
    assert any("Approximate features" in warning for warning in availability.warnings)


def test_feature_check_does_not_modify_inputs() -> None:
    user = make_user()
    cards = [make_credit_card()]
    accounts = [make_credit_account(), make_payment_account()]
    transactions = [make_repayment_transaction()]
    before = snapshot_state(user, cards, accounts, transactions)

    build_credit_feature_snapshot(
        user,
        cards=cards,
        accounts=accounts,
        transactions=transactions,
        customer_type="existing",
        as_of=AS_OF,
    )

    after = snapshot_state(user, cards, accounts, transactions)
    assert after == before
