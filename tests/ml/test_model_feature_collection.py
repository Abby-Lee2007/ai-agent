"""Tests for the top-five application model feature collection."""

from __future__ import annotations

import math

import ml.credit_risk.predict as predict_module
import ml.credit_risk.top5_model as top5_module
from finagent.data.models import Account, Card, User
from finagent.risk.credit_risk.model_feature_collection import (
    FEATURE_ORDER,
    FEATURE_SPECS,
    REQUIRED_FEATURE_ORDER,
    REQUIRED_MODEL_FEATURES,
    CreditRiskFeatureCollection,
    feature_contract_payload,
)
from tests.support import add_uploaded_transaction, make_agent, make_store


MODEL_CORE_ANSWERS = (
    "教育本科",
    "按时还清,按时还清,按时还清,按时还清,按时还清,按时还清",
    "100,200,300,400,500,600",
    "1000,2000",
)


def make_collection() -> CreditRiskFeatureCollection:
    store = make_store()
    return CreditRiskFeatureCollection.from_context(
        user=store.get_user("u_001"),
        cards=store.cards_of("u_001"),
        accounts=store.accounts_of("u_001"),
    )


def fill_all_groups(collection: CreditRiskFeatureCollection) -> None:
    collection.apply_text("教育本科；婚姻未婚")
    collection.apply_text(
        "按时还清,按时还清,按时还清,按时还清,按时还清,按时还清"
    )
    collection.apply_text("100,200,300,400,500,600")
    collection.apply_text("1000,2000")


def test_contract_exposes_top_five_inputs_and_verified_model_status() -> None:
    payload = feature_contract_payload()

    assert payload["feature_order"] == [
        "X6", "X7", "X8", "X9", "X10", "X11",
        "X18", "X19", "X20", "X21", "X22", "X23",
        "X12", "X13", "X1", "X3",
    ]
    assert payload["feature_importance_order"] == list(REQUIRED_MODEL_FEATURES)
    assert payload["required_feature_order"] == list(REQUIRED_FEATURE_ORDER)
    assert len(payload["all_supported_feature_order"]) == 23
    assert payload["artifact_status"] == "available"
    assert payload["prediction_available"] is True


def test_integer_and_category_features_reject_fractional_codes() -> None:
    assert FEATURE_SPECS["X5"].validate(32.5)[0] is False
    assert FEATURE_SPECS["X6"].validate(1.5)[0] is False
    assert FEATURE_SPECS["X6"].validate(1)[0] is True


def test_existing_business_data_fills_only_trusted_available_fields() -> None:
    collection = make_collection()
    values = collection.values

    assert values["X1"].status == "verified"
    assert values["X1"].source == "existing_credit_account"
    assert values["X2"].status == "missing"
    assert values["X5"].status == "collected"
    assert values["X3"].status == "missing"
    assert values["X4"].status == "missing"


def test_no_existing_credit_card_does_not_invent_current_limit() -> None:
    user = User(
        user_id="u_new",
        name="New User",
        phone="13800138000",
        gender="unknown",
        age=0,
    )
    collection = CreditRiskFeatureCollection.from_context(
        user=user,
        cards=(),
        accounts=(),
    )

    assert collection.values["X1"].status == "missing"
    assert collection.values["X1"].value is None
    assert collection.values["X2"].status == "missing"
    assert collection.values["X5"].status == "missing"


def test_new_customer_can_provide_limit_and_repayment_history_in_one_turn() -> None:
    user = User(
        user_id="u_new",
        name="New User",
        phone="13800138000",
        gender="unknown",
        age=30,
    )
    collection = CreditRiskFeatureCollection.from_context(
        user=user,
        cards=(),
        accounts=(),
    )
    collection.apply_text(
        "当前额度5万；教育本科；"
        "还款状态：按时还清,按时还清,按时还清,按时还清,按时还清,按时还清；"
        "还款金额：100,200,300,400,500,600；账单金额：1000,2000"
    )

    assert collection.progress_payload()["required_complete"] is True
    assert collection.model_input_payload()["features"] == {
        "X1": 50_000,
        "X3": 2,
        "X6": -1, "X7": -1, "X8": -1, "X9": -1, "X10": -1, "X11": -1,
        "X12": 1000, "X13": 2000,
        "X18": 100, "X19": 200, "X20": 300, "X21": 400, "X22": 500, "X23": 600,
    }


