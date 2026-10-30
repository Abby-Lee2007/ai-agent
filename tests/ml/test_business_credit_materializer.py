"""Tests for the read-only Business Credit Feature Materializer v0.1."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from finagent.data.models import Account, Card, Transaction, User
from finagent.risk.credit_risk.business_contract import BUSINESS_FEATURE_KEYS
from finagent.risk.credit_risk.materializer import BusinessFeatureMaterializer


AS_OF = datetime(2026, 10, 30, 8, 30, tzinfo=timezone.utc)
HISTORY_FEATURES = tuple(
    feature
    for feature in BUSINESS_FEATURE_KEYS
    if feature.startswith(
        ("repayment_status_", "statement_amount_", "payment_amount_")
    )
)


def make_user(
    *,
    user_id: str = "u_test",
    birthday: str = "1985-06-15",
) -> User:
    return User(
        user_id=user_id,
        name="Test User",
        phone="13800138000",
        gender="male",
        age=30,
        birthday=birthday,
        verified=True,
    )


def make_credit_card(
    *,
    card_id: str = "CARD_MATERIALIZER_001",
    user_id: str = "u_test",
    account_id: str = "ACC_CREDIT",
    credit_limit: float = 50_000.0,
) -> Card:
    return Card(
        card_id=card_id,
        user_id=user_id,
        kind="credit",
        account_id=account_id,
        status="active",
        credit_limit=credit_limit,
        used=1_200.0,
    )


def make_credit_account(
    *,
    user_id: str = "u_test",
    account_id: str = "ACC_CREDIT",
    credit_limit: float = 50_000.0,
    currency: str = "CNY",
) -> Account:
    return Account(
        account_id=account_id,
        user_id=user_id,
        kind="credit",
        name="Credit account",
        tail="0001",
        balance=1_200.0,
        currency=currency,
        credit_limit=credit_limit,
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
        name="Payment account",
        tail="0002",
        balance=20_000.0,
        currency=currency,
        status="active",
    )


def make_repayment_transaction(
    *,
    card_id: str = "CARD_MATERIALIZER_001",
    user_id: str = "u_test",
) -> Transaction:
    return Transaction(
        txn_id="TXN_MATERIALIZER_001",
        user_id=user_id,
        account_id="ACC_CASH",
        timestamp="2026-10-20T12:00:00",
        amount=-500.0,
        merchant="Credit card repayment",
        category="信用卡还款",
        status="success",
        lifecycle_status="completed",
        tags=["credit_card_repayment"],
        metadata={"credit_card": card_id},
    )


def materialize(
    user: User,
    *,
    cards: tuple[Card, ...] = (),
    accounts: tuple[Account, ...] = (),
    transactions: tuple[Transaction, ...] = (),
    customer_type: str = "existing",
):
    return BusinessFeatureMaterializer().materialize(
        user,
        cards=cards,
        accounts=accounts,
        transactions=transactions,
        as_of=AS_OF,
        customer_type=customer_type,
    )


def diagnostic_codes(snapshot) -> set[str]:
    return {diagnostic.code for diagnostic in snapshot.diagnostics}


def test_new_customer_has_no_formal_statement_history() -> None:
    snapshot = materialize(
        make_user(),
        customer_type="new",
    )

    assert snapshot.customer_type == "new"
    assert "current_credit_limit" in snapshot.coverage_summary.missing_features
    assert set(HISTORY_FEATURES).issubset(
        snapshot.coverage_summary.missing_features
    )
    assert snapshot.coverage_summary.statement_cycles == ()
    assert snapshot.coverage_summary.six_month_coverage_complete is False
    assert "formal_statement_history_missing" in diagnostic_codes(snapshot)


def test_existing_customer_with_partial_business_data() -> None:
    snapshot = materialize(
        make_user(),
        cards=(make_credit_card(account_id="ACC_MISSING"),),
        accounts=(),
    )

    assert "current_credit_limit" in snapshot.coverage_summary.missing_features
    assert snapshot.coverage_summary.reliable_coverage_ratio == 0.0
    assert snapshot.coverage_summary.six_month_reliable_complete is False
    assert "credit_account_ownership_unverified" in diagnostic_codes(snapshot)


def test_repayment_transaction_without_formal_statement_stays_missing() -> None:
    snapshot = materialize(
        make_user(),
        cards=(make_credit_card(),),
        accounts=(make_credit_account(), make_payment_account()),
        transactions=(make_repayment_transaction(),),
    )

    assert set(HISTORY_FEATURES).issubset(
        snapshot.coverage_summary.missing_features
    )
    assert "formal_statement_cycle_missing" in diagnostic_codes(snapshot)
    assert snapshot.coverage_summary.six_month_coverage_complete is False
    assert snapshot.coverage_summary.six_month_reliable_complete is False


def test_account_ownership_failure_blocks_current_credit_limit() -> None:
    snapshot = materialize(
        make_user(),
        cards=(make_credit_card(),),
        accounts=(
            make_credit_account(user_id="u_other"),
            make_payment_account(),
        ),
    )

    assert "current_credit_limit" in snapshot.coverage_summary.missing_features
    assert "account_ownership_mismatch" in diagnostic_codes(snapshot)
    assert "credit_account_ownership_unverified" in diagnostic_codes(snapshot)


def test_card_ownership_failure_blocks_current_credit_limit() -> None:
    snapshot = materialize(
        make_user(),
        cards=(make_credit_card(user_id="u_other"),),
        accounts=(make_credit_account(), make_payment_account()),
    )

    assert "current_credit_limit" in snapshot.coverage_summary.missing_features
    assert "card_ownership_mismatch" in diagnostic_codes(snapshot)


def test_repayment_currency_mismatch_is_diagnosed() -> None:
    snapshot = materialize(
        make_user(),
        cards=(make_credit_card(),),
        accounts=(
            make_credit_account(currency="CNY"),
            make_payment_account(currency="USD"),
        ),
        transactions=(make_repayment_transaction(),),
    )

    assert "repayment_currency_mismatch" in diagnostic_codes(snapshot)
    assert set(HISTORY_FEATURES).issubset(
        snapshot.coverage_summary.missing_features
    )


def test_multiple_credit_cards_are_not_aggregated_implicitly() -> None:
    snapshot = materialize(
        make_user(),
        cards=(
            make_credit_card(card_id="CARD_001", account_id="ACC_001"),
            make_credit_card(card_id="CARD_002", account_id="ACC_002"),
        ),
        accounts=(
            make_credit_account(account_id="ACC_001", credit_limit=20_000.0),
            make_credit_account(account_id="ACC_002", credit_limit=30_000.0),
        ),
    )

    assert "current_credit_limit" in snapshot.coverage_summary.missing_features
    assert "multiple_credit_cards_ambiguous" in diagnostic_codes(snapshot)


def test_user_without_credit_card_has_no_current_limit() -> None:
    snapshot = materialize(
        make_user(),
        accounts=(make_payment_account(),),
    )

    assert "current_credit_limit" in snapshot.coverage_summary.missing_features
    assert "no_active_credit_card" in diagnostic_codes(snapshot)


def test_normal_data_generates_valid_snapshot() -> None:
    snapshot = materialize(
        make_user(),
        cards=(make_credit_card(),),
        accounts=(make_credit_account(), make_payment_account()),
    )

    assert "current_credit_limit" in snapshot.coverage_summary.reliable_features
    assert "verified_age" in snapshot.coverage_summary.approximate_features
    assert "gender" in snapshot.coverage_summary.policy_blocked_features
    assert snapshot.coverage_summary.six_month_coverage_complete is False
    assert snapshot.coverage_summary.six_month_reliable_complete is False
    assert snapshot.coverage_summary.reliable_coverage_ratio > 0


def test_no_repayment_and_no_statement_creates_no_history_claims() -> None:
    snapshot = materialize(
        make_user(),
        cards=(make_credit_card(),),
        accounts=(make_credit_account(), make_payment_account()),
        transactions=(),
    )

    assert set(HISTORY_FEATURES).issubset(
        snapshot.coverage_summary.missing_features
    )
    assert snapshot.coverage_summary.statement_cycles == ()
    assert snapshot.coverage_summary.completeness_by_period == {}


def test_materializer_does_not_mutate_inputs() -> None:
    user = make_user()
    cards = [make_credit_card()]
    accounts = [make_credit_account(), make_payment_account()]
    transactions = [make_repayment_transaction()]
    before = (
        asdict(user),
        [asdict(card) for card in cards],
        [asdict(account) for account in accounts],
        [asdict(txn) for txn in transactions],
    )

    materialize(
        user,
        cards=tuple(cards),
        accounts=tuple(accounts),
        transactions=tuple(transactions),
    )

    after = (
        asdict(user),
        [asdict(card) for card in cards],
        [asdict(account) for account in accounts],
        [asdict(txn) for txn in transactions],
    )
    assert after == before


def test_references_are_opaque_uuid_values() -> None:
    snapshot = materialize(
        make_user(user_id="u_sensitive_identifier"),
        cards=(make_credit_card(),),
        accounts=(make_credit_account(), make_payment_account()),
    )

    assert snapshot.customer_ref.startswith("CUST_")
    assert "u_sensitive_identifier" not in snapshot.customer_ref
    assert all(
        source.source_record_ref.startswith("SRC_")
        for source in snapshot.source_manifest
    )
