"""Regression tests for transaction rollback, idempotency and ledger safety."""
from __future__ import annotations

import unittest
import threading
import tempfile
import zipfile
from pathlib import Path

from finagent.agent.core import AgentReply
from finagent.agent.llm_planner import LLMToolPlanner
from finagent.agent.planner import Plan, PlanStep, Planner
from finagent.agent.registry import build_default_registry
from finagent.api.service import AgentSessionService, StepUpRequired
from finagent.data.card_ledger import card_asset_total
from finagent.data.sqlite import OptimisticConcurrencyError, SQLiteRepository
from finagent.nlu.intent_parser import IntentParser
from finagent.nlu.intent_schema import Intent
from finagent.tools.base import ToolResult
from finagent.vision.excel_reader import ExcelReadError, read_excel
from tests.support import make_agent, make_store, run_to_completion


def confirm_card_update(service, session_id, card_id, values):
    try:
        return service.update_card_settings(session_id, card_id, values)
    except StepUpRequired as exc:
        return service.update_card_settings(
            session_id,
            card_id,
            {
                **values,
                "confirmation_token": exc.challenge["confirmation_token"],
                "payment_password": "246810",
            },
        )


class FakeToolLLM:
    available = True
    last_error = ""

    def __init__(self, name: str, arguments=None):
        self.name = name
        self.arguments = arguments or {}

    def chat_with_tools(self, messages, tools, tool_choice="auto"):
        return {"name": self.name, "arguments": self.arguments}


class FakeIntentLLM:
    available = True
    model = "fake"
    last_error = ""

    def __init__(self, intent: str, slots=None):
        self.intent = intent
        self.slots = slots or {}

    def chat_json(self, messages):
        return {
            "intent": self.intent,
            "slots": self.slots,
            "confidence": 0.99,
        }


class TestTransactionalExecution(unittest.TestCase):
    def test_sandbox_failure_rolls_back_transaction_and_balance(self):
        agent = make_agent()
        card = agent.providers.cards.find("u_001", "工资卡")
        before_balance = card.balance
        before_txns = len(agent.store.transactions)

        def fail_submit(*args, **kwargs):
            raise RuntimeError("sandbox unavailable")

        agent.sandbox.payment.submit = fail_submit
        plan = Plan("transfer.send", [PlanStep(
            "transfer.send",
            {
                "payee": "王强",
                "amount": 100,
                "channel": "wechat",
                "account": "工资卡",
                "payment_password": "246810",
            },
        )])
        reply = agent._execute(plan, agent.context())

        self.assertFalse(reply.ok)
        self.assertAlmostEqual(card.balance, before_balance)
        self.assertEqual(len(agent.store.transactions), before_txns)
        self.assertEqual(agent._workflow_payload(reply)["status"], "failed")
        self.assertTrue(reply.trace[0]["rolled_back"])

    def test_risk_is_rechecked_after_sms_verification_before_execution(self):
        agent = make_agent()
        card = agent.providers.cards.find("u_001", "信用卡")
        original_limit = card.credit_limit
        reply = agent.handle("把信用卡额度提到30000")
        self.assertEqual(reply.kind, "confirm")
        reply = agent.handle("确认")
        self.assertEqual(reply.kind, "verify")

        card.status = "frozen"
        reply = agent.handle("123456")

        self.assertFalse(reply.ok)
        self.assertEqual(
            agent.providers.cards.find("u_001", "信用卡").credit_limit,
            original_limit,
        )
        self.assertEqual(reply.data["workflow"]["status"], "failed")

    def test_multi_step_failure_rolls_back_earlier_step(self):
        agent = make_agent()
        card = agent.providers.cards.find("u_001", "信用卡")

        class FailingLostTool:
            name = "card.report_lost"
            description = "failure"
            params = []
            risk_level = "high"
            requires_confirmation = True

            @staticmethod
            def required_params():
                return []

            @staticmethod
            def run(*args, **kwargs):
                return ToolResult(False, "simulated report-lost failure")

        original_get = agent.registry.get
        agent.registry.get = lambda name: (
            FailingLostTool()
            if name == "card.report_lost" else original_get(name)
        )
        plan = Plan("card.report_lost", [
            PlanStep("card.freeze", {"card": "信用卡"}),
            PlanStep("card.report_lost", {"card": "信用卡"}),
        ])
        reply = agent._execute(plan, agent.context())

        self.assertFalse(reply.ok)
        self.assertEqual(agent.store.cards[card.card_id].status, "active")
        self.assertTrue(all(step["rolled_back"] for step in reply.trace))


