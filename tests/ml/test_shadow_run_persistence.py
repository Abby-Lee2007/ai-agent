"""SQLite persistence tests for credit-risk shadow runs."""

from __future__ import annotations

import json
import sqlite3
import threading
from unittest.mock import Mock, patch

import pytest

from finagent.api.service import AgentSessionService
from finagent.data.sqlite import SQLiteRepository
from finagent.risk.credit_risk.integration import BusinessCreditShadowIntegration
from finagent.risk.credit_risk.run_repository import CreditShadowRunRepository
from tests.support import add_uploaded_transaction, make_store


APPLICATION_STEPS = (
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


def input_objects():
    store = make_store()
    return {
        "user": store.get_user("u_001"),
        "cards": tuple(store.cards_of("u_001")),
        "accounts": tuple(store.accounts_of("u_001")),
        "transactions": tuple(store.transactions_of("u_001")),
    }


def ensure_uploaded(service: AgentSessionService, session_id: str) -> None:
    add_uploaded_transaction(service._sessions[session_id].agent)


def apply_application(service: AgentSessionService, session_id: str):
    ensure_uploaded(service, session_id)
    reply = service.send_message(
        session_id,
        "我想申请一张工商银行的信用卡",
        "persist-request-0",
    )["reply"]
    for index, answer in enumerate(APPLICATION_STEPS, start=1):
        request_id = f"persist-request-{index}"
        reply = service.send_message(
            session_id,
            answer,
            request_id,
        )["reply"]
    return reply, request_id


def shadow_run_count(db_path) -> int:
    connection = sqlite3.connect(db_path)
    try:
        return int(connection.execute(
            "SELECT COUNT(*) FROM credit_shadow_runs"
        ).fetchone()[0])
    finally:
        connection.close()


def test_repository_persists_and_queries_by_three_ids(tmp_path) -> None:
    db_path = tmp_path / "shadow-runs.db"
    repository = CreditShadowRunRepository(SQLiteRepository(db_path))
    integration = BusinessCreditShadowIntegration(
        mode="shadow",
        run_repository=repository,
    )
    run = integration.observe_credit_application(
        **input_objects(),
        application_id="CARD-PERSIST-001",
        request_id="REQUEST-PERSIST-001",
    )

    assert run is not None
    assert integration.pending_persistence_count() == 1
    persisted = integration.flush_pending()
    assert len(persisted) == 1
    assert integration.pending_persistence_count() == 0

    reloaded = CreditShadowRunRepository(SQLiteRepository(db_path))
    by_run = reloaded.get(run.shadow_run_id)
    by_identity = reloaded.get_by_identity(
        "CARD-PERSIST-001",
        "REQUEST-PERSIST-001",
    )
    by_application = reloaded.list_by_application("CARD-PERSIST-001")
    by_request = reloaded.list_by_request("REQUEST-PERSIST-001")

    assert by_run is not None
    assert by_identity is not None
    assert by_run.shadow_run_id == run.shadow_run_id
    assert by_identity.shadow_run_id == run.shadow_run_id
    assert len(by_application) == 1
    assert len(by_request) == 1
    assert by_application[0].model_status == "not_called"
    assert by_application[0].rule_impact == "none"
    assert "13800138000" not in json.dumps(
        by_run.to_persistence_payload(),
        ensure_ascii=False,
    )

    restarted_service = Mock()
    restarted_integration = BusinessCreditShadowIntegration(
        mode="shadow",
        service=restarted_service,
        run_repository=reloaded,
    )
    replayed = restarted_integration.observe_credit_application(
        **input_objects(),
        application_id="CARD-PERSIST-001",
        request_id="REQUEST-PERSIST-001",
    )
    assert replayed is not None
    assert replayed.shadow_run_id == run.shadow_run_id
    restarted_service.evaluate_business_shadow.assert_not_called()


def test_database_unique_constraint_handles_cross_integration_competition(
    tmp_path,
) -> None:
    db_path = tmp_path / "shadow-competition.db"
    repository_one = CreditShadowRunRepository(SQLiteRepository(db_path))
    repository_two = CreditShadowRunRepository(SQLiteRepository(db_path))
    integration_one = BusinessCreditShadowIntegration(
        mode="shadow",
        run_repository=repository_one,
    )
    integration_two = BusinessCreditShadowIntegration(
        mode="shadow",
        run_repository=repository_two,
    )
    inputs = input_objects()
    service_barrier = threading.Barrier(2)
    original_one = integration_one._service.evaluate_business_shadow
    original_two = integration_two._service.evaluate_business_shadow

    def service_one(**kwargs):
        service_barrier.wait(timeout=3)
        return original_one(**kwargs)

    def service_two(**kwargs):
        service_barrier.wait(timeout=3)
        return original_two(**kwargs)

    integration_one._service.evaluate_business_shadow = service_one
    integration_two._service.evaluate_business_shadow = service_two
    observed = []
    errors = []

    def observe(integration):
        try:
            observed.append(integration.observe_credit_application(
                **inputs,
                application_id="CARD-COMPETE-001",
                request_id="REQUEST-COMPETE-001",
            ))
        except Exception as exc:
            errors.append(exc)

    observation_threads = [
        threading.Thread(target=observe, args=(integration_one,)),
        threading.Thread(target=observe, args=(integration_two,)),
    ]
    for thread in observation_threads:
        thread.start()
    for thread in observation_threads:
        thread.join(timeout=5)

    assert errors == []
    assert all(not thread.is_alive() for thread in observation_threads)
    first, second = observed

    assert first is not None
    assert second is not None
    assert first.shadow_run_id != second.shadow_run_id
    commit_barrier = threading.Barrier(2)
    persisted = [[], []]

    def flush(integration, target):
        commit_barrier.wait(timeout=3)
        target.extend(integration.flush_pending())

    persistence_threads = [
        threading.Thread(target=flush, args=(integration_one, persisted[0])),
        threading.Thread(target=flush, args=(integration_two, persisted[1])),
    ]
    for thread in persistence_threads:
        thread.start()
    for thread in persistence_threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in persistence_threads)
    assert len(persisted[0]) == 1
    assert len(persisted[1]) == 1
    assert persisted[0][0].shadow_run_id == persisted[1][0].shadow_run_id
    assert shadow_run_count(db_path) == 1
    assert len(repository_one.list_by_application("CARD-COMPETE-001")) == 1


