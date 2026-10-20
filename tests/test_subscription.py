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

    def test_list_active_subscriptions(self):
        reply = self.agent.handle("我的订阅都有什么")
        self.assertEqual(reply.intent, "subscription.list")
        self.assertEqual(reply.data["count"], 6)

    def test_remind_upcoming_charges(self):
        reply = self.agent.handle("最近有哪些订阅要扣费")
        self.assertEqual(reply.intent, "subscription.remind")
        self.assertGreaterEqual(reply.data["count"], 1)
        self.assertTrue(self.agent.store.tasks_of("u_001", status="pending"))

    def test_cancel_subscription(self):
        reply = self.agent.handle("取消腾讯视频会员")
        self.assertEqual(reply.kind, "confirm")
        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok)
        self.assertEqual(reply.data["merchant"], "腾讯视频VIP")
        active = self.agent.store.subscriptions_of("u_001", status="active")
        self.assertNotIn("腾讯视频VIP", {s.merchant for s in active})

    def test_cancel_frees_monthly_cost(self):
        before = self.agent.store.monthly_subscription_total("u_001")
        run_to_completion(self.agent, self.agent.handle("取消Keep会员"))
        after = self.agent.store.monthly_subscription_total("u_001")
        self.assertAlmostEqual(before - after, 19.0, places=2)

    def test_cancel_unknown_subscription(self):
        reply = self.agent.handle("取消奈飞会员")
        if reply.kind == "question":
            reply = self.agent.handle("奈飞")
        reply = run_to_completion(self.agent, reply)
        self.assertFalse(reply.ok)
        self.assertIn("没有找到", reply.text)


if __name__ == "__main__":
    unittest.main()
