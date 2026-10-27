"""场景六：跨场景联动（生日惊喜 / 每日简报 / 定时任务执行）。"""
from __future__ import annotations

import unittest

from tests.support import make_agent, run_to_completion, send_money


def begin_birthday_plan(agent, text: str = "我老婆快生日了，帮我准备一下"):
    reply = agent.handle(text)
    if reply.kind == "question" and agent.session.pending is not None:
        reply = agent.handle("11月20日")
    return reply


class TestCrossScene(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def test_birthday_plan_is_multi_step(self):
        reply = begin_birthday_plan(self.agent)
        self.assertEqual(reply.intent, "crossscene.birthday_plan")
        self.assertEqual(reply.kind, "confirm")
        self.assertEqual(reply.plan, ["goal.create", "crossscene.schedule_gift", "crossscene.schedule_gift"])
        self.assertIn("2026-11-18", reply.text)   # 生日前 2 天
        self.assertIn("1,000.00", reply.text)

    def test_birthday_plan_execution_locks_money_and_books_gifts(self):
        reply = run_to_completion(self.agent, begin_birthday_plan(self.agent))
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
        reply = begin_birthday_plan(self.agent, "帮我给老师准备生日惊喜，预算2000")
        self.assertIn("2,000.00", reply.text)

    def test_generic_birthday_shortcut_asks_for_recipient(self):
        reply = self.agent.handle("生日惊喜")

        self.assertEqual(reply.intent, "crossscene.birthday_plan")
        self.assertEqual(reply.kind, "question")
        self.assertEqual(self.agent.session.pending.missing, ["person"])
        self.assertNotIn("老婆", reply.text)
        self.assertNotIn("爱人", reply.text)

        reply = self.agent.handle("老师")
        self.assertEqual(reply.kind, "question")
        self.assertIn("生日", reply.text)
        self.assertEqual(self.agent.session.pending.missing, ["birthday"])

    def test_birthday_plan_asks_for_unknown_birthday(self):
        self.agent.store.get_user("u_003").birthday = ""
        reply = self.agent.handle("帮我给王强准备生日惊喜")
        self.assertEqual(reply.kind, "question")
        self.assertIn("生日", reply.text)
        reply = self.agent.handle("3月2日")
        self.assertEqual(reply.kind, "confirm")
        self.assertIn("2027-03-02", reply.text)

    def test_due_tasks_execute_on_time_travel(self):
        run_to_completion(self.agent, begin_birthday_plan(self.agent))
        self.assertEqual(self.agent.run_due_tasks(), [])
        self.agent.clock.advance(days=40)
        messages = self.agent.run_due_tasks()
        self.assertEqual(sum("预约下单" in message for message in messages), 2)
        self.assertEqual(self.agent.store.tasks_of("u_001", status="pending"), [])

    def test_scheduled_transfer_executes_later(self):
        send_money(self.agent, "明天给妈妈转2000")
        self.agent.handle("确认")
        transactions = len(self.agent.store.transactions_of("u_001"))
        self.agent.clock.advance(days=1)
        messages = self.agent.run_due_tasks()
        self.assertTrue(any("定时任务" in m for m in messages))
        self.assertGreaterEqual(
            len(self.agent.store.transactions_of("u_001")),
            transactions + 1,
        )

    def test_daily_brief_aggregates_multiple_domains(self):
        reply = self.agent.handle("今天有什么要注意的")
        self.assertEqual(reply.intent, "crossscene.daily_brief")
        for section in ("风险提醒", "订阅扣费", "待执行计划"):
            self.assertIn(section, reply.text)

    def test_goal_list(self):
        run_to_completion(self.agent, begin_birthday_plan(self.agent))
        reply = self.agent.handle("我的攒钱目标")
        self.assertEqual(reply.intent, "goal.list")
        self.assertIn("生日惊喜金", reply.text)

    def test_souvenir_gift_plan_asks_for_date_and_creates_task(self):
        reply = self.agent.handle("给老婆准备一束花")
        self.assertEqual(reply.intent, "crossscene.gift_plan")
        self.assertEqual(reply.kind, "question")
        self.assertIn("日期", reply.text)

        reply = self.agent.handle("11月20日")
        self.assertEqual(reply.kind, "confirm")
        self.assertIn("鲜花", reply.text)

        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok, reply.text)
        tasks = self.agent.store.tasks_of("u_001", status="pending")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].kind, "gift_order")

    def test_generic_graduation_gift_asks_for_item_and_custom_price(self):
        reply = self.agent.handle("明天毕业了我想给老师送个毕业礼物")
        self.assertEqual(reply.intent, "crossscene.gift_plan")
        self.assertEqual(reply.kind, "question")
        self.assertIn("什么礼物", reply.text)

        reply = self.agent.handle("给老师下单一个苹果手机")
        self.assertEqual(reply.kind, "question")
        self.assertIn("价格", reply.text)

        reply = self.agent.handle("预算8000")
        self.assertEqual(reply.kind, "confirm")
        self.assertIn("苹果手机", reply.text)
        self.assertIn("8,000.00", reply.text)
        self.assertIn("2026-10-15", reply.text)
        self.assertIn("用户报价", reply.text)

        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok, reply.text)
        task = self.agent.store.tasks_of("u_001", status="pending")[0]
        self.assertEqual(task.payload["item"], "苹果手机")
        self.assertEqual(task.payload["budget"], 8000.0)
        self.assertEqual(task.payload["source"], "user:quote")

    def test_gift_confirmation_can_be_revised_to_another_item_and_price(self):
        reply = self.agent.handle("明天给老师送一束花")
        self.assertEqual(reply.kind, "confirm")
        self.assertIn("2026-10-15", reply.text)
        self.assertIn("花间集", reply.text)

        reply = self.agent.handle("把礼物改成苹果手机，预算8000")
        self.assertEqual(reply.intent, "crossscene.gift_plan")
        self.assertEqual(reply.kind, "confirm")
        self.assertIn("苹果手机", reply.text)
        self.assertIn("8,000.00", reply.text)
        self.assertIn("2026-10-15", reply.text)

        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok, reply.text)
        task = self.agent.store.tasks_of("u_001", status="pending")[0]
        self.assertEqual(task.payload["item"], "苹果手机")
        self.assertEqual(task.payload["budget"], 8000.0)

    def test_other_custom_gift_is_not_forced_to_flower_or_cake(self):
        reply = self.agent.handle("明天给老师送一盒茶叶，预算300")
        self.assertEqual(reply.intent, "crossscene.gift_plan")
        self.assertEqual(reply.kind, "confirm")
        self.assertIn("一盒茶叶", reply.text)
        self.assertIn("用户报价", reply.text)

        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok, reply.text)
        task = self.agent.store.tasks_of("u_001", status="pending")[0]
        self.assertEqual(task.payload["item"], "一盒茶叶")
        self.assertEqual(task.payload["budget"], 300.0)

    def test_custom_gift_name_with_variant_is_preserved(self):
        reply = self.agent.handle("明天给老师下单一个苹果手机 Pro，预算8999")
        self.assertEqual(reply.intent, "crossscene.gift_plan")
        self.assertEqual(reply.kind, "confirm")
        self.assertIn("苹果手机 Pro", reply.text)


if __name__ == "__main__":
    unittest.main()