def test_service_persists_after_formal_session_and_restart_query(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("FINAGENT_CREDIT_SHADOW_MODE", "shadow")
    db_path = tmp_path / "service-persist.db"
    service = AgentSessionService(
        make_store(),
        llm_mode="off",
        store_factory=make_store,
        repository=SQLiteRepository(db_path),
    )
    session_id = service.create_session()["session_id"]
    reply, request_id = apply_application(service, session_id)
    application_id = reply["data"]["application_id"]
    runtime_run = service._sessions[session_id].agent.credit_shadow.recent_runs()[0]

    persisted = CreditShadowRunRepository(SQLiteRepository(db_path))
    stored = persisted.get_by_identity(application_id, request_id)
    assert stored is not None
    assert stored.application_id == application_id
    assert stored.request_id == request_id
    assert stored.shadow_run_id == runtime_run.shadow_run_id
    assert service._sessions[session_id].agent.credit_shadow.pending_persistence_count() == 0

    restarted_repository = CreditShadowRunRepository(
        SQLiteRepository(db_path)
    )
    restarted = restarted_repository.get(stored.shadow_run_id)
    assert restarted is not None
    assert restarted.shadow_run_id == stored.shadow_run_id

    replay = service.send_message(session_id, "确认", request_id)
    assert replay["replayed"] is True
    assert shadow_run_count(db_path) == 1


def test_shadow_off_creates_no_persistent_run(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FINAGENT_CREDIT_SHADOW_MODE", "off")
    db_path = tmp_path / "shadow-off.db"
    service = AgentSessionService(
        make_store(),
        llm_mode="off",
        store_factory=make_store,
        repository=SQLiteRepository(db_path),
    )
    session_id = service.create_session()["session_id"]

    reply, _ = apply_application(service, session_id)

    assert reply["data"]["application_id"]
    assert shadow_run_count(db_path) == 0


def test_formal_session_failure_discards_pending_shadow_run(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("FINAGENT_CREDIT_SHADOW_MODE", "shadow")
    db_path = tmp_path / "session-failure.db"
    repository = SQLiteRepository(db_path)
    service = AgentSessionService(
        make_store(),
        llm_mode="off",
        store_factory=make_store,
        repository=repository,
    )
    session_id = service.create_session()["session_id"]
    ensure_uploaded(service, session_id)
    for index, text in enumerate(
        ("我想申请一张工商银行的信用卡", *APPLICATION_STEPS[:-1])
    ):
        service.send_message(session_id, text, f"session-failure-{index}")
    agent = service._sessions[session_id].agent

    with patch.object(
        repository,
        "save_session",
        side_effect=RuntimeError("simulated formal save failure"),
    ):
        with pytest.raises(RuntimeError, match="simulated formal save failure"):
            service.send_message(
                session_id,
                APPLICATION_STEPS[-1],
                "session-failure-final",
            )

    assert shadow_run_count(db_path) == 0
    assert agent.credit_shadow.pending_persistence_count() == 0
    failures = agent.credit_shadow.recent_persistence_failures()
    assert len(failures) == 1
    assert failures[0].reason == "business_session_not_saved"


def test_shadow_persistence_failure_does_not_fail_formal_reply(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("FINAGENT_CREDIT_SHADOW_MODE", "shadow")
    db_path = tmp_path / "shadow-write-failure.db"
    service = AgentSessionService(
        make_store(),
        llm_mode="off",
        store_factory=make_store,
        repository=SQLiteRepository(db_path),
    )
    session_id = service.create_session()["session_id"]
    ensure_uploaded(service, session_id)
    for index, text in enumerate(
        ("我想申请一张工商银行的信用卡", *APPLICATION_STEPS[:-1])
    ):
        service.send_message(session_id, text, f"write-failure-{index}")
    agent = service._sessions[session_id].agent

    with patch.object(
        service._credit_shadow_runs,
        "save",
        side_effect=RuntimeError("simulated shadow write failure"),
    ):
        reply = service.send_message(
            session_id,
            APPLICATION_STEPS[-1],
            "write-failure-final",
        )["reply"]

    assert reply["ok"] is True
    assert reply["data"]["application_id"]
    assert shadow_run_count(db_path) == 0
    failures = agent.credit_shadow.recent_persistence_failures()
    assert len(failures) == 1
    assert failures[0].error_type == "RuntimeError"
    assert failures[0].reason == "shadow_persistence_error"


def test_shadow_service_error_is_persisted_without_formal_failure(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("FINAGENT_CREDIT_SHADOW_MODE", "shadow")
    db_path = tmp_path / "shadow-error.db"
    service = AgentSessionService(
        make_store(),
        llm_mode="off",
        store_factory=make_store,
        repository=SQLiteRepository(db_path),
    )
    session_id = service.create_session()["session_id"]
    agent = service._sessions[session_id].agent

    with patch.object(
        agent.credit_shadow._service,
        "evaluate_business_shadow",
        side_effect=RuntimeError("simulated shadow service error"),
    ):
        reply, request_id = apply_application(service, session_id)

    application_id = reply["data"]["application_id"]
    stored = CreditShadowRunRepository(
        SQLiteRepository(db_path)
    ).get_by_identity(application_id, request_id)
    assert stored is not None
    assert stored.status == "shadow_error"
    assert stored.model_status == "not_called"
    assert stored.rule_impact == "none"
    assert reply["ok"] is True