class TestSchedulerIdempotency(unittest.TestCase):
    def test_same_scheduled_task_cannot_charge_twice(self):
        agent = make_agent()
        card = agent.providers.cards.find("u_001", "工资卡")
        before = card.balance
        task = agent.store.add_task(
            user_id="u_001",
            kind="scheduled_transfer",
            run_date=agent.clock.date_str(),
            title="幂等测试",
            payload={
                "payee": "林雅雯",
                "amount": 100,
                "channel": "wechat",
                "account": "工资卡",
                "payment_verified": True,
            },
            repeat="once",
        )
        ctx = agent.context()

        first = agent.scheduler._execute(ctx, task)
        second = agent.scheduler._execute(ctx, task)

        self.assertIn("100.00", first)
        self.assertEqual(second, "")
        self.assertAlmostEqual(card.balance, before - 100)
        matching = [
            txn for txn in agent.store.transactions.values()
            if (txn.metadata or {}).get("idempotency_key")
            == f"task:{task.task_id}:{task.run_date}"
        ]
        self.assertEqual(len(matching), 1)

    def test_concurrent_scheduler_calls_do_not_double_charge(self):
        agent = make_agent()
        card = agent.providers.cards.find("u_001", "工资卡")
        before = card.balance
        task = agent.store.add_task(
            user_id="u_001",
            kind="scheduled_transfer",
            run_date=agent.clock.date_str(),
            title="并发测试",
            payload={
                "payee": "林雅雯",
                "amount": 100,
                "channel": "wechat",
                "account": "工资卡",
                "payment_verified": True,
            },
            repeat="once",
        )
        barrier = threading.Barrier(2)
        messages = []

        def execute():
            barrier.wait()
            messages.append(agent.scheduler._execute(agent.context(), task))

        threads = [threading.Thread(target=execute) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        self.assertAlmostEqual(card.balance, before - 100)
        self.assertEqual(sum(1 for message in messages if message), 1)

    def test_gift_settlement_rejects_frozen_card_without_partial_charge(self):
        agent = make_agent()
        created = run_to_completion(agent, agent.handle("锁定2000块做应急金"))
        self.assertTrue(created.ok)
        goal = agent.store.goals_of("u_001", "active")[0]
        account = agent.store.accounts.get(goal.account_id)
        card = next(
            card for card in agent.store.cards_of("u_001")
            if card.account_id == account.account_id
        )
        card.status = "frozen"
        before_balance = account.balance
        before_locked = account.locked
        task = agent.store.add_task(
            user_id="u_001",
            kind="gift_order",
            run_date=agent.clock.date_str(),
            title="生日礼物",
            payload={
                "goal_name": goal.name,
                "budget": 500,
                "person": "林雅雯",
                "item": "鲜花",
                "merchant": "测试花店",
            },
            repeat="once",
        )

        message = agent.scheduler._execute(agent.context(), task)

        self.assertIn("失败", message)
        self.assertAlmostEqual(account.balance, before_balance)
        self.assertAlmostEqual(account.locked, before_locked)
        self.assertEqual(agent.store.tasks[task.task_id].status, "failed")


class TestRequestIdempotency(unittest.TestCase):
    def test_stale_service_cannot_overwrite_newer_session_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent.db"
            first = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            session_id = first.create_session()["session_id"]
            second = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            second.get_profile_settings(session_id)

            first.update_profile_settings(session_id, {"name": "最新用户"})
            with self.assertRaises(OptimisticConcurrencyError):
                second.update_profile_settings(session_id, {"name": "旧版本用户"})

            restored = first.get_profile_settings(session_id)
            stale_instance_reloaded = second.get_profile_settings(session_id)
            self.assertEqual(restored["user"]["name"], "最新用户")
            self.assertEqual(
                stale_instance_reloaded["user"]["name"],
                "最新用户",
            )

    def test_request_id_cannot_be_reused_for_different_message(self):
        service = AgentSessionService(make_store(), llm_mode="off")
        session_id = service.create_session()["session_id"]
        first = service.send_message(session_id, "我有哪些卡", "same-request")
        self.assertTrue(first["reply"]["ok"])

        with self.assertRaises(ValueError):
            service.send_message(session_id, "我还有多少钱", "same-request")

    def test_concurrent_payment_auth_with_same_request_id_charges_once(self):
        service = AgentSessionService(
            make_store(),
            llm_mode="off",
            store_factory=make_store,
        )
        session_id = service.create_session()["session_id"]
        agent = service._sessions[session_id].agent
        card = agent.providers.cards.find("u_001", "工资卡")
        before = card.balance
        service.send_message(session_id, "给王强转200块，用微信", "transfer-init")
        service.send_message(session_id, "确认", "transfer-confirm")

        barrier = threading.Barrier(2)
        payloads = []

        def submit_password():
            barrier.wait()
            payloads.append(
                service.send_message(session_id, "246810", "transfer-password")
            )

        threads = [threading.Thread(target=submit_password) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        self.assertAlmostEqual(card.balance, before - 200)
        self.assertEqual(sum(1 for payload in payloads if payload["replayed"]), 1)

    def test_completed_request_replays_after_restart_without_aux_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent.db"
            first = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            session_id = first.create_session()["session_id"]
            first.send_message(
                session_id,
                "给王强转200块，用微信",
                "atomic-replay-init",
            )
            first.send_message(session_id, "确认", "atomic-replay-confirm")
            completed = first.send_message(
                session_id,
                "246810",
                "atomic-replay-password",
            )
            card = first._sessions[session_id].agent.providers.cards.find(
                "u_001",
                "工资卡",
            )
            balance_after = card.balance
            txn_count = len(first._sessions[session_id].agent.store.transactions)

            second = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            second.repository.load_idempotency = lambda *args: None
            replayed = second.send_message(
                session_id,
                "246810",
                "atomic-replay-password",
            )
            restored = second._sessions[session_id].agent
            restored_card = restored.providers.cards.find("u_001", "工资卡")

        self.assertTrue(completed["reply"]["ok"])
        self.assertTrue(replayed["replayed"])
        self.assertAlmostEqual(restored_card.balance, balance_after)
        self.assertEqual(len(restored.store.transactions), txn_count)

    def test_service_background_scan_runs_due_task_once(self):
        service = AgentSessionService(
            make_store(),
            llm_mode="off",
            store_factory=make_store,
        )
        session_id = service.create_session()["session_id"]
        agent = service._sessions[session_id].agent
        card = agent.providers.cards.find("u_001", "工资卡")
        before = card.balance
        agent.store.add_task(
            user_id="u_001",
            kind="scheduled_transfer",
            run_date=agent.clock.date_str(),
            title="后台任务",
            payload={
                "payee": "林雅雯",
                "amount": 100,
                "channel": "wechat",
                "account": "工资卡",
                "payment_verified": True,
            },
            repeat="once",
        )

        first = service.run_due_tasks()
        second = service.run_due_tasks()

        self.assertIn(session_id, first)
        self.assertEqual(second, {})
        self.assertAlmostEqual(card.balance, before - 100)

    def test_background_scan_restores_unopened_sessions_after_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent.db"
            first = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            session_id = first.create_session()["session_id"]
            agent = first._sessions[session_id].agent
            card = agent.providers.cards.find("u_001", "工资卡")
            before = card.balance
            agent.store.add_task(
                user_id="u_001",
                kind="scheduled_transfer",
                run_date=agent.clock.date_str(),
                title="重启后的定时转账",
                payload={
                    "payee": "林雅雯",
                    "amount": 100,
                    "channel": "wechat",
                    "account": "工资卡",
                    "payment_verified": True,
                },
                repeat="once",
            )
            first._persist_session(first._sessions[session_id])

            second = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            results = second.run_due_tasks()
            restored = second._sessions[session_id].agent
            restored_card = restored.providers.cards.find("u_001", "工资卡")
            second_scan = second.run_due_tasks()

        self.assertIn(session_id, results)
        self.assertAlmostEqual(restored_card.balance, before - 100)
        self.assertEqual(second_scan, {})

    def test_pending_confirmation_survives_restart_without_payment_secret(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent.db"
            repository = SQLiteRepository(db_path)
            first = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=repository,
            )
            session_id = first.create_session()["session_id"]
            pending = first.send_message(
                session_id,
                "给王强转200块，用微信",
                "pending-restart-init",
            )
            self.assertEqual(pending["reply"]["kind"], "confirm")

            second = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            auth = second.send_message(session_id, "确认", "pending-restart-confirm")
            self.assertEqual(auth["reply"]["kind"], "question")
            done = second.send_message(session_id, "246810", "pending-restart-password")

            self.assertTrue(done["reply"]["ok"])
            agent = second._sessions[session_id].agent
            self.assertTrue(any(
                txn.merchant == "王强" and txn.amount == -200
                for txn in agent.store.transactions.values()
            ))


class TestLedgerSettingsSafety(unittest.TestCase):
    def test_sensitive_card_changes_require_unmodified_single_use_confirmation(self):
        service = AgentSessionService(make_store(), llm_mode="off")
        session_id = service.create_session()["session_id"]
        agent = service._sessions[session_id].agent
        card = agent.providers.cards.find("u_001", "信用卡")
        original_online = card.online_enabled

        with self.assertRaises(StepUpRequired) as raised:
            service.update_card_settings(
                session_id,
                card.card_id,
                {"online_enabled": not original_online},
            )
        challenge = raised.exception.challenge
        self.assertEqual(card.online_enabled, original_online)

        with self.assertRaises(ValueError):
            service.update_card_settings(
                session_id,
                card.card_id,
                {
                    "credit_limit": 60000,
                    "confirmation_token": challenge["confirmation_token"],
                    "payment_password": "246810",
                },
            )
        self.assertEqual(card.online_enabled, original_online)

        with self.assertRaises(StepUpRequired) as retried:
            service.update_card_settings(
                session_id,
                card.card_id,
                {"credit_limit": 60000},
            )
        confirmed = service.update_card_settings(
            session_id,
            card.card_id,
            {
                "credit_limit": 60000,
                "confirmation_token": retried.exception.challenge["confirmation_token"],
                "payment_password": "246810",
            },
        )
        self.assertEqual(confirmed["credit_limit"], 60000)

        with self.assertRaises(ValueError):
            service.update_card_settings(
                session_id,
                card.card_id,
                {
                    "credit_limit": 65000,
                    "confirmation_token": retried.exception.challenge["confirmation_token"],
                    "payment_password": "246810",
                },
            )

    def test_card_step_up_is_bound_to_session_and_rejects_wrong_password(self):
        service = AgentSessionService(make_store(), llm_mode="off")
        first_session = service.create_session()["session_id"]
        second_session = service.create_session()["session_id"]
        first_agent = service._sessions[first_session].agent
        second_agent = service._sessions[second_session].agent
        first_card = first_agent.providers.cards.find("u_001", "信用卡")
        second_card = second_agent.providers.cards.find("u_001", "信用卡")

        with self.assertRaises(StepUpRequired) as raised:
            service.update_card_settings(
                first_session,
                first_card.card_id,
                {"status": "frozen"},
            )
        token = raised.exception.challenge["confirmation_token"]

        with self.assertRaises(ValueError):
            service.update_card_settings(
                second_session,
                second_card.card_id,
                {
                    "status": "frozen",
                    "confirmation_token": token,
                    "payment_password": "246810",
                },
            )
        self.assertEqual(second_card.status, "active")

        with self.assertRaises(StepUpRequired) as retried:
            service.update_card_settings(
                first_session,
                first_card.card_id,
                {"status": "frozen"},
            )
        with self.assertRaises(ValueError) as wrong_password:
            service.update_card_settings(
                first_session,
                first_card.card_id,
                {
                    "status": "frozen",
                    "confirmation_token": retried.exception.challenge["confirmation_token"],
                    "payment_password": "000000",
                },
            )
        self.assertIn("交易密码验证失败", str(wrong_password.exception))
        self.assertEqual(first_card.status, "active")

    def test_credit_limit_cannot_be_lowered_below_current_usage(self):
        service = AgentSessionService(make_store(), llm_mode="off")
        session_id = service.create_session()["session_id"]
        card = service._sessions[session_id].agent.providers.cards.find(
            "u_001",
            "信用卡",
        )
        with self.assertRaises(ValueError):
            service.update_card_settings(
                session_id,
                card.card_id,
                {"credit_limit": max(card.used - 1, 0)},
            )
        self.assertGreaterEqual(card.credit_limit, card.used)

    def test_card_limit_fields_are_scoped_to_the_correct_card_type(self):
        service = AgentSessionService(make_store(), llm_mode="off")
        session_id = service.create_session()["session_id"]
        agent = service._sessions[session_id].agent
        debit = agent.providers.cards.find("u_001", "工资卡")
        credit = agent.providers.cards.find("u_001", "信用卡")

        with self.assertRaises(ValueError):
            service.update_card_settings(
                session_id,
                debit.card_id,
                {"credit_limit": 10000},
            )
        with self.assertRaises(ValueError):
            service.update_card_settings(
                session_id,
                credit.card_id,
                {"daily_limit": 10000},
            )

    def test_new_card_gets_its_own_zero_balance_account(self):
        service = AgentSessionService(
            make_store(),
            llm_mode="off",
            store_factory=make_store,
        )
        session_id = service.create_session()["session_id"]
        agent = service._sessions[session_id].agent
        existing_accounts = set(agent.store.accounts)

        created = service.create_card_settings(session_id, {
            "kind": "virtual",
            "brand": "测试银行",
            "tail": "4444",
        })

        account = agent.store.accounts.get(
            agent.store.cards[created["card_id"]].account_id
        )
        self.assertIsNotNone(account)
        self.assertNotIn(account.account_id, existing_accounts)
        self.assertAlmostEqual(account.balance, 0.0)

    def test_profile_card_creation_cannot_mint_balance_or_debt(self):
        service = AgentSessionService(
            make_store(),
            llm_mode="off",
            store_factory=make_store,
        )
        session_id = service.create_session()["session_id"]
        created = service.create_card_settings(session_id, {
            "kind": "debit",
            "brand": "测试银行",
            "tail": "5555",
            "balance": 999999,
            "used": 999999,
        })
        self.assertAlmostEqual(created["balance"], 0.0)
        self.assertAlmostEqual(created["used"], 0.0)

    def test_profile_test_cards_cannot_bypass_activation_or_limits(self):
        service = AgentSessionService(
            make_store(),
            llm_mode="off",
            store_factory=make_store,
        )
        session_id = service.create_session()["session_id"]

        created = service.create_card_settings(session_id, {
            "kind": "debit",
            "brand": "测试银行",
            "tail": "4444",
        })
        self.assertEqual(created["status"], "active")
        self.assertTrue(created["is_demo"])
        self.assertEqual(created["source"], "user:profile:test")

        with self.assertRaises(ValueError):
            service.create_card_settings(session_id, {
                "kind": "credit",
                "tail": "5555",
                "credit_limit": 999999,
            })
        clamped = service.create_card_settings(session_id, {
            "kind": "debit",
            "tail": "6666",
            "daily_limit": 1000,
            "single_limit": 2000,
        })
        self.assertEqual(clamped["daily_limit"], 1000)
        self.assertEqual(clamped["single_limit"], 1000)

    def test_card_settings_keep_account_projection_consistent(self):
        service = AgentSessionService(
            make_store(),
            llm_mode="off",
            store_factory=make_store,
        )
        session_id = service.create_session()["session_id"]
        agent = service._sessions[session_id].agent
        card = next(
            item for item in agent.store.cards_of("u_001")
            if item.kind == "credit"
        )
        account = agent.store.accounts.get(card.account_id)

        confirm_card_update(
            service,
            session_id,
            card.card_id,
            {"credit_limit": 12345},
        )

        self.assertAlmostEqual(card.used, 12340)
        self.assertAlmostEqual(card.credit_limit, 12345)
        self.assertAlmostEqual(account.balance, 12340)
        self.assertAlmostEqual(account.credit_limit, 12345)

        with self.assertRaises(ValueError):
            service.update_card_settings(
                session_id,
                card.card_id,
                {"used": 9999},
            )

    def test_profile_settings_cannot_unfreeze_card_without_risk_flow(self):
        service = AgentSessionService(
            make_store(),
            llm_mode="off",
            store_factory=make_store,
        )
        session_id = service.create_session()["session_id"]
        agent = service._sessions[session_id].agent
        card = agent.providers.cards.find("u_001", "信用卡")
        card.status = "frozen"

        with self.assertRaises(ValueError):
            service.update_card_settings(
                session_id,
                card.card_id,
                {"status": "active"},
            )

    def test_nonzero_balance_card_cannot_be_deleted(self):
        service = AgentSessionService(
            make_store(),
            llm_mode="off",
            store_factory=make_store,
        )
        session_id = service.create_session()["session_id"]
        agent = service._sessions[session_id].agent
        card = agent.providers.cards.find("u_001", "工资卡")
        before = card_asset_total(agent.store, "u_001")

        with self.assertRaises(ValueError):
            service.delete_card_settings(session_id, card.card_id)

        self.assertAlmostEqual(card_asset_total(agent.store, "u_001"), before)
        self.assertIn(card.card_id, agent.store.cards)

    def test_deleting_goal_releases_account_and_card_lock(self):
        service = AgentSessionService(
            make_store(),
            llm_mode="off",
            store_factory=make_store,
        )
        session_id = service.create_session()["session_id"]
        service.send_message(session_id, "锁定2000块做应急金", "goal-create-1")
        service.send_message(session_id, "确认", "goal-create-2")
        agent = service._sessions[session_id].agent
        goal = agent.store.goals_of("u_001", "active")[0]
        account = agent.store.accounts.get(goal.account_id)
        card = next(
            card for card in agent.store.cards_of("u_001")
            if card.account_id == account.account_id
        )
        self.assertAlmostEqual(account.locked, 2000)
        self.assertAlmostEqual(card.locked, 2000)

        service.delete_goal_settings(session_id, goal.goal_id)

        self.assertAlmostEqual(account.locked, 0)
        self.assertAlmostEqual(card.locked, 0)

    def test_card_number_phone_and_labeled_password_are_redacted_in_history(self):
        agent = make_agent()
        agent.handle(
            "绑定一张中国银行的储蓄卡，"
            "卡号6222020000000007，取款密码123456，手机号13800138000"
        )
        retained = agent.session.history[0]["content"]
        self.assertNotIn("6222020000000007", retained)
        self.assertNotIn("13800138000", retained)
        self.assertNotIn("123456", retained)
        self.assertIn("****", retained)

    def test_pending_bind_state_with_card_number_is_not_persisted(self):
        agent = make_agent()
        agent.session.pending = type("PendingLike", (), {
            "intent": "card.bind",
            "slots": {"card_no": "6222020000000007", "phone": "13800138000"},
            "plan": None,
        })()
        exported = agent.export_pending()
        self.assertFalse(exported["restorable"])
        self.assertNotIn("6222020000000007", str(exported))
        self.assertNotIn("13800138000", str(exported))

    def test_excel_zip_bomb_is_rejected_before_decompression(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bomb.xlsx"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    "xl/sharedStrings.xml",
                    b"0" * (13 * 1024 * 1024),
                )
            with self.assertRaises(ExcelReadError) as raised:
                read_excel(path)
            self.assertIn("拒绝解析", str(raised.exception))


class TestModelBoundary(unittest.TestCase):
    def test_model_cannot_add_sensitive_optional_arguments(self):
        registry = build_default_registry()
        llm = FakeToolLLM(
            "transfer__split_aa",
            {"channel": "bank", "note": "safe note"},
        )
        planner = LLMToolPlanner(registry, llm)
        proposal = planner.propose(
            "transfer.split_aa",
            [("transfer.split_aa", {
                "total": "total",
                "participants": "participants",
                "channel": "channel",
                "note": "note",
            })],
            {"total": 300, "participants": ["我", "王强"]},
            "AA 300",
        )
        self.assertIsNotNone(proposal)
        self.assertNotIn("channel", proposal["args"])
        self.assertEqual(proposal["args"]["note"], "safe note")

    def test_multi_step_plan_is_not_replaced_by_model(self):
        registry = build_default_registry()
        llm = FakeToolLLM("card__freeze", {})
        planner = Planner(registry, LLMToolPlanner(registry, llm))
        plan = planner.plan(
            Intent(
                "card.report_lost",
                {"card": "信用卡"},
                confidence=0.95,
                source="rule",
            ),
            make_agent().context(),
        )
        self.assertEqual(plan.tools(), ["card.freeze", "card.report_lost"])

    def test_llm_cannot_override_confident_rule_intent(self):
        parser = IntentParser(
            store=make_store(),
            user_id="u_001",
            llm=FakeIntentLLM(
                "card.freeze",
                {"card": "信用卡"},
            ),
            mode="on",
        )
        intent = parser.parse("给老婆转2000块，用微信")
        self.assertEqual(intent.name, "transfer.send")
        self.assertIn("冲突", parser.last_source)

    def test_llm_cannot_invent_financial_slots_for_unknown_text(self):
        parser = IntentParser(
            store=make_store(),
            user_id="u_001",
            llm=FakeIntentLLM(
                "transfer.send",
                {
                    "payee": "陌生人",
                    "amount": 9999,
                    "account": "尾号6222",
                },
            ),
            mode="on",
        )
        intent = parser.parse("帮我处理一下这件事")
        self.assertEqual(intent.name, "transfer.send")
        self.assertNotIn("payee", intent.slots)
        self.assertNotIn("amount", intent.slots)
        self.assertNotIn("account", intent.slots)

    def test_workflow_failure_is_not_marked_completed(self):
        agent = make_agent()
        status = agent._workflow_payload(
            AgentReply("failed", ok=False, kind="executed")
        )["status"]
        self.assertEqual(status, "failed")


if __name__ == "__main__":
    unittest.main()