def test_multiple_credit_cards_do_not_merge_current_limits() -> None:
    user = User(user_id="u_multi", name="Multi User", phone="13800138000")
    cards = (
        Card("card-a", user.user_id, "credit", "credit-a", status="active"),
        Card("card-b", user.user_id, "credit", "credit-b", status="active"),
    )
    accounts = (
        Account(
            "credit-a", user.user_id, "credit", "Credit A", "0001",
            credit_limit=30_000, status="active",
        ),
        Account(
            "credit-b", user.user_id, "credit", "Credit B", "0002",
            credit_limit=50_000, status="active",
        ),
    )
    collection = CreditRiskFeatureCollection.from_context(
        user=user,
        cards=cards,
        accounts=accounts,
    )

    assert collection.values["X1"].status == "missing"
    assert collection.values["X1"].value is None


def test_multi_field_basic_answer_updates_progress_without_repeating_fields() -> None:
    collection = make_collection()
    collection.apply_text("教育本科；婚姻未婚；年龄32")

    assert collection.values["X3"].value == 2
    assert collection.values["X4"].value == 2
    assert collection.values["X5"].value == 32
    assert collection.next_group() == "repayment_status"
    progress = collection.progress_payload()
    assert progress["collected_features"] == 4
    assert progress["next_group"] == "repayment_status"
    assert "教育程度" not in collection.next_question()


def test_required_repayment_group_cannot_be_skipped() -> None:
    collection = make_collection()
    collection.apply_text("教育本科")
    collection.apply_text("跳过本组")

    assert collection.values["X6"].status == "missing"
    assert collection.values["X9"].status == "missing"
    assert collection.last_errors
    assert collection.next_group() == "repayment_status"
    assert collection.progress_payload()["required_complete"] is False


def test_field_level_refusal_keeps_other_basic_answers() -> None:
    collection = make_collection()
    collection.apply_text("教育本科；不提供婚姻")

    assert collection.values["X3"].status == "collected"
    assert collection.values["X3"].value == 2
    assert collection.values["X4"].status == "declined"
    assert collection.values["X4"].value is None
    assert collection.next_group() == "repayment_status"


def test_six_statement_amounts_are_parsed_and_wan_is_supported() -> None:
    collection = make_collection()
    collection.apply_text("教育本科")
    collection.apply_text(
        "按时还清,按时还清,按时还清,按时还清,按时还清,按时还清"
    )
    collection.apply_text("100,200,300,400,500,600")
    collection.apply_text("账单金额：-1000,2万元")

    assert collection.values["X12"].value == -1000
    assert collection.values["X13"].value == 20_000
    assert collection.next_group() is None
    assert collection.progress_payload()["required_complete"] is True


def test_optional_features_can_be_supplied_in_one_follow_up() -> None:
    collection = make_collection()
    collection.apply_text("教育本科；婚姻未婚")
    collection.apply_text("按时还清,按时还清,按时还清,按时还清,按时还清,按时还清")
    collection.apply_text("100,200,300,400,500,600")
    collection.apply_text(
        "账单金额：1000,2000,3000,4000,5000,6000"
    )

    assert collection.values["X3"].value == 2
    assert collection.values["X4"].value == 2
    assert collection.values["X12"].value == 1000
    assert collection.values["X18"].value == 100
    assert collection.progress_payload()["required_complete"] is True


def test_invalid_six_amount_list_is_rejected_and_can_be_retried() -> None:
    collection = make_collection()
    collection.apply_text("教育本科")
    collection.apply_text(
        "按时还清,按时还清,按时还清,按时还清,按时还清,按时还清"
    )

    collection.apply_text("100,200,300,400,500,not-a-number")
    assert collection.next_group() == "payment_amounts"
    assert collection.values["X18"].status == "missing"
    assert collection.last_errors

    collection.apply_text("100,200,300,400,500,600")
    collection.apply_text("1000,2000")
    assert collection.next_group() is None
    assert collection.values["X23"].value == 600


def test_top_five_features_enable_prediction_while_rest_are_supplementary() -> None:
    collection = make_collection()
    fill_all_groups(collection)

    progress = collection.progress_payload()
    assert progress["collected_features"] == 18
    assert progress["required_collected_features"] == 5
    assert progress["required_complete"] is True
    assert progress["ready_for_prediction"] is True
    assert progress["model_status"] == "ready"
    assert progress["policy_allowed_for_decision"] is False
    assert collection.next_group() is None
    collected_values = {
        item["key"]: item["display_value"]
        for item in progress["collected_values"]
    }
    assert len(collected_values) == 18
    assert collected_values["X3"] == "本科/大专"
    assert collected_values["X6"] == "按时还清"


