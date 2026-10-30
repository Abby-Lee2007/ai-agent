"""Integration tests for Business Materializer and Credit Risk Shadow Service."""

from __future__ import annotations

import copy
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

import ml.credit_risk.predict as predict_module
from finagent.data.models import Account, Card, Transaction, User
from finagent.data.store import DataStore
from finagent.agent.core import BankingAgent
from finagent.risk.credit_risk.business_contract import (
    BUSINESS_FEATURE_KEYS,
    BusinessFeatureContractError,
    MaterializedFeatureValue,
)
from finagent.risk.credit_risk.materializer import MaterializerInputError
from finagent.risk.credit_risk.integration import BusinessCreditShadowIntegration
from finagent.risk.credit_risk.service import CreditRiskService
from finagent.tools.card_tools import CardApplyTool
from tests.support import add_uploaded_transaction, make_store


AS_OF = datetime(2026, 10, 30, 8, 30, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 30, 8, 35, tzinfo=timezone.utc)
HISTORY_FEATURES = tuple(
    feature
    for feature in BUSINESS_FEATURE_KEYS
    if feature.startswith(
        ("repayment_status_", "statement_amount_", "payment_amount_")
    )
)


def make_user(user_id: str = "u_test") -> User:
    return User(
        user_id=user_id,
        name="Test User",
        phone="13800138000",
        gender="male",
        age=30,
        birthday="1985-06-15",
        verified=True,
    )


def make_card(
    *,
    card_id: str = "CARD_SECRET_001",
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
    currency: str = "CNY",
) -> Account:
    return Account(
        account_id="ACC_CASH",
        user_id="u_test",
        kind="current",
        name="Payment account",
        tail="0002",
        balance=20_000.0,
        currency=currency,
        status="active",
    )


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


def make_service(
    *,
    mode: str = "shadow",
    materializer=None,
) -> CreditRiskService:
    kwargs = {"mode": mode, "now_provider": lambda: NOW}
    if materializer is not None:
        kwargs["business_materializer"] = materializer
    return CreditRiskService(**kwargs)


def normal_inputs():
    return {
        "user": make_user(),
        "cards": (make_card(),),
        "accounts": (make_credit_account(), make_payment_account()),
        "transactions": (make_transaction(),),
        "customer_type": "existing",
        "as_of": AS_OF,
    }


def test_normal_customer_runs_materializer_and_shadow_without_model() -> None:
    service = make_service()

    result = service.evaluate_business_shadow(**normal_inputs())

    assert result.status == "insufficient_data"
    assert result.snapshot is not None
    assert result.assessment is not None
    assert "current_credit_limit" in result.assessment.reliable_features
    assert result.audit.model_result_status == "not_called"
    assert result.audit.rule_impact == "none"


def test_only_current_limit_and_age_are_available() -> None:
    result = make_service().evaluate_business_shadow(**normal_inputs())

    assert result.audit.reliable_features == ("current_credit_limit",)
    assert result.audit.approximate_features == ("verified_age",)
    assert "education_level" in result.audit.missing_features
    assert "marital_status" in result.audit.missing_features


def test_no_formal_six_month_history_is_created() -> None:
    result = make_service().evaluate_business_shadow(**normal_inputs())

    assert set(HISTORY_FEATURES).issubset(result.audit.missing_features)
    assert result.audit.statement_cycle_ids == ()
    assert result.audit.six_month_coverage_complete is False
    assert result.audit.six_month_reliable_complete is False
    assert result.status == "insufficient_data"


def test_approximate_and_policy_blocked_states_are_not_upgraded() -> None:
    result = make_service().evaluate_business_shadow(**normal_inputs())

    assert "verified_age" in result.audit.approximate_features
    assert "verified_age" not in result.audit.reliable_features
    assert "gender" in result.audit.policy_blocked_features
    assert "gender" not in result.audit.reliable_features
    age_feature = result.snapshot.features["verified_age"]
    assert age_feature.status == "approximate"
    assert age_feature.value is not None


def test_missing_features_are_not_filled_or_converted_to_zero() -> None:
    result = make_service().evaluate_business_shadow(**normal_inputs())

    assert "repayment_status_1" in result.audit.missing_features
    assert "repayment_status_1" not in result.snapshot.features
    education = result.snapshot.features["education_level"]
    assert education.status == "missing"
    assert education.value is None


def test_multiple_cards_are_not_aggregated_implicitly() -> None:
    inputs = normal_inputs()
    inputs["cards"] = (
        make_card(card_id="CARD_1", account_id="ACC_1"),
        make_card(card_id="CARD_2", account_id="ACC_2"),
    )
    inputs["accounts"] = (
        make_credit_account(account_id="ACC_1", credit_limit=20_000.0),
        make_credit_account(account_id="ACC_2", credit_limit=30_000.0),
    )

    result = make_service().evaluate_business_shadow(**inputs)

    assert "current_credit_limit" in result.audit.missing_features
    assert {
        diagnostic.code for diagnostic in result.audit.diagnostics
    }.issuperset({"multiple_credit_cards_ambiguous"})


