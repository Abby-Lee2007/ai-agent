"""Digital twin, proactive advice and advanced risk analytics."""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from tests.support import make_agent
from finagent.vision.receipt_reader import ReceiptReader


class TestCashflowDigitalTwin(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def test_forecast_builds_daily_series_and_events(self):
        reply = self.agent.handle("预测我未来90天的现金流")

        self.assertEqual(reply.intent, "forecast.cashflow")
        self.assertTrue(reply.ok)
        forecast = reply.data["forecast"]
        self.assertEqual(forecast["horizon_days"], 90)
        self.assertEqual(len(forecast["series"]), 91)
        self.assertGreater(forecast["start_balance"], 0)
        self.assertTrue(forecast["events"])
        self.assertIn("confidence", forecast)
        self.assertIn("cashflow_breakdown", forecast)
        self.assertGreaterEqual(len(forecast["action_plan"]), 1)
        self.assertEqual(len(forecast["stress_tests"]), 3)
        self.assertIn("daily_safe_budget", forecast)
        self.assertGreaterEqual(
            len(forecast["cashflow_breakdown"]),
            7,
        )
        self.assertEqual(
            len(forecast["baseline_series"]),
            len(forecast["series"]),
        )

    def test_forecast_includes_pending_gift_budget(self):
        account = self.agent.store.primary_account("u_001")
        self.agent.store.add_task(
            user_id="u_001",
            kind="gift_order",
            run_date=(self.agent.clock.today() + timedelta(days=8)).isoformat(),
            title="生日蛋糕预约",
            payload={"budget": 328.0, "person": "林雅雯", "item": "蛋糕"},
        )

        reply = self.agent.handle("预测我未来90天的现金流")
        gift_events = [
            event for event in reply.data["forecast"]["events"]
            if event["kind"] == "scheduled_task"
            and "生日蛋糕预约" in event["label"]
        ]

        self.assertTrue(gift_events)
        self.assertEqual(gift_events[0]["amount"], -328.0)
        self.assertGreaterEqual(
            reply.data["forecast"]["cashflow_breakdown"]["scheduled_tasks"],
            328.0,
        )

    def test_forecast_uses_uploaded_bill_transactions(self):
        account = self.agent.store.accounts_of("u_001")[0]
        self.agent.store.add_transaction(
            user_id="u_001",
            account_id=account.account_id,
            amount=-188.0,
            merchant="上传咖啡商户",
            category="餐饮",
            timestamp="2026-10-10T09:00:00",
            tags=["receipt_import"],
            source="upload:user",
            is_demo=False,
        )
        reply = self.agent.handle("预测我未来90天的现金流")

        forecast = reply.data["forecast"]
        self.assertTrue(forecast["uses_uploaded_bills"])
        self.assertGreaterEqual(forecast["uploaded_transaction_count"], 1)
        self.assertIn("已导入账单", forecast["data_scope"])

    def test_purchase_scenario_reduces_projection(self):
        baseline = self.agent.handle("预测我未来90天的现金流").data["forecast"]
        scenario = self.agent.handle("如果下个月买8000元电脑会怎样")

        self.assertEqual(scenario.intent, "forecast.cashflow")
        projected = scenario.data["forecast"]
        self.assertLess(projected["end_balance"], baseline["end_balance"])
        self.assertGreaterEqual(
            baseline["end_balance"] - projected["end_balance"],
            7000,
        )

    def test_income_drop_scenario_returns_synced_parameters_and_deltas(self):
        baseline = self.agent.handle("预测我未来90天的现金流").data["forecast"]
        scenario = self.agent.handle("收入下降20%后未来90天现金流会怎样")
        projected = scenario.data["forecast"]

        self.assertEqual(projected["scenario"]["income_change_pct"], -20.0)
        self.assertFalse(projected["scenario"]["is_baseline"])
        self.assertLess(projected["end_balance"], baseline["end_balance"])
        self.assertLess(projected["scenario_delta_end"], 0)
        self.assertLess(projected["scenario_delta_min"], 0)
        self.assertIn("当前情景", scenario.text)
        self.assertIn("收入变化 -20%", scenario.text)

    def test_workflow_metadata_is_attached(self):
        reply = self.agent.handle("预测我未来90天的现金流")

        workflow = reply.data["workflow"]
        self.assertTrue(workflow["workflow_id"].startswith("WF-"))
        self.assertEqual(workflow["status"], "completed")
        self.assertIn("advisory", workflow["agents"])
        self.assertEqual(workflow["steps"][0]["tool"], "forecast.cashflow")

    def test_workflow_id_is_stable_across_confirmation_turns(self):
        first = self.agent.handle("冻结卡片")
        first_id = first.data["workflow"]["workflow_id"]
        second = self.agent.handle("信用卡尾号8877")

        self.assertEqual(second.kind, "confirm")
        self.assertEqual(second.data["workflow"]["workflow_id"], first_id)
        self.assertEqual(
            second.data["workflow"]["status"],
            "waiting_confirmation",
        )


class TestProactiveAndPlaybook(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def test_personal_cfo_brief_collects_alerts(self):
        reply = self.agent.handle("主动财务管家")

        self.assertEqual(reply.intent, "advisor.proactive")
        self.assertTrue(reply.ok)
        self.assertIn("brief", reply.data)
        self.assertGreaterEqual(len(reply.data["brief"]), 1)
        self.assertIn("未来30天", reply.text)

    def test_financial_health_score_is_explainable(self):
        reply = self.agent.handle("我的财务健康分")

        self.assertEqual(reply.intent, "advisor.health_score")
        health = reply.data["health"]
        self.assertGreaterEqual(health["score"], 0)
        self.assertLessEqual(health["score"], 100)
        self.assertEqual(len(health["dimensions"]), 7)
        self.assertEqual(len(health["recommendations"]), 3)
        self.assertIn("methodology", health)
        self.assertIn("limitations", health["methodology"])
        for item in health["dimensions"]:
            self.assertIn("benchmark", item)
            self.assertIn("standard", item)
            self.assertIn("current", item)
        for item in health["recommendations"]:
            self.assertIn("steps", item)
            self.assertIn("target", item)
            self.assertGreaterEqual(item["expected_score_gain"], 0)

    def test_event_center_returns_actionable_events(self):
        reply = self.agent.handle("我的主动事件")

        self.assertEqual(reply.intent, "advisor.events")
        self.assertIn("events", reply.data)
        self.assertGreaterEqual(len(reply.data["events"]), 1)
        self.assertIn("counts", reply.data)
        for event in reply.data["events"]:
            self.assertIn("event_id", event)
            self.assertIn("action_prompt", event)
        ids = [event["event_id"] for event in reply.data["events"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_event_snooze_and_ignore_are_persisted_in_profile(self):
        tool = self.agent.registry.get("advisor.events")
        ctx = self.agent.context()
        listed = tool.run(ctx, action="list")
        first = listed.data["events"][0]

        tool.run(ctx, action="snooze", event_id=first["event_id"])
        state = self.agent.store.get_user("u_001").preferences["financial_event_state"]
        self.assertIn(first["event_id"], state)
        self.assertTrue(state[first["event_id"]]["snooze_until"])

        visible = tool.run(ctx, action="list")
        self.assertNotIn(
            first["event_id"],
            {event["event_id"] for event in visible.data["events"]},
        )
        self.assertGreaterEqual(visible.data["counts"]["snoozed"], 1)

        second = visible.data["events"][0]
        tool.run(ctx, action="ignore", event_id=second["event_id"])
        state = self.agent.store.get_user("u_001").preferences["financial_event_state"]
        self.assertTrue(state[second["event_id"]]["ignored"])

    def test_risk_playbook_has_confirmed_steps(self):
        reply = self.agent.handle("给我最近异常交易的风险处置方案")

        self.assertEqual(reply.intent, "risk.playbook")
        self.assertEqual(len(reply.data["playbook"]), 5)
        stages = [item["stage"] for item in reply.data["playbook"]]
        self.assertEqual(
            stages,
            ["contain", "verify", "dispute", "replace", "monitor"],
        )
        self.assertTrue(reply.data["playbook"][0]["needs_confirmation"])
        commands = {
            item["stage"]: item["command"]
            for item in reply.data["playbook"]
        }
        self.assertTrue(commands["verify"].startswith("搜索消费记录 "))
        self.assertIn("TXN", commands["dispute"])

    def test_dispute_action_is_executable_and_does_not_reopen_playbook(self):
        playbook = self.agent.handle("给我最近异常交易的风险处置方案")
        dispute_step = next(
            item
            for item in playbook.data["playbook"]
            if item["stage"] == "dispute"
        )

        confirmation = self.agent.handle(dispute_step["command"])

        self.assertEqual(confirmation.intent, "risk.dispute")
        self.assertEqual(confirmation.kind, "confirm")
        self.assertIn("模拟审核", confirmation.text)

        executed = self.agent.handle("确认")

        self.assertTrue(executed.ok)
        self.assertEqual(executed.intent, "risk.dispute")
        self.assertIn("模拟争议申请已提交", executed.text)
        self.assertTrue(executed.data["dispute"]["dispute_id"].startswith("ALERT"))
        alerts = list(self.agent.store.alerts.values())
        self.assertTrue(any(
            alert.source == "risk.dispute"
            for alert in alerts
        ))


class TestAdvancedAnalytics(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def test_account_reconciliation_is_clean(self):
        reply = self.agent.handle("帮我做账务对账")

        self.assertEqual(reply.intent, "account.reconcile")
        self.assertTrue(reply.ok)
        self.assertEqual(reply.data["issues"], [])
        self.assertAlmostEqual(
            reply.data["asset_total"],
            reply.data["expected_asset_total"],
            places=2,
        )
        self.assertIn("手工修改", reply.data["purpose"])
        self.assertIn("manual_fix_supported", reply.data)

    def test_account_reconciliation_can_apply_confirmed_manual_fix(self):
        card = self.agent.store.find_card("u_001", "工资卡")
        account = self.agent.store.accounts[card.account_id]
        card.balance += 123.45

        check = self.agent.handle("帮我做账务对账")
        self.assertTrue(check.ok)
        self.assertTrue(check.data["issue_details"])
        repair = check.data["issue_details"][0]["repair_commands"][1]
        confirmation = self.agent.handle(repair)

        self.assertEqual(confirmation.intent, "account.reconcile_adjust")
        self.assertEqual(confirmation.kind, "confirm")

        executed = self.agent.handle("确认")
        self.assertTrue(executed.ok)
        self.assertAlmostEqual(card.balance, account.balance, places=2)
        self.assertTrue(any(
            item["event"] == "account.reconcile_adjusted"
            for item in self.agent.store.audit
        ))

    def test_wealth_analytics_returns_drawdown_and_correlation(self):
        self.agent.handle("推荐几个稳健的理财产品")
        reply = self.agent.handle("分析这些理财产品的风险")

        self.assertEqual(reply.intent, "wealth.analytics")
        analytics = reply.data["analytics"]
        self.assertGreater(analytics["product_count"], 0)
        self.assertGreater(analytics["average_volatility_pct"], 0)
        self.assertGreater(len(analytics["correlation_matrix"]), 1)

    def test_aa_status_lists_share_payload(self):
        self.agent.handle("昨晚聚餐960，我、王强、李思琪AA")
        self.agent.handle("确认")
        reply = self.agent.handle("AA收款进度")

        self.assertEqual(reply.intent, "transfer.aa_status")
        self.assertEqual(len(reply.data["sheets"]), 1)
        self.assertIn("模拟收款邀请", reply.data["sheets"][0]["share_text"])

    def test_receipt_quality_and_confidence_are_reported(self):
        result = ReceiptReader().read_text(
            "2026-10-01 星巴克 -32.00\n2026-10-02 地铁 -6.00",
            date(2026, 10, 28),
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.quality["record_count"], 2)
        self.assertGreaterEqual(result.quality["score"], 60)
        self.assertEqual(len(result.validation["issues"]), 0)
        self.assertIn("confidence", result.records[0].to_payload())

    def test_receipt_amount_conservation_flags_mismatched_total(self):
        result = ReceiptReader().read_text(
            "2026-10-01 星巴克 -32.00\n"
            "2026-10-02 地铁 -6.00\n"
            "合计支出 100.00",
            date(2026, 10, 28),
        )

        self.assertTrue(result.ok)
        self.assertFalse(result.validation["ok"])
        self.assertTrue(any("相差" in issue for issue in result.validation["issues"]))


if __name__ == "__main__":
    unittest.main()
