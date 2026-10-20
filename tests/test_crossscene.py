"""场景六：跨场景联动（生日惊喜 / 每日简报 / 定时任务执行）。"""
from __future__ import annotations

import unittest

from tests.support import make_agent, run_to_completion, send_money


class TestCrossScene(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def test_birthday_plan_is_multi_step(self):
        reply = self.agent.handle("我老婆快生日了，帮我准备一下")
        self.assertEqual(reply.intent, "crossscene.birthday_plan")
        self.assertEqual(reply.kind, "confirm")
        self.assertEqual(reply.plan, ["goal.create", "crossscene.schedule_gift", "crossscene.schedule_gift"])
        self.assertIn("2026-11-18", reply.text)   # 生日前 2 天
        self.assertIn("1,000.00", reply.text)

    def test_birthday_plan_execution_locks_money_and_books_gifts(self):
        reply = run_to_completion(self.agent, self.agent.handle("我老婆快生日了，帮我准备一下"))
        self.assertTrue(reply.ok)
        self.assertEqual(len(reply.trace), 3)
        goal = self.agent.store.goals_of("u_001", status="active")[0]
        self.assertEqual(goal.locked_amount, 1000.0)
        self.assertEqual(goal.deadline, "2026-11-20")
        account = self.agent.store.primary_account("u_001")
        self.assertEqual(account.locked, 1000.0)
        tasks = self.agent.store.tasks_of("u_001", status="pending")
        self.assertEqual(len(tasks), 2)
        self.assertEqual({t.payload["item"] for t in tasks}, {"鲜花", "蛋糕"})
        self.assertEqual({t.run_date for t in tasks}, {"2026-11-18"})

    def test_birthday_plan_with_custom_budget(self):
        reply = self.agent.handle("帮我准备生日惊喜，预算2000")
        self.assertIn("2,000.00", reply.text)

    def test_birthday_plan_asks_for_unknown_birthday(self):
        self.agent.store.get_user("u_003").birthday = ""
        reply = self.agent.handle("帮我给王强准备生日惊喜")
        self.assertEqual(reply.kind, "question")
        self.assertIn("生日", reply.text)
        reply = self.agent.handle("3月2日")
        self.assertEqual(reply.kind, "confirm")
        self.assertIn("2027-03-02", reply.text)

    def test_due_tasks_execute_on_time_travel(self):
        run_to_completion(self.agent, self.agent.handle("我老婆快生日了，帮我准备一下"))
        self.assertEqual(self.agent.run_due_tasks(), [])
        self.agent.clock.advance(days=40)
        messages = self.agent.run_due_tasks()
        self.assertEqual(len(messages), 2)
        self.assertTrue(all("预约下单" in m for m in messages))
        self.assertEqual(self.agent.store.tasks_of("u_001", status="pending"), [])

    def test_scheduled_transfer_executes_later(self):
        send_money(self.agent, "明天给妈妈转2000")
        self.agent.handle("确认")
        transactions = len(self.agent.store.transactions_of("u_001"))
        self.agent.clock.advance(days=1)
        messages = self.agent.run_due_tasks()
        self.assertTrue(any("定时任务" in m for m in messages))
        self.assertEqual(len(self.agent.store.transactions_of("u_001")), transactions + 1)

    def test_daily_brief_aggregates_multiple_domains(self):
        reply = self.agent.handle("今天有什么要注意的")
        self.assertEqual(reply.intent, "crossscene.daily_brief")
        for section in ("风险提醒", "订阅扣费", "待执行计划"):
            self.assertIn(section, reply.text)

    def test_goal_list(self):
        run_to_completion(self.agent, self.agent.handle("我老婆快生日了，帮我准备一下"))
        reply = self.agent.handle("我的攒钱目标")
        self.assertEqual(reply.intent, "goal.list")
        self.assertIn("生日惊喜金", reply.text)


if __name__ == "__main__":
    unittest.main()