@pytest.mark.parametrize(
    ("card_user", "account_user"),
    [
        ("u_other", "u_test"),
        ("u_test", "u_other"),
    ],
)
def test_card_or_account_ownership_mismatch_degrades_safely(
    card_user: str,
    account_user: str,
) -> None:
    inputs = normal_inputs()
    inputs["cards"] = (make_card(user_id=card_user),)
    inputs["accounts"] = (
        make_credit_account(user_id=account_user),
        make_payment_account(),
    )

    result = make_service().evaluate_business_shadow(**inputs)

    assert result.status == "insufficient_data"
    assert "current_credit_limit" in result.audit.missing_features
    diagnostic_codes = {item.code for item in result.audit.diagnostics}
    assert diagnostic_codes & {
        "card_ownership_mismatch",
        "account_ownership_mismatch",
        "credit_account_ownership_unverified",
    }


def test_audit_contains_no_sensitive_trade_or_identity_values() -> None:
    result = make_service().evaluate_business_shadow(**normal_inputs())
    serialized = repr(asdict(result.audit))

    for sensitive in (
        "13800138000",
        "CARD_SECRET_001",
        "TXN_SECRET_001",
        "Sensitive merchant name",
    ):
        assert sensitive not in serialized


def test_materializer_exception_is_isolated_from_business_flow() -> None:
    materializer = Mock()
    materializer.materialize.side_effect = RuntimeError("unexpected")
    inputs = normal_inputs()
    approval_state = {"decision": "通过", "score": 82, "limit": 10_000.0}
    before = copy.deepcopy(approval_state)

    result = make_service(materializer=materializer).evaluate_business_shadow(
        **inputs
    )

    assert result.status == "shadow_error"
    assert result.audit.error_code == "business_materializer_exception"
    assert result.audit.rule_impact == "none"
    assert approval_state == before


def test_materializer_input_error_returns_invalid_input() -> None:
    materializer = Mock()
    materializer.materialize.side_effect = MaterializerInputError("bad input")

    result = make_service(materializer=materializer).evaluate_business_shadow(
        **normal_inputs()
    )

    assert result.status == "invalid_input"
    assert result.audit.error_code == "materializer_invalid_input"


def test_business_contract_error_returns_shadow_error() -> None:
    materializer = Mock()
    materializer.materialize.side_effect = BusinessFeatureContractError(
        "invalid contract"
    )

    result = make_service(materializer=materializer).evaluate_business_shadow(
        **normal_inputs()
    )

    assert result.status == "shadow_error"
    assert result.audit.error_code == "business_contract_validation_error"
    assert result.audit.rule_impact == "none"


def test_off_mode_does_not_call_business_materializer() -> None:
    materializer = Mock()
    service = make_service(mode="off", materializer=materializer)

    result = service.evaluate_business_shadow(user=None)

    assert result.status == "off"
    assert result.audit.model_result_status == "not_called"
    materializer.materialize.assert_not_called()


def test_shadow_never_loads_or_calls_model(monkeypatch) -> None:
    called = []

    def fail(*args, **kwargs):
        called.append((args, kwargs))
        raise AssertionError("model must not be called")

    monkeypatch.setattr(predict_module, "load_model_artifact", fail)
    monkeypatch.setattr(predict_module, "predict_one", fail)

    result = make_service().evaluate_business_shadow(**normal_inputs())

    assert result.status == "insufficient_data"
    assert result.audit.model_result_status == "not_called"
    assert called == []


def test_shadow_does_not_write_database_or_invoke_approval(monkeypatch) -> None:
    db_calls = []
    approval_calls = []

    def fail_db(*args, **kwargs):
        db_calls.append((args, kwargs))
        raise AssertionError("database write is forbidden")

    def fail_approval(*args, **kwargs):
        approval_calls.append((args, kwargs))
        raise AssertionError("approval path is forbidden")

    monkeypatch.setattr(sqlite3, "connect", fail_db)
    monkeypatch.setattr(DataStore, "log", fail_db)
    monkeypatch.setattr(CardApplyTool, "run", fail_approval)

    approval_state = {"decision": "人工复核", "score": 55, "limit": 5_000.0}
    before = copy.deepcopy(approval_state)
    result = make_service().evaluate_business_shadow(**normal_inputs())

    assert result.status == "insufficient_data"
    assert db_calls == []
    assert approval_calls == []
    assert approval_state == before


def test_shadow_does_not_modify_business_objects() -> None:
    inputs = normal_inputs()
    user = inputs["user"]
    cards = list(inputs["cards"])
    accounts = list(inputs["accounts"])
    transactions = list(inputs["transactions"])
    before = (
        asdict(user),
        [asdict(card) for card in cards],
        [asdict(account) for account in accounts],
        [asdict(txn) for txn in transactions],
    )

    make_service().evaluate_business_shadow(
        user=user,
        cards=tuple(cards),
        accounts=tuple(accounts),
        transactions=tuple(transactions),
        customer_type="existing",
        as_of=AS_OF,
    )

    after = (
        asdict(user),
        [asdict(card) for card in cards],
        [asdict(account) for account in accounts],
        [asdict(txn) for txn in transactions],
    )
    assert after == before


