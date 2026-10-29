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

    def test_gift_lock_and_purchase_sync_card_goal_and_ledger(self):
        recommendation = begin_birthday_recommendation(self.agent)
        selected = recommendation.data["options"][0]
        run_to_completion(
            self.agent,
            choose_first_gift(self.agent, recommendation),
        )
        account = self.agent.store.primary_account("u_001")
        card = self.agent.store.find_card("u_001", "工资卡")
        birthday_goal = next(
            goal for goal in self.agent.store.goals_of("u_001", status="active")
            if goal.scenario == "birthday"
        )
        selected_total = round(float(selected["total"]), 2)
        balance_after_lock = account.balance

        self.assertAlmostEqual(birthday_goal.locked_amount, selected_total, places=2)
        self.assertAlmostEqual(account.locked, selected_total, places=2)
        self.assertAlmostEqual(card.locked, selected_total, places=2)
        self.assertAlmostEqual(card.available, card.balance - selected_total, places=2)

        self.agent.clock.advance(days=40)
        gift_tasks = [
            task for task in self.agent.scheduler.due("u_001")
            if task.kind == "gift_order"
        ]
        messages = [
            self.agent.scheduler._execute(self.agent.context(), task)
            for task in gift_tasks
        ]
        executed_tasks = [
            task for task in gift_tasks if task.status == "done"
        ]

        self.assertGreaterEqual(len(messages), len(selected["items"]))
        self.assertAlmostEqual(account.balance, balance_after_lock - selected_total, places=2)
        self.assertAlmostEqual(card.balance, account.balance, places=2)
        self.assertEqual(account.locked, 0.0)
        self.assertEqual(card.locked, 0.0)
        self.assertEqual(birthday_goal.status, "achieved")
        self.assertEqual(birthday_goal.locked_amount, 0.0)
        self.assertTrue(all(task.payload.get("payment_txn_id") for task in executed_tasks))
        self.assertTrue(all(
            self.agent.store.transactions[task.payload["payment_txn_id"]].category == "跨场景"
            for task in executed_tasks
        ))

    def test_birthday_plan_with_custom_budget(self):
        reply = begin_birthday_recommendation(
            self.agent,
            "帮我给老师准备生日惊喜，预算2000",
            budget="2000",
        )
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

    def test_birthday_shortcut_uses_the_only_saved_care_person(self):
        prefs = dict(self.agent.store.get_user("u_001").preferences or {})
        prefs["care_people"] = [{
            "person_id": "CARE-SISTER",
            "name": "妹妹",
            "relation": "妹妹",
            "birthday": "2000-05-01",
        }]
        self.agent.store.get_user("u_001").preferences = prefs

        reply = self.agent.handle("生日惊喜")

        self.assertEqual(reply.intent, "crossscene.birthday_plan")
        self.assertNotIn("要送给谁", reply.text)
        self.assertIn("妹妹", reply.text)
        self.assertIn("2027-05-01", reply.text)
        self.assertNotIn("birthday", self.agent.session.pending.missing)

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

    def test_spouse_alias_uses_saved_birthday_during_person_slot(self):
        reply = self.agent.handle("生日惊喜")
        self.assertEqual(self.agent.session.pending.missing, ["person"])

        reply = self.agent.handle("配偶")

        self.assertEqual(reply.intent, "crossscene.birthday_plan")
        self.assertIn("林雅雯", reply.text)
        self.assertIn("2026-11-20", reply.text)
        self.assertNotIn("没有「配偶」的生日", reply.text)
        self.assertEqual(self.agent.session.pending.missing, ["preferences", "avoid", "budget"])

    def test_spouse_alias_can_replace_person_while_birthday_is_pending(self):
        self.agent.handle("生日惊喜")
        reply = self.agent.handle("老师")
        self.assertEqual(self.agent.session.pending.missing, ["birthday"])

        reply = self.agent.handle("老婆")

        self.assertIn("林雅雯", reply.text)
        self.assertIn("2026-11-20", reply.text)
        self.assertNotIn("请输入「11月20日」", reply.text)
        self.assertEqual(self.agent.session.pending.slots["person"], "老婆")
        self.assertEqual(self.agent.session.pending.missing, ["preferences", "avoid", "budget"])

    def test_combined_birthday_answer_splits_and_syncs_profile(self):
        self.agent.handle("生日惊喜")
        reply = self.agent.handle("老婆")
        self.assertEqual(reply.intent, "crossscene.birthday_plan")

        reply = self.agent.handle(
            "海鲜过敏、郁金香过敏、喜欢跑步、第五人格、有睡眠障碍"
        )

        self.assertIn("预算", reply.text)
        self.assertNotIn("礼物偏好", reply.text)
        self.assertEqual(self.agent.session.pending.missing, ["budget"])
        self.assertIn("跑步", self.agent.session.pending.slots["preferences"])
        self.assertIn("第五人格", self.agent.session.pending.slots["preferences"])
        self.assertIn("睡眠障碍", self.agent.session.pending.slots["preferences"])
        self.assertIn("海鲜过敏", self.agent.session.pending.slots["avoid"])
        self.assertIn("郁金香过敏", self.agent.session.pending.slots["avoid"])

        prefs = self.agent.store.get_user("u_001").preferences
        stored_favorites = "、".join(prefs.get("favorite_gift") or [])
        stored_avoid = "、".join(prefs.get("avoid") or [])
        self.assertIn("第五人格", stored_favorites)
        self.assertIn("海鲜", stored_avoid)
        self.assertIn("郁金香过敏", stored_avoid)

    def test_long_preference_answer_is_not_rejected(self):
        self.agent.handle("生日惊喜")
        self.agent.handle("老婆")

        reply = self.agent.handle(
            "平时喜欢饰品、手工、蓝色、玫瑰和茉莉的味道、爱跑步"
        )

        self.assertIn("过敏", reply.text)
        self.assertNotIn("礼物偏好", reply.text)
        self.assertEqual(self.agent.session.pending.missing, ["avoid", "budget"])
        self.assertIn(
            "蓝色",
            self.agent.session.pending.slots["preferences"],
        )

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

    def test_fruit_allergy_excludes_cakes_and_prioritizes_safe_gifts(self):
        recommendation = begin_birthday_recommendation(
            self.agent,
            budget="90000",
            preferences="沿用资料",
            avoid="大部分水果都过敏",
        )

        self.assertEqual(recommendation.kind, "question")
        self.assertEqual(self.agent.session.pending.kind, "gift_choice")
        options = recommendation.data["options"]
        self.assertEqual(len(options), 3)
        all_items = [
            item
            for option in options
            for item in option["items"]
        ]
        self.assertFalse(any(item["item"] == "蛋糕" for item in all_items))
        self.assertFalse(any(
            word in f"{item['label']} {item['merchant']}"
            for item in all_items
            for word in ("蛋糕", "水果", "芒果", "草莓", "甜品")
        ))
        self.assertTrue(any(item["item"] == "礼物" for item in options[0]["items"]))

    def test_birthday_recommendations_follow_supplied_preference(self):
        prefs = dict(self.agent.store.get_user("u_001").preferences or {})
        prefs["care_people"] = [{
            "person_id": "CARE-SISTER",
            "name": "两只猪头",
            "relation": "妹妹",
            "birthday": "2000-10-17",
        }]
        self.agent.store.get_user("u_001").preferences = prefs

        recommendation = begin_birthday_recommendation(
            self.agent,
            text="给两只猪头准备生日惊喜",
            budget="90000",
            preferences="大疆",
            avoid="无",
        )

        option_names = [option["name"] for option in recommendation.data["options"]]
        self.assertEqual(len(option_names), 3)
        self.assertTrue(all(
            "大疆" in name or "无人机" in name or "云台" in name or "运动相机" in name
            for name in option_names
        ))

    def test_unknown_preference_creates_taobao_search_options(self):
        prefs = dict(self.agent.store.get_user("u_001").preferences or {})
        prefs["care_people"] = [{
            "person_id": "CARE-SISTER",
            "name": "两只猪头",
            "relation": "妹妹",
            "birthday": "2000-10-17",
        }]
        self.agent.store.get_user("u_001").preferences = prefs

        recommendation = begin_birthday_recommendation(
            self.agent,
            text="给两只猪头准备生日惊喜",
            budget="90000",
            preferences="乐高",
            avoid="无",
        )

        options = recommendation.data["options"]
        self.assertEqual(len(options), 3)
        self.assertTrue(all("乐高" in option["name"] for option in options))
        self.assertTrue(all(option.get("external_only") for option in options))
        self.assertTrue(all(
            option["items"][0]["merchant"] == "淘宝搜索"
            and option["items"][0]["price_pending"]
            for option in options
        ))
        self.assertTrue(all(
            option.get("price_label")
            and option.get("price_estimate_min", 0) > 0
            and option.get("price_estimate_max", 0)
            >= option.get("price_estimate_min", 0)
            and option.get("price_source") == "市场参考区间（非实时淘宝价格）"
            for option in options
        ))
        self.assertIn("市场参考价", recommendation.text)
        self.assertNotIn("价格待核实", recommendation.text)

    def test_hobby_preferences_generate_concrete_gifts_and_new_batches(self):
        recommendation = begin_birthday_recommendation(
            self.agent,
            budget="2000",
            preferences="还喜欢编织、长跑、第五人格",
            avoid="无",
        )
        first = recommendation.data["options"]
        names = [option["name"] for option in first]

        self.assertEqual(len(first), 3)
        self.assertGreaterEqual(recommendation.data["total_options"], 9)
        self.assertFalse(any(name.endswith(("生日礼物", "礼盒", "精选")) for name in names))
        self.assertTrue(any("毛线" in name or "编织" in name for name in names))
        self.assertTrue(any("跑步" in name or "运动" in name for name in names))
        self.assertTrue(any("第五人格" in name for name in names))
        self.assertTrue(all(option.get("external_only") for option in first))
        self.assertTrue(all(option.get("price_label") for option in first))

        second_reply = self.agent.handle("换一批")
        second = second_reply.data["options"]
        second_names = [option["name"] for option in second]
        self.assertEqual(len(second), 3)
        self.assertFalse(set(names) & set(second_names))
        self.assertEqual(second_reply.data["batch_index"], 2)

        third_reply = self.agent.handle("换一批")
        third_names = [option["name"] for option in third_reply.data["options"]]
        self.assertFalse(set(names) & set(third_names))
        self.assertFalse(set(second_names) & set(third_names))

    def test_sleep_need_is_turned_into_concrete_gifts_with_saved_hobbies(self):
        prefs = dict(self.agent.store.get_user("u_001").preferences or {})
        prefs["care_people"] = [{
            "person_id": "CARE-SPOUSE",
            "name": "林雅雯",
            "relation": "配偶",
            "birthday": "1996-11-20",
            "favorite_gift": ["鲜花", "蛋糕", "还喜欢编织、长跑、第五人格"],
            "avoid": ["海鲜"],
        }]
        self.agent.store.get_user("u_001").preferences = prefs

        recommendation = begin_birthday_recommendation(
            self.agent,
            budget="2000",
            preferences="她睡眠质量不好",
            avoid="无",
        )
        options = recommendation.data["options"]
        names = [option["name"] for option in options]

        self.assertTrue(any(
            word in names[0] for word in ("眼罩", "白噪音", "颈枕", "阅读灯")
        ))
        self.assertTrue(any(
            word in " ".join(names)
            for word in ("编织", "跑步", "第五人格")
        ))
        self.assertFalse(any(name.endswith(("生日礼物", "礼盒", "精选")) for name in names))
        self.assertIn("不宣称治疗失眠", recommendation.text)
        self.assertTrue(all(option.get("price_label") for option in options))

        next_batch = self.agent.handle("换一批")
        next_names = [option["name"] for option in next_batch.data["options"]]
        self.assertFalse(set(names) & set(next_names))
        self.assertTrue(all(
            option.get("search_query") not in {"她睡眠质量不好 生日礼物"}
            for option in options
            if option.get("search_query")
        ))

    def test_birthday_preference_followup_acknowledges_answer(self):
        prefs = dict(self.agent.store.get_user("u_001").preferences or {})
        prefs["care_people"] = [{
            "person_id": "CARE-SISTER",
            "name": "两只猪头",
            "relation": "妹妹",
            "birthday": "2000-10-17",
        }]
        self.agent.store.get_user("u_001").preferences = prefs

        reply = self.agent.handle("给两只猪头准备生日惊喜")
        self.assertEqual(self.agent.session.pending.missing[0], "preferences")

        reply = self.agent.handle("大疆")

        self.assertIn("已记录偏好「大疆」", reply.text)
        self.assertIn("过敏", reply.text)
        self.assertEqual(self.agent.session.pending.missing[0], "avoid")

    def test_negative_preference_answer_uses_saved_preferences(self):
        prefs = dict(self.agent.store.get_user("u_001").preferences or {})
        prefs["care_people"] = [{
            "person_id": "CARE-SISTER",
            "name": "两只猪头",
            "relation": "妹妹",
            "birthday": "2000-10-17",
            "favorite_gift": ["大疆"],
            "avoid": [],
        }]
        prefs["avoid"] = []
        self.agent.store.get_user("u_001").preferences = prefs

        reply = self.agent.handle("给两只猪头准备生日惊喜")
        self.assertEqual(self.agent.session.pending.missing[0], "preferences")

        reply = self.agent.handle("没有")

        self.assertNotIn("已记录偏好「没有」", reply.text)
        self.assertIn("过敏", reply.text)
        self.assertEqual(self.agent.session.pending.missing[0], "avoid")
        self.assertEqual(
            self.agent.session.pending.slots.get("preferences"),
            "沿用资料",
        )

        self.agent.handle("无")
        recommendation = self.agent.handle("90000")

        option_names = [option["name"] for option in recommendation.data["options"]]
        self.assertTrue(all("大疆" in name for name in option_names))

    def test_gift_choice_can_switch_to_another_shortcut(self):
        begin_birthday_recommendation(self.agent)
        self.assertEqual(self.agent.session.pending.kind, "gift_choice")

        switched = self.agent.handle("自动续费检测")

        self.assertEqual(switched.intent, "subscription.detect")
        self.assertIsNone(self.agent.session.pending)
        self.assertNotIn("请回复要选择的方案序号", switched.text)

    def test_due_tasks_execute_on_time_travel(self):
        recommendation = begin_birthday_recommendation(self.agent)
        run_to_completion(self.agent, choose_first_gift(self.agent, recommendation))
        self.assertEqual(self.agent.run_due_tasks(), [])
        self.agent.clock.advance(days=40)
        messages = self.agent.run_due_tasks()
        self.assertEqual(sum("预约下单" in message for message in messages), 2)
        self.assertEqual(self.agent.store.tasks_of("u_001", status="pending"), [])

    def test_scheduled_transfer_executes_later(self):
        send_money(self.agent, "明天给妈妈转2000")
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
        self.assertEqual(reply.kind, "confirm")
        self.assertTrue(reply.data["daily_brief_intake"]["materials"])
        self.assertTrue(reply.data["daily_brief_intake"]["channels"])
        self.assertIn("不会随机编造", reply.text)

        reply = self.agent.handle("确认")
        for section in ("风险提醒", "订阅扣费", "待执行计划"):
            self.assertIn(section, reply.text)
        self.assertTrue(reply.data["materials_confirmed"])
        self.assertIn("数据口径", reply.text)

    def test_goal_list(self):
        recommendation = begin_birthday_recommendation(self.agent)
        run_to_completion(self.agent, choose_first_gift(self.agent, recommendation))
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

    def test_gift_item_is_not_misread_as_birthday_intent(self):
        for item in ("鲜花饼", "生日蛋糕", "鲜花礼盒"):
            with self.subTest(item=item):
                agent = make_agent()
                reply = agent.handle("伴手礼/礼物预约")
                self.assertEqual(agent.session.pending.missing, ["person"])

                reply = agent.handle("妹妹")
                self.assertEqual(agent.session.pending.missing, ["item"])

                reply = agent.handle(item)

                self.assertEqual(reply.intent, "crossscene.gift_plan")
                self.assertEqual(reply.kind, "question")
                self.assertEqual(agent.session.pending.intent, "crossscene.gift_plan")
                self.assertEqual(agent.session.pending.slots.get("item"), item)
                self.assertEqual(agent.session.pending.missing, ["date"])

    def test_failed_birthday_recommendation_can_retry_with_new_constraints(self):
        reply = begin_birthday_recommendation(
            self.agent,
            budget="70",
            avoid="对花粉和奶制品过敏",
        )

        self.assertFalse(reply.ok)
        self.assertIn("没有可预约礼物", reply.text)
        self.assertEqual(self.agent.session.pending.kind, "gift_recommend_retry")
        self.assertEqual(self.agent.session.pending.slots.get("budget"), 70.0)

        retried = self.agent.handle("预算1000，没有禁忌")

        self.assertTrue(retried.ok, retried.text)
        self.assertEqual(retried.intent, "crossscene.birthday_plan")
        self.assertEqual(self.agent.session.pending.kind, "gift_choice")
        self.assertEqual(self.agent.session.pending.slots.get("budget"), 1000.0)
        self.assertEqual(self.agent.session.pending.slots.get("avoid"), "没有禁忌")

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