def test_labeled_correction_updates_a_previously_collected_field() -> None:
    collection = make_collection()
    fill_all_groups(collection)
    collection.apply_text("修改教育为研究生；婚姻已婚")

    assert collection.values["X3"].value == 1
    assert collection.values["X4"].value == 1
    assert collection.last_errors == ()


def test_labeled_history_correction_updates_the_original_six_slots() -> None:
    collection = make_collection()
    fill_all_groups(collection)
    collection.apply_text(
        "修改还款状态：逾期1个月,按时还清,无消费,0,-1,2"
    )

    assert collection.values["X6"].value == 1
    assert collection.values["X9"].value == 0
    assert collection.values["X11"].value == 2


def test_collection_state_round_trips_for_pending_session_recovery() -> None:
    store = make_store()
    collection = CreditRiskFeatureCollection.from_context(
        user=store.get_user("u_001"),
        cards=store.cards_of("u_001"),
        accounts=store.accounts_of("u_001"),
    )
    collection.apply_text("教育本科；婚姻未婚")
    restored = CreditRiskFeatureCollection.from_context(
        user=store.get_user("u_001"),
        cards=store.cards_of("u_001"),
        accounts=store.accounts_of("u_001"),
        previous=collection.to_dict(),
    )

    assert restored.values["X3"].value == 2
    assert restored.values["X4"].value == 2
    assert restored.next_group() == "repayment_status"


def test_pending_agent_collection_survives_export_and_restore() -> None:
    agent = make_agent()
    add_uploaded_transaction(agent)
    reply = agent.handle("我想申请一张工商银行的信用卡")
    for answer in (
        "13800138000",
        "互联网公司产品经理",
        "25000",
        "3000",
        "30000",
        "日常消费",
        "已上传",
        "同意",
        "教育本科；婚姻未婚",
    ):
        reply = agent.handle(answer)

    exported = agent.export_pending()
    assert exported is not None
    assert exported["restorable"] is True
    assert "13800138000" not in str(exported)
    assert "phone" in exported["redacted_fields"]
    assert "credit_model_features" in exported["slots"]

    restored_agent = make_agent()
    add_uploaded_transaction(restored_agent)
    restored_agent.restore_pending(exported)
    reply = restored_agent.handle("13800138000")
    assert reply.kind == "question"
    assert "还款状态" in reply.text

    for answer in MODEL_CORE_ANSWERS[1:]:
        reply = restored_agent.handle(answer)

    assert reply.kind == "confirm"
    assert reply.data["credit_risk_collection"]["required_collected_features"] == 5
    assert reply.data["credit_risk_collection"]["next_group"] is None


def test_repayment_history_answer_does_not_switch_to_bill_analysis() -> None:
    agent = make_agent()
    add_uploaded_transaction(agent)
    agent.handle("我想申请一张工商银行的信用卡")
    for answer in (
        "13800138000",
        "互联网公司产品经理",
        "25000",
        "3000",
        "30000",
        "日常消费",
        "已上传",
        "同意",
    ):
        agent.handle(answer)

    reply = agent.handle("按时还款；按时还清；无消费；逾期还款")

    assert reply.intent == "card.apply"
    assert reply.kind == "question"
    assert "逾期还款" in reply.text or "修正" in reply.text
    assert agent.session.pending is not None
    assert agent.session.pending.intent == "card.apply"
    assert agent.session.pending.missing == ["credit_model_features"]


def test_sensitive_fields_remain_blocked_for_formal_decisions() -> None:
    collection = make_collection()
    collection.apply_text("性别女；教育本科；婚姻未婚")

    assert collection.values["X2"].decision_allowed is False
    assert collection.values["X3"].decision_allowed is False
    assert collection.values["X4"].decision_allowed is False
    assert collection.progress_payload()["policy_allowed_for_decision"] is False
    assert set(collection.progress_payload()["required_feature_keys"]) == {
        "REPAYMENT_STATUS_6",
        "REPAYMENT_AMOUNT_6",
        "STATEMENT_AMOUNT_2",
        "X1",
        "X3",
    }


