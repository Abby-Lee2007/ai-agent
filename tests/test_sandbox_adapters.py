"""Unified bank/payment/broker sandbox lifecycle tests."""
from __future__ import annotations

import unittest

from finagent.integrations.sandbox_adapters import SandboxTransitionError
from tests.support import make_agent, run_to_completion


class TestSandboxAdapters(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def test_payment_transfer_records_full_callback_history(self):
        reply = run_to_completion(
            self.agent,
            self.agent.handle("给王强转1块，用微信"),
        )
        execution_id = reply.data["sandbox_execution_id"]
        execution = self.agent.store.sandbox_executions[execution_id]

        self.assertEqual(execution.provider, "payment")
        self.assertEqual(execution.operation, "transfer")
        self.assertEqual(execution.status, "completed")
        self.assertEqual(
            [item["status"] for item in execution.history],
            ["submitted", "processing", "completed"],
        )
        self.assertTrue(execution.provider_reference)

    def test_broker_subscribe_and_redeem_use_sandbox_orders(self):
        subscribe = self.agent.handle("买1万块稳利债券A")
        self.assertIn("付款银行卡", subscribe.text)
        subscribe = run_to_completion(
            self.agent,
            self.agent.handle("用工资卡"),
        )
        execution = self.agent.store.sandbox_executions[
            subscribe.data["sandbox_execution_id"]
        ]
        self.assertEqual(execution.provider, "broker")
        self.assertEqual(execution.operation, "subscribe")
        self.assertEqual(execution.status, "completed")

        redeem = run_to_completion(
            self.agent,
            self.agent.handle("赎回稳利债券A"),
        )
        self.assertTrue(redeem.data["sandbox_execution_ids"])
        redeem_execution = self.agent.store.sandbox_executions[
            redeem.data["sandbox_execution_ids"][0]
        ]
        self.assertEqual(redeem_execution.provider, "broker")
        self.assertEqual(redeem_execution.operation, "redeem")

    def test_returned_transfer_is_reversed_once(self):
        reply = run_to_completion(
            self.agent,
            self.agent.handle("给王强转1块，用微信"),
        )
        execution_id = reply.data["sandbox_execution_id"]
        txn = self.agent.store.transactions[reply.data["txn_id"]]
        account = self.agent.store.accounts[txn.account_id]
        balance_before = account.balance

        self.agent.sandbox.gateway.callback(
            execution_id,
            status="returned",
            reason="收款渠道退回",
        )

        self.assertEqual(txn.lifecycle_status, "returned")
        self.assertAlmostEqual(account.balance, balance_before + 1.0, places=2)
        self.assertEqual(
            self.agent.store.sandbox_executions[execution_id].status,
            "returned",
        )

        history_length = len(self.agent.store.sandbox_executions[execution_id].history)
        repeated = self.agent.sandbox.gateway.callback(
            execution_id,
            status="returned",
        )
        self.assertEqual(repeated.status, "returned")
        self.assertEqual(len(repeated.history), history_length)
        self.assertAlmostEqual(account.balance, balance_before + 1.0, places=2)

    def test_invalid_transition_is_rejected(self):
        execution = self.agent.sandbox.payment.submit(
            "transfer",
            "TXN-NOT-USED",
            {"amount": 1},
        )
        self.agent.sandbox.gateway.transition(execution.execution_id, "completed")

        with self.assertRaises(SandboxTransitionError):
            self.agent.sandbox.gateway.transition(execution.execution_id, "processing")


if __name__ == "__main__":
    unittest.main()
