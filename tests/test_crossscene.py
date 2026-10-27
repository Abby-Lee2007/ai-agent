"""场景六：跨场景联动（生日惊喜 / 每日简报 / 定时任务执行）。"""
from __future__ import annotations

import unittest

from tests.support import make_agent, run_to_completion, send_money


def begin_birthday_recommendation(
    agent,
    text: str = "我老婆快生日了，帮我准备一下",
    budget: str = "1000",
    preferences: str = "沿用资料",
    avoid: str = "无",
):
    reply = agent.handle(text)
    slots_answered = 0
    while (
        reply.kind == "question"
        and agent.session.pending is not None
        and agent.session.pending.kind == "slot"
        and slots_answered < 6
    ):
        slot = agent.session.pending.missing[0]
        answer = {
            "birthday": "11月20日",
            "preferences": preferences,
            "avoid": avoid,
            "budget": budget,
        }.get(slot)
        if answer is None:
            break
        reply = agent.handle(answer)
        slots_answered += 1
    return reply


def choose_first_gift(agent, reply):
    reply = agent.handle("第1个")
    return reply


class TestCrossScene(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def test_birthday_plan_is_multi_step(self):
        reply = begin_birthday_recommendation(self.agent)
        self.assertEqual(reply.intent, "crossscene.birthday_plan")
        self.assertEqual(reply.kind, "question")
        self.assertEqual(self.agent.session.pending.kind, "gift_choice")
        self.assertIn("2026-11-20", reply.text)
        self.assertIn("第1个", reply.text)
        self.assertEqual(self.agent.store.tasks_of("u_001", status="pending"), [])

        selected = choose_first_gift(self.agent, reply)
        self.assertEqual(selected.kind, "confirm")
        self.assertEqual(selected.plan[0], "goal.create")
        self.assertIn("2026-11-18", selected.text)

    def test_birthday_plan_execution_locks_money_and_books_gifts(self):
        recommendation = begin_birthday_recommendation(self.agent)
        selected_option = recommendation.data["options"][0]
        reply = run_to_completion(
            self.agent,
            choose_first_gift(self.agent, recommendation),
        )
        self.assertTrue(reply.ok)
        self.assertEqual(len(reply.trace), 1 + len(selected_option["items"]))
        goal = self.agent.store.goals_of("u_001", status="active")[0]
        self.assertEqual(goal.locked_amount, selected_option["total"])
        self.assertEqual(goal.deadline, "2026-11-20")
        account = self.agent.store.primary_account("u_001")
        self.assertEqual(account.locked, selected_option["total"])
        tasks = self.agent.store.tasks_of("u_001", status="pending")
        self.assertEqual(len(tasks), len(selected_option["items"]))
        self.assertEqual(
            {t.payload["item"] for t in tasks},
            {item["label"] for item in selected_option["items"]},
        )
        self.assertEqual({t.run_date for t in tasks}, {"2026-11-18"})

    def test_birthday_plan_with_custom_budget(self):
        reply = begin_birthday_recommendation(
            self.agent,
            "帮我准备生日惊喜，预算2000",
            budget="2000",
        )
        self.assertIn("2,000.00", reply.text)

    def test_birthday_plan_asks_for_unknown_birthday(self):
        self.agent.store.get_user("u_003").birthday = ""
        reply = self.agent.handle("帮我给王强准备生日惊喜")
        self.assertEqual(reply.kind, "question")
        self.assertIn("生日", reply.text)
        reply = self.agent.handle("3月2日")
        self.assertEqual(reply.kind, "question")
        self.assertIn("礼物偏好", reply.text)

    def test_known_spouse_birthday_is_not_asked_again(self):
        reply = self.agent.handle("我老婆快生日了，帮我准备一下")
        self.assertNotIn("请输入「11月20日」", reply.text)
        self.assertIn("2026-11-20", reply.text)
        self.assertIn("礼物偏好", reply.text)
        self.assertIn("preferences", self.agent.session.pending.missing)

    def test_gift_recommendations_respect_allergies_and_support_switch(self):
        recommendation = begin_birthday_recommendation(
            self.agent,
            preferences="她喜欢有纪念意义的东西，不喜欢太浓的香味",
            avoid="对花粉和奶制品过敏",
        )
        self.assertEqual(recommendation.kind, "question")
        self.assertEqual(self.agent.session.pending.kind, "gift_choice")
        self.assertTrue(recommendation.data["options"])
        option_names = {option["name"] for option in recommendation.data["options"]}
        self.assertFalse(option_names & {"心意礼物", "精选礼物", "纪念礼物"})
        self.assertTrue(any(
            "定制相册" in name or "银饰礼盒" in name
            or "双人手作体验" in name or "纪念照片书" in name
            or "定制饰品" in name
            for name in option_names
        ))
        self.assertTrue(all(
            any(item["item"] == "礼物" for item in option["items"])
            for option in recommendation.data["options"]
        ))
        self.assertFalse(self.agent.store.tasks_of("u_001", status="pending"))
        prefs = self.agent.store.get_user("u_001").preferences
        care = next(
            (
                item for item in prefs.get("care_people") or []
                if item.get("relation") in {"配偶", "老婆", "妻子", "爱人"}
                or item.get("name") == "林雅雯"
            ),
            None,
        )
        stored_favorites = care.get("favorite_gift") if care else prefs.get("favorite_gift")
        stored_avoid = care.get("avoid") if care else prefs.get("avoid")
        if not isinstance(stored_favorites, list):
            stored_favorites = [str(stored_favorites or "")]
        if not isinstance(stored_avoid, list):
            stored_avoid = [str(stored_avoid or "")]
        self.assertTrue(any("纪念意义" in item for item in stored_favorites))
        self.assertTrue(any("花粉" in item for item in stored_avoid))

        switched = self.agent.handle("换一批")
        self.assertEqual(switched.kind, "question")
        self.assertEqual(self.agent.session.pending.kind, "gift_choice")
        self.assertTrue(switched.data["options"])

    def test_due_tasks_execute_on_time_travel(self):
        recommendation = begin_birthday_recommendation(self.agent)
        run_to_completion(self.agent, choose_first_gift(self.agent, recommendation))
        self.assertEqual(self.agent.run_due_tasks(), [])
        self.agent.clock.advance(days=40)
        messages = self.agent.run_due_tasks()
        self.assertEqual(len(messages), 2)
        self.assertTrue(all("预约下单" in m for m in messages))
        self.assertEqual(self.agent.store.tasks_of("u_001", status="pending"), [])

    def test_scheduled_transfer_executes_later(self):
        send_money(self.agent, "明天给妈妈转2000")
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
        recommendation = begin_birthday_recommendation(self.agent)
        run_to_completion(self.agent, choose_first_gift(self.agent, recommendation))
        reply = self.agent.handle("我的攒钱目标")
        self.assertEqual(reply.intent, "goal.list")
        self.assertIn("生日惊喜金", reply.text)


if __name__ == "__main__":
    unittest.main()