def make_integrated_agent(
    mode: str,
    *,
    service=None,
) -> BankingAgent:
    return BankingAgent(
        make_store(),
        "u_001",
        llm_mode="off",
        credit_shadow=BusinessCreditShadowIntegration(
            mode=mode,
            service=service,
        ),
    )


def apply_credit_card(
    agent: BankingAgent,
    request_id_prefix: str = "shadow-integration-test",
):
    add_uploaded_transaction(agent)
    reply = agent.handle(
        "我想申请一张工商银行的信用卡",
        request_id=f"{request_id_prefix}-0",
    )
    for index, answer in enumerate((
        "13800138000",
        "互联网公司产品经理",
        "25000",
        "3000",
        "30000",
        "日常消费",
        "已上传",
        "同意",
        "教育本科",
        "按时还清,按时还清,按时还清,按时还清,按时还清,按时还清",
        "100,200,300,400,500,600",
        "1000,2000",
        "确认",
    ), start=1):
        reply = agent.handle(
            answer,
            request_id=f"{request_id_prefix}-{index}",
        )
    return reply


def approval_result(reply) -> dict:
    return {
        "ok": reply.ok,
        "kind": reply.kind,
        "text": reply.text,
        "decision": reply.data["decision"],
        "score": reply.data["score"],
        "reference_limit": reply.data["reference_limit"],
    }


def test_business_shadow_defaults_off_from_environment(monkeypatch) -> None:
    monkeypatch.delenv("FINAGENT_CREDIT_SHADOW_MODE", raising=False)
    integration = BusinessCreditShadowIntegration.from_env()
    assert integration.mode == "off"
    assert integration.enabled is False


def test_off_agent_does_not_call_materializer_service() -> None:
    service = Mock()
    agent = make_integrated_agent("off", service=service)

    reply = apply_credit_card(agent)

    assert reply.ok is True
    service.evaluate_business_shadow.assert_not_called()
    assert agent.credit_shadow.recent_audits() == ()


def test_on_agent_runs_shadow_without_changing_business_result(monkeypatch) -> None:
    model_calls = []

    def fail_model(*args, **kwargs):
        model_calls.append((args, kwargs))
        raise AssertionError("model must not be called")

    monkeypatch.setattr(predict_module, "load_model_artifact", fail_model)
    monkeypatch.setattr(predict_module, "predict_one", fail_model)

    off_reply = apply_credit_card(make_integrated_agent("off"))
    agent = make_integrated_agent("shadow")
    on_reply = apply_credit_card(agent)

    assert approval_result(on_reply) == approval_result(off_reply)
    assert model_calls == []
    audits = agent.credit_shadow.recent_audits()
    assert len(audits) == 1
    assert audits[0].status == "insufficient_data"
    assert audits[0].model_result_status == "not_called"
    assert audits[0].six_month_coverage_complete is False
    assert audits[0].six_month_reliable_complete is False


def test_agent_reply_and_sse_payload_do_not_expose_shadow() -> None:
    agent = make_integrated_agent("shadow")
    reply = apply_credit_card(agent)
    payload = asdict(reply)
    serialized = json.dumps(payload, ensure_ascii=False)

    assert "shadow" not in serialized.lower()
    assert "credit_risk" in serialized.lower()
    assert reply.data["credit_risk_model"]["executed"] is True
    assert reply.data["credit_risk_model"]["probability"] is not None
    assert all("shadow" not in str(item).lower() for item in reply.trace)
    assert all("shadow" not in str(key).lower() for key in reply.data)


def test_shadow_exception_does_not_change_business_result() -> None:
    off_reply = apply_credit_card(make_integrated_agent("off"))
    failing_service = Mock()
    failing_service.evaluate_business_shadow.side_effect = RuntimeError(
        "shadow failure"
    )
    agent = make_integrated_agent("shadow", service=failing_service)

    on_reply = apply_credit_card(agent)
    audits = agent.credit_shadow.recent_audits()

    assert approval_result(on_reply) == approval_result(off_reply)
    assert len(audits) == 1
    assert audits[0].status == "shadow_error"
    assert audits[0].error_code == "credit_shadow_integration_exception"
    assert audits[0].rule_impact == "none"


def test_agent_shadow_path_does_not_open_sqlite(monkeypatch) -> None:
    def fail_connect(*args, **kwargs):
        raise AssertionError("database is forbidden in shadow path")

    monkeypatch.setattr(sqlite3, "connect", fail_connect)
    agent = make_integrated_agent("shadow")
    reply = apply_credit_card(agent)

    assert reply.ok is True
    assert len(agent.credit_shadow.recent_audits()) == 1


def test_shadow_audit_buffer_contains_no_sensitive_identity_values() -> None:
    agent = make_integrated_agent("shadow")
    apply_credit_card(agent)
    serialized = repr([asdict(audit) for audit in agent.credit_shadow.recent_audits()])

    for sensitive in (
        "13800138000",
        "CARD000001",
        "TXN",
        "互联网公司产品经理",
    ):
        assert sensitive not in serialized