def test_agent_calls_top_five_model_and_keeps_formal_result_separate(
    monkeypatch,
) -> None:
    calls = []
    original_predict = top5_module.predict_top5

    def spy_predict(features, **kwargs):
        calls.append(dict(features))
        return original_predict(features, **kwargs)

    def fail_old_model(*args, **kwargs):
        raise AssertionError("old predict_one must not be called")

    monkeypatch.setattr(top5_module, "predict_top5", spy_predict)
    monkeypatch.setattr(predict_module, "load_model_artifact", fail_old_model)
    monkeypatch.setattr(predict_module, "predict_one", fail_old_model)
    agent = make_agent()
    add_uploaded_transaction(agent)
    reply = agent.handle("我想申请一张工商银行的信用卡")
    for answer in (
        "13800138000",
        "互联网公司产品经理",
        "25000",
        "3000",
        "30000",
        "日常消费",
        "已上传",
        "同意",
        *MODEL_CORE_ANSWERS,
    ):
        reply = agent.handle(answer)

    assert reply.kind == "confirm"
    assert reply.data["credit_risk_collection"]["required_collected_features"] == 5
    assert reply.data["credit_risk_model"]["status"] == "ready"

    final = agent.handle("确认")
    assert final.ok is True
    assert final.kind == "executed"
    assert final.data["decision"] in {"通过", "转人工复核", "未通过"}
    assert final.data["credit_risk_model"]["executed"] is True
    assert final.data["credit_risk_model"]["prediction_status"] == "success"
    assert math.isfinite(final.data["credit_risk_model"]["predicted_probability"])
    assert calls and set(calls[0]) == set(REQUIRED_FEATURE_ORDER)
    assert "credit_model_features" not in final.trace[0]["args"]


def test_model_feature_collection_does_not_change_formal_card_decision() -> None:
    def run_application(model_answers):
        agent = make_agent()
        add_uploaded_transaction(agent)
        agent.handle("我想申请一张工商银行的信用卡")
        reply = None
        for answer in (
            "13800138000",
            "互联网公司产品经理",
            "25000",
            "3000",
            "30000",
            "日常消费",
            "已上传",
            "同意",
            *model_answers,
        ):
            reply = agent.handle(answer)
        assert reply.kind == "confirm"
        return agent.handle("确认")

    first = run_application(MODEL_CORE_ANSWERS)
    second = run_application(MODEL_CORE_ANSWERS)

    fields = ("decision", "score", "reference_limit")
    assert {key: first.data[key] for key in fields} == {
        key: second.data[key] for key in fields
    }


def test_model_failure_does_not_change_formal_application(monkeypatch) -> None:
    def fail_prediction(*args, **kwargs):
        raise RuntimeError("simulated model failure")

    monkeypatch.setattr(top5_module, "predict_top5", fail_prediction)
    agent = make_agent()
    add_uploaded_transaction(agent)
    agent.handle("我想申请一张工商银行的信用卡")
    for answer in (
        "13800138000",
        "互联网公司产品经理",
        "25000",
        "3000",
        "30000",
        "日常消费",
        "已上传",
        "同意",
        *MODEL_CORE_ANSWERS,
    ):
        reply = agent.handle(answer)
    assert reply.kind == "confirm"
    final = agent.handle("确认")

    assert final.ok is True
    assert final.data["decision"] in {"通过", "转人工复核", "未通过"}
    assert final.data["credit_risk_model"]["prediction_status"] == "model_error"
    assert final.data["credit_risk_model"]["probability"] is None


def test_high_model_risk_escalates_formal_pass_to_manual_review(monkeypatch) -> None:
    def high_risk_prediction(*args, **kwargs):
        return {
            "prediction_status": "success",
            "status": "success",
            "model_name": "test-high-risk-model",
            "model_version": "test",
            "model_type": "test",
            "model_sha256": "test",
            "predicted_probability": 0.82,
            "probability": 0.82,
            "threshold": 0.5,
            "predicted_label": 1,
            "risk_label": "较高模型风险",
            "risk_label_text": "模型识别到较高的违约风险",
            "feature_completeness": 1.0,
            "feature_quality": "test",
            "missing_features": [],
            "feature_source_summary": {},
            "model_limitations": [],
            "fairness_warning": "test only",
            "explanation": "test only",
        }

    monkeypatch.setattr(top5_module, "predict_top5", high_risk_prediction)
    agent = make_agent()
    add_uploaded_transaction(agent)
    agent.handle("我想申请一张工商银行的信用卡")
    for answer in (
        "13800138000",
        "互联网公司产品经理",
        "25000",
        "3000",
        "30000",
        "日常消费",
        "已上传",
        "同意",
        *MODEL_CORE_ANSWERS,
    ):
        reply = agent.handle(answer)
    assert reply.kind == "confirm"
    final = agent.handle("确认")

    assert final.ok is True
    assert final.data["decision"] == "转人工复核"
    assert final.data["formal_rule_decision"] == "通过"
    assert final.data["risk_gate_applied"] is True
    assert final.data["credit_risk_model"]["business_rule_result"]["decision"] == "通过"
    assert final.data["credit_risk_model"]["final_application_status"] == "转人工复核"
    assert "模拟预审结果：通过" not in final.text

