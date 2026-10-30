"""Stage-1 identity tests for credit-risk shadow runs."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import FrozenInstanceError, asdict
from unittest.mock import Mock, patch

import pytest

from finagent.agent.core import BankingAgent
from finagent.agent.planner import Plan, PlanStep
from finagent.api.service import AgentSessionService
from finagent.data.sqlite import SQLiteRepository
from finagent.risk.credit_risk.integration import BusinessCreditShadowIntegration
from finagent.risk.credit_risk.run import (
    CreditRiskShadowRun,
    SHADOW_RUN_SCHEMA_VERSION,
)
from finagent.tools.base import ToolResult
from tests.support import add_uploaded_transaction, make_store


CREDIT_APPLICATION_STEPS = (
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
)


def apply_credit_application(
    agent: BankingAgent,
    *,
    request_prefix: str,
) -> tuple[dict, str]:
    add_uploaded_transaction(agent)
    reply = agent.handle(
        "我想申请一张工商银行的信用卡",
        request_id=f"{request_prefix}-0",
    )
    final_request_id = ""
    for index, answer in enumerate(CREDIT_APPLICATION_STEPS, start=1):
        final_request_id = f"{request_prefix}-{index}"
        reply = agent.handle(answer, request_id=final_request_id)
    return asdict(reply), final_request_id


def shadow_inputs() -> dict:
    store = make_store()
    return {
        "user": store.get_user("u_001"),
        "cards": tuple(store.cards_of("u_001")),
        "accounts": tuple(store.accounts_of("u_001")),
        "transactions": tuple(store.transactions_of("u_001")),
    }


def test_service_passes_real_request_id_to_agent() -> None:
    service = AgentSessionService(make_store(), llm_mode="off")
    session_id = service.create_session()["session_id"]
    agent = service._sessions[session_id].agent

    with patch.object(agent, "handle", wraps=agent.handle) as handle:
        service.send_message(
            session_id,
            "我还有多少钱",
            request_id="service-real-request-001",
        )

    handle.assert_called_once()
    assert handle.call_args.kwargs["request_id"] == "service-real-request-001"
    assert agent.current_request_id == ""


def test_shadow_run_uses_real_application_and_request_ids(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("FINAGENT_CREDIT_SHADOW_MODE", "shadow")
    service = AgentSessionService(
        make_store(),
        llm_mode="off",
        store_factory=make_store,
        repository=SQLiteRepository(tmp_path / "shadow-identity.db"),
    )
    session_id = service.create_session()["session_id"]
    add_uploaded_transaction(service._sessions[session_id].agent)
    request_ids = [f"application-request-{index}" for index in range(20)]

    reply = service.send_message(
        session_id,
        "我想申请一张工商银行的信用卡",
        request_ids[0],
    )["reply"]
    final_request_id = request_ids[0]
    for index, answer in enumerate(CREDIT_APPLICATION_STEPS, start=1):
        final_request_id = request_ids[index]
        reply = service.send_message(
            session_id,
            answer,
            final_request_id,
        )["reply"]

    application_id = reply["data"]["application_id"]
    agent = service._sessions[session_id].agent
    runs = agent.credit_shadow.recent_runs()

    assert len(runs) == 1
    run = runs[0]
    assert isinstance(run, CreditRiskShadowRun)
    assert run.application_id == application_id
    assert run.request_id == final_request_id
    assert run.shadow_run_id.startswith("SHADOW-")
    assert run.schema_version == SHADOW_RUN_SCHEMA_VERSION
    assert run.audit.application_id == application_id
    assert run.audit.request_id == final_request_id
    assert run.audit.shadow_run_id == run.shadow_run_id
    assert run.model_status == "not_called"
    assert run.rule_impact == "none"

    replayed = service.send_message(
        session_id,
        "确认",
        final_request_id,
    )
    assert replayed["replayed"] is True
    assert len(agent.credit_shadow.recent_runs()) == 1


def test_shadow_off_creates_no_run_or_skip() -> None:
    agent = BankingAgent(
        make_store(),
        "u_001",
        llm_mode="off",
        credit_shadow=BusinessCreditShadowIntegration(mode="off"),
    )

    reply, _ = apply_credit_application(agent, request_prefix="shadow-off")

    assert reply["ok"] is True
    assert agent.credit_shadow.recent_runs() == ()
    assert agent.credit_shadow.recent_skips() == ()


def test_missing_request_id_skips_without_calling_service() -> None:
    service = Mock()
    integration = BusinessCreditShadowIntegration(mode="shadow", service=service)

    run = integration.observe_credit_application(
        **shadow_inputs(),
        application_id="CARD-REAL-001",
        request_id="",
    )

    assert run is None
    service.evaluate_business_shadow.assert_not_called()
    assert len(integration.recent_skips()) == 1
    assert integration.recent_skips()[0].reason == "missing_request_id"
    assert integration.recent_runs() == ()


def test_missing_application_id_skips_without_calling_service() -> None:
    service = Mock()
    integration = BusinessCreditShadowIntegration(mode="shadow", service=service)

    run = integration.observe_credit_application(
        **shadow_inputs(),
        application_id="",
        request_id="request-real-001",
    )

    assert run is None
    service.evaluate_business_shadow.assert_not_called()
    assert len(integration.recent_skips()) == 1
    assert integration.recent_skips()[0].reason == "missing_application_id"
    assert integration.recent_runs() == ()


def test_same_application_and_request_returns_original_run() -> None:
    integration = BusinessCreditShadowIntegration(mode="shadow")
    inputs = shadow_inputs()

    first = integration.observe_credit_application(
        **inputs,
        application_id="CARD-REAL-002",
        request_id="request-real-002",
    )
    second = integration.observe_credit_application(
        **inputs,
        application_id="CARD-REAL-002",
        request_id="request-real-002",
    )
    another_request = integration.observe_credit_application(
        **inputs,
        application_id="CARD-REAL-002",
        request_id="request-real-003",
    )

    assert first is not None
    assert second is first
    assert another_request is not None
    assert another_request.shadow_run_id != first.shadow_run_id
    assert len(integration.recent_runs()) == 2
    assert len(integration.recent_audits()) == 2


def test_shadow_exception_is_traceable_and_does_not_change_reply() -> None:
    failing_service = Mock()
    failing_service.evaluate_business_shadow.side_effect = RuntimeError(
        "simulated shadow failure"
    )
    agent = BankingAgent(
        make_store(),
        "u_001",
        llm_mode="off",
        credit_shadow=BusinessCreditShadowIntegration(
            mode="shadow",
            service=failing_service,
        ),
    )

    reply, final_request_id = apply_credit_application(
        agent,
        request_prefix="shadow-failure",
    )
    runs = agent.credit_shadow.recent_runs()

    assert reply["ok"] is True
    assert reply["data"]["application_id"]
    assert len(runs) == 1
    assert runs[0].application_id == reply["data"]["application_id"]
    assert runs[0].request_id == final_request_id
    assert runs[0].status == "shadow_error"
    assert runs[0].model_status == "not_called"
    assert runs[0].error_code == "credit_shadow_integration_exception"


def test_shadow_run_is_immutable() -> None:
    integration = BusinessCreditShadowIntegration(mode="shadow")
    run = integration.observe_credit_application(
        **shadow_inputs(),
        application_id="CARD-REAL-004",
        request_id="request-real-004",
    )

    assert run is not None
    with pytest.raises(FrozenInstanceError):
        run.application_id = "CARD-OTHER"


def test_stage_two_creates_shadow_database_table(tmp_path) -> None:
    db_path = tmp_path / "stage-one.db"
    SQLiteRepository(db_path)
    connection = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        connection.close()

    assert "credit_shadow_runs" in tables


def test_same_key_concurrent_calls_execute_shadow_service_once() -> None:
    integration = BusinessCreditShadowIntegration(mode="shadow")
    inputs = shadow_inputs()
    original = integration._service.evaluate_business_shadow
    start = threading.Barrier(8)
    entered = threading.Event()
    release = threading.Event()
    counter_lock = threading.Lock()
    calls = 0
    results = []
    errors = []

    def wrapped(**kwargs):
        nonlocal calls
        with counter_lock:
            calls += 1
        entered.set()
        release.wait(timeout=5)
        return original(**kwargs)

    integration._service.evaluate_business_shadow = wrapped

    def worker():
        try:
            start.wait(timeout=5)
            result = integration.observe_credit_application(
                **inputs,
                application_id="CARD-CONCURRENT-001",
                request_id="REQUEST-CONCURRENT-001",
            )
            results.append(result)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    assert entered.wait(timeout=3)
    release.set()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert calls == 1
    assert len(results) == 8
    assert all(result is results[0] for result in results)
    assert len(integration.recent_runs()) == 1
    assert integration._inflight == {}

    replay = integration.observe_credit_application(
        **inputs,
        application_id="CARD-CONCURRENT-001",
        request_id="REQUEST-CONCURRENT-001",
    )
    assert replay is results[0]
    assert calls == 1


def test_same_key_concurrent_service_failure_returns_one_error_run() -> None:
    integration = BusinessCreditShadowIntegration(mode="shadow")
    inputs = shadow_inputs()
    start = threading.Barrier(5)
    entered = threading.Event()
    release = threading.Event()
    counter_lock = threading.Lock()
    calls = 0
    results = []
    errors = []

    def failing_service(**kwargs):
        nonlocal calls
        with counter_lock:
            calls += 1
        entered.set()
        release.wait(timeout=5)
        raise RuntimeError("simulated service failure")

    integration._service.evaluate_business_shadow = failing_service

    def worker():
        try:
            start.wait(timeout=5)
            result = integration.observe_credit_application(
                **inputs,
                application_id="CARD-CONCURRENT-002",
                request_id="REQUEST-CONCURRENT-002",
            )
            results.append(result)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    assert entered.wait(timeout=3)
    release.set()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert calls == 1
    assert len(results) == 5
    assert all(result is results[0] for result in results)
    assert results[0] is not None
    assert results[0].status == "shadow_error"
    assert len(integration.recent_runs()) == 1
    assert integration._inflight == {}


def test_different_keys_can_execute_shadow_service_in_parallel() -> None:
    integration = BusinessCreditShadowIntegration(mode="shadow")
    inputs = shadow_inputs()
    original = integration._service.evaluate_business_shadow
    service_barrier = threading.Barrier(2)
    counter_lock = threading.Lock()
    calls = 0
    results = []
    errors = []

    def wrapped(**kwargs):
        nonlocal calls
        with counter_lock:
            calls += 1
        service_barrier.wait(timeout=3)
        return original(**kwargs)

    integration._service.evaluate_business_shadow = wrapped

    def worker(request_id):
        try:
            results.append(integration.observe_credit_application(
                **inputs,
                application_id="CARD-PARALLEL-001",
                request_id=request_id,
            ))
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("REQUEST-PARALLEL-A",)),
        threading.Thread(target=worker, args=("REQUEST-PARALLEL-B",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert calls == 2
    assert len(results) == 2
    assert len({result.shadow_run_id for result in results if result}) == 2
    assert len(integration.recent_runs()) == 2
    assert integration._inflight == {}


def test_unexpected_error_releases_claim_and_wakes_waiters() -> None:
    integration = BusinessCreditShadowIntegration(mode="shadow")
    inputs = shadow_inputs()
    service_entered = threading.Event()
    release = threading.Event()
    counter_lock = threading.Lock()
    calls = 0
    errors = []

    original_service = integration._service.evaluate_business_shadow

    def wrapped_service(**kwargs):
        nonlocal calls
        with counter_lock:
            calls += 1
        service_entered.set()
        release.wait(timeout=5)
        return original_service(**kwargs)

    integration._service.evaluate_business_shadow = wrapped_service

    def worker():
        try:
            integration.observe_credit_application(
                **inputs,
                application_id="CARD-CONCURRENT-003",
                request_id="REQUEST-CONCURRENT-003",
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    with patch.object(
        integration,
        "_run_from_audit",
        side_effect=RuntimeError("unexpected run construction failure"),
    ):
        for thread in threads:
            thread.start()
        assert service_entered.wait(timeout=3)
        release.set()
        for thread in threads:
            thread.join(timeout=5)

        assert all(not thread.is_alive() for thread in threads)
        assert calls == 1
        assert len(errors) == 2
        assert integration._inflight == {}
        assert integration.recent_runs() == ()

    integration._service.evaluate_business_shadow = original_service
    recovered = integration.observe_credit_application(
        **inputs,
        application_id="CARD-CONCURRENT-003",
        request_id="REQUEST-CONCURRENT-003",
    )
    assert recovered is not None
    assert recovered.shadow_run_id
    assert recovered.application_id == "CARD-CONCURRENT-003"
    assert recovered.request_id == "REQUEST-CONCURRENT-003"
    assert len(integration.recent_runs()) == 1


def test_double_shadow_failure_does_not_change_formal_application_result() -> None:
    off_agent = BankingAgent(
        make_store(),
        "u_001",
        llm_mode="off",
        credit_shadow=BusinessCreditShadowIntegration(mode="off"),
    )
    off_reply, _ = apply_credit_application(
        off_agent,
        request_prefix="double-failure-off",
    )

    shadow_agent = BankingAgent(
        make_store(),
        "u_001",
        llm_mode="off",
        credit_shadow=BusinessCreditShadowIntegration(mode="shadow"),
    )
    with (
        patch.object(
            shadow_agent.credit_shadow,
            "observe_credit_application",
            side_effect=RuntimeError("shadow observation failed"),
        ),
        patch.object(
            shadow_agent.credit_shadow,
            "record_failure",
            side_effect=RuntimeError("failure recorder failed"),
        ),
    ):
        shadow_reply, _ = apply_credit_application(
            shadow_agent,
            request_prefix="double-failure-shadow",
        )

    fields = ("ok", "decision", "score", "reference_limit")
    assert {field: shadow_reply["data"].get(field) for field in fields[1:]} == {
        field: off_reply["data"].get(field) for field in fields[1:]
    }
    assert shadow_reply["ok"] == off_reply["ok"]
    assert shadow_agent.credit_shadow.recent_runs() == ()


def test_failed_card_application_does_not_create_shadow_run() -> None:
    agent = BankingAgent(
        make_store(),
        "u_001",
        llm_mode="off",
        credit_shadow=BusinessCreditShadowIntegration(mode="shadow"),
    )
    tool = agent.registry.get("card.apply")
    plan = Plan(
        intent="card.apply",
        steps=[PlanStep(tool="card.apply", args={})],
    )

    with patch.object(
        tool,
        "run",
        return_value=ToolResult(
            False,
            "simulated formal application failure",
            data={"kind": "credit", "application_id": ""},
        ),
    ):
        reply = agent._execute(plan, agent.context())

    assert reply.ok is False
    assert agent.credit_shadow.recent_runs() == ()
    assert agent.credit_shadow.recent_skips() == ()
