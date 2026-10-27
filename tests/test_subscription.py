"""场景五：订阅 / 代扣管理（自动识别 / 续费提醒 / 一键取消）。"""
from __future__ import annotations

import unittest

from tests.support import make_agent, run_to_completion


class TestSubscription(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def test_detect_only_real_subscriptions(self):
        reply = self.agent.handle("有哪些自动续费")
        self.assertEqual(reply.intent, "subscription.detect")
        merchants = {item["merchant"] for item in reply.data["items"]}
        expected = {"腾讯视频VIP", "QQ音乐绿钻", "iCloud+ 云存储",
                    "美团外卖会员", "Keep会员", "百度网盘超级会员"}
        self.assertEqual(merchants, expected)
        # 25 + 15 + 21(已涨价) + 15 + 19 + 25
        self.assertAlmostEqual(reply.data["monthly_total"], 120.0, places=2)

    def test_detect_ignores_irregular_spending(self):
        reply = self.agent.handle("有哪些自动续费")
        merchants = {item["merchant"] for item in reply.data["items"]}
        for noise in ("中石化加油站", "山姆会员店", "淘宝", "中国移动话费", "猫眼电影"):
            self.assertNotIn(noise, merchants)

    def test_detect_flags_price_hike(self):
        reply = self.agent.handle("有哪些自动续费")
        hiked = {item["merchant"] for item in reply.data["items"] if item["hiked"]}
        self.assertIn("iCloud+ 云存储", hiked)

    def test_detect_shows_expiry_and_auto_renew_status(self):
        reply = self.agent.handle("帮我看看有哪些自动续费")
        self.assertIn("到期时间", reply.text)
        rows = {item["merchant"]: item for item in reply.data["items"]}
        self.assertEqual(rows["腾讯视频VIP"]["expires_at"], "2026-11-08")
        self.assertTrue(rows["腾讯视频VIP"]["auto_renew"])

    def test_list_active_subscriptions(self):
        reply = self.agent.handle("我的订阅都有什么")
        self.assertEqual(reply.intent, "subscription.list")
        self.assertEqual(reply.data["count"], 6)

    def test_list_shows_expiry_and_auto_renew_status(self):
        reply = self.agent.handle("我的订阅都有什么")
        self.assertIn("到期时间", reply.text)
        self.assertIn("自动续费", reply.text)
        rows = {item["merchant"]: item for item in reply.data["items"]}
        self.assertEqual(rows["腾讯视频VIP"]["expires_at"], "2026-11-08")
        self.assertTrue(rows["腾讯视频VIP"]["auto_renew"])
        self.assertEqual(rows["腾讯视频VIP"]["category"], "视频会员")
        self.assertEqual(rows["腾讯视频VIP"]["last_charge_date"], "2026-10-08")
        self.assertEqual(rows["腾讯视频VIP"]["cycle"], "monthly")
        self.assertTrue(rows["iCloud+ 云存储"]["hiked"])

    def test_remind_upcoming_charges(self):
        reply = self.agent.handle("最近有哪些订阅要扣费")
        self.assertEqual(reply.intent, "subscription.remind")
        self.assertGreaterEqual(reply.data["count"], 1)
        self.assertTrue(self.agent.store.tasks_of("u_001", status="pending"))

    def test_cancel_removes_related_reminder_from_task_list(self):
        self.agent.handle("最近有哪些订阅要扣费")
        before = self.agent.handle("我的计划")
        self.assertIn("Keep会员", before.text)
        run_to_completion(self.agent, self.agent.handle("取消Keep会员"))
        after = self.agent.handle("我的计划")
        self.assertNotIn("Keep会员", after.text)
        cancelled = self.agent.store.tasks_of("u_001", status="cancelled")
        self.assertEqual(
            {task.kind for task in cancelled},
            {"reminder"},
        )

    def test_cancel_subscription(self):
        reply = self.agent.handle("取消腾讯视频会员")
        self.assertEqual(reply.kind, "confirm")
        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok)
        self.assertEqual(reply.data["merchant"], "腾讯视频VIP")
        active = self.agent.store.subscriptions_of("u_001", status="active")
        self.assertNotIn("腾讯视频VIP", {s.merchant for s in active})

    def test_list_keeps_cancelled_subscription_with_auto_renew_off(self):
        run_to_completion(self.agent, self.agent.handle("取消腾讯视频会员"))
        reply = self.agent.handle("我的订阅都有什么")
        rows = {item["merchant"]: item for item in reply.data["items"]}
        cancelled = rows["腾讯视频VIP"]
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse(cancelled["auto_renew"])
        self.assertEqual(cancelled["expires_at"], "2026-11-08")
        self.assertIn("已取消", reply.text)

    def test_cancel_frees_monthly_cost(self):
        before = self.agent.store.monthly_subscription_total("u_001")
        run_to_completion(self.agent, self.agent.handle("取消Keep会员"))
        after = self.agent.store.monthly_subscription_total("u_001")
        self.assertAlmostEqual(before - after, 19.0, places=2)

    def test_cancelled_subscription_can_be_resumed(self):
        run_to_completion(self.agent, self.agent.handle("取消腾讯视频会员"))

        reply = self.agent.handle("恢复腾讯视频VIP自动续费")
        self.assertEqual(reply.intent, "subscription.resume")
        self.assertEqual(reply.kind, "confirm")
        self.assertIn("恢复", reply.text)

        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok)
        subscription = next(
            item for item in self.agent.store.subscriptions_of("u_001")
            if item.merchant == "腾讯视频VIP"
        )
        self.assertEqual(subscription.status, "active")
        self.assertTrue(subscription.auto_renew)
        self.assertEqual(subscription.expires_at, "2026-11-08")

        listing = self.agent.handle("我的订阅都有什么")
        row = next(
            item for item in listing.data["items"]
            if item["merchant"] == "腾讯视频VIP"
        )
        self.assertEqual(row["status"], "active")
        self.assertTrue(row["auto_renew"])
        self.assertAlmostEqual(listing.data["monthly_total"], 120.0, places=2)

    def test_resume_rolls_expired_subscription_to_next_cycle(self):
        run_to_completion(self.agent, self.agent.handle("取消Keep会员"))
        self.agent.clock.advance(days=2)

        run_to_completion(self.agent, self.agent.handle("重新开通Keep会员"))
        subscription = next(
            item for item in self.agent.store.subscriptions_of("u_001")
            if item.merchant == "Keep会员"
        )
        self.assertEqual(subscription.status, "active")
        self.assertTrue(subscription.auto_renew)
        self.assertEqual(subscription.expires_at, "2026-11-15")
        self.assertEqual(subscription.next_charge_date, "2026-11-15")

    def test_change_cycle_only_updates_pending_plan(self):
        target = next(
            item for item in self.agent.store.subscriptions_of("u_001")
            if item.merchant == "腾讯视频VIP"
        )
        current_expiry = target.expires_at
        current_amount = target.amount
        tool = self.agent.registry.get("subscription.change_cycle")
        self.assertIsNotNone(tool)

        result = tool.run(
            self.agent.context(),
            merchant="腾讯视频",
            term_months=12,
            renewal_price=288,
        )

        self.assertTrue(result.ok, result.message)
        self.assertEqual(target.expires_at, current_expiry)
        self.assertEqual(target.amount, current_amount)
        self.assertEqual(target.pending_term_months, 12)
        self.assertEqual(target.pending_amount, 288.0)
        self.assertTrue(target.pending_auto_renew)
        self.assertEqual(target.pending_effective_at, current_expiry)

    def test_subscription_list_exposes_pending_plan(self):
        run_to_completion(
            self.agent,
            self.agent.handle("把腾讯视频改成12个月续费，续费价288元"),
        )

        reply = self.agent.handle("我的订阅都有什么")
        row = next(
            item for item in reply.data["items"]
            if item["merchant"] == "腾讯视频VIP"
        )
        self.assertEqual(row["pending_term_months"], 12)
        self.assertEqual(row["pending_amount"], 288.0)
        self.assertTrue(row["pending_auto_renew"])
        self.assertEqual(row["pending_effective_at"], "2026-11-08")
        self.assertIn("下期：12 个月", reply.text)

    def test_cancel_unknown_subscription(self):
        reply = self.agent.handle("取消奈飞会员")
        if reply.kind == "question":
            reply = self.agent.handle("奈飞")
        reply = run_to_completion(self.agent, reply)
        self.assertFalse(reply.ok)
        self.assertIn("没有找到", reply.text)


if __name__ == "__main__":
    unittest.main()
