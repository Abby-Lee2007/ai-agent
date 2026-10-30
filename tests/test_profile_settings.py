"""Editable profile, birthday surprise, and card settings tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from finagent.api.service import AgentSessionService, StepUpRequired
from finagent.data.sqlite import SQLiteRepository
from tests.support import make_store


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


class TestProfileSettings(unittest.TestCase):
    def test_profile_phone_must_be_an_11_digit_mainland_mobile_number(self):
        service = AgentSessionService(make_store(), llm_mode="off")
        session_id = service.create_session(user_id="u_001", channel="h5")["session_id"]

        for value in ("1380013800", "138001380000", "23800138000", "1380013800a"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    service.update_profile_settings(session_id, {"phone": value})

        profile = service.update_profile_settings(
            session_id,
            {"phone": "138 0013 8000"},
        )
        self.assertEqual(profile["user"]["phone"], "13800138000")

    def test_birthdays_cannot_be_in_the_future(self):
        service = AgentSessionService(make_store(), llm_mode="off")
        session_id = service.create_session(user_id="u_001", channel="h5")["session_id"]
        future = "2027-01-01"

        cases = [
            {"birthday": future},
            {"spouse_birthday": future},
            {
                "care_people": [{
                    "name": "小雅",
                    "relation": "朋友",
                    "birthday": future,
                }]
            },
        ]
        for values in cases:
            with self.subTest(field=next(iter(values))):
                with self.assertRaises(ValueError) as raised:
                    service.update_profile_settings(session_id, values)
                self.assertIn("不能晚于今天", str(raised.exception))

    def test_care_person_can_store_month_day_without_inventing_birth_year(self):
        service = AgentSessionService(make_store(), llm_mode="off")
        session_id = service.create_session(user_id="u_001", channel="h5")["session_id"]

        profile = service.update_profile_settings(session_id, {
            "care_people": [{
                "name": "陈妈妈",
                "relation": "母亲",
                "birthday_month_day": "11-20",
            }],
        })
        person = profile["preferences"]["care_people"][0]
        self.assertEqual(person["birthday"], "")
        self.assertEqual(person["birthday_month_day"], "11-20")

        with self.assertRaises(ValueError):
            service.update_profile_settings(session_id, {
                "care_people": [{
                    "name": "陈妈妈",
                    "relation": "母亲",
                    "birthday_month_day": "13-40",
                }],
            })

    def test_risk_assessment_result_is_written_back_to_profile(self):
        service = AgentSessionService(make_store(), llm_mode="off")
        session_id = service.create_session(user_id="u_001", channel="h5")["session_id"]
        service.send_message(session_id, "推荐几个理财产品", request_id="risk-start")
        for index in range(7):
            service.send_message(session_id, "b", request_id=f"risk-answer-{index}")

        profile = service.get_profile_settings(session_id)
        self.assertIn("C", profile["user"]["risk_profile"])
        self.assertGreaterEqual(profile["user"]["risk_score"], 1)
        self.assertTrue(profile["user"]["risk_assessed_at"])
        self.assertGreater(profile["user"]["risk_assessment_score"], 0)
        self.assertEqual(
            profile["user"]["risk_assessment_method"],
            "basic_questionnaire_v1",
        )

    def test_legacy_risk_level_is_backfilled_when_session_reloads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent.db"
            first = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            session_id = first.create_session(user_id="u_001", channel="h5")["session_id"]
            record = first._sessions[session_id]
            record.agent.risk_level = 2
            record.agent.risk_assessed_at = "2026-10-27T12:00:00"
            first._persist_session(record)

            second = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            profile = second.get_profile_settings(session_id)

        self.assertIn("C2", profile["user"]["risk_profile"])
        self.assertEqual(profile["user"]["risk_score"], 2)
        self.assertEqual(
            profile["user"]["risk_assessed_at"],
            "2026-10-27T12:00:00",
        )

    def test_profile_card_note_is_visible_to_chat_immediately(self):
        service = AgentSessionService(make_store(), llm_mode="off")
        session_id = service.create_session(user_id="u_001", channel="h5")["session_id"]
        profile = service.get_profile_settings(session_id)
        savings_card = next(card for card in profile["cards"] if card["tail"] == "6688")
        service.update_card_settings(
            session_id,
            savings_card["card_id"],
            {"note": "日常消费卡"},
        )

        reply = service.send_message(
            session_id,
            "从工资卡转200到日常消费卡中",
            request_id="internal-transfer-after-card-edit",
        )["reply"]

        self.assertEqual(reply["kind"], "confirm")
        self.assertIn("6688", reply["text"])
        self.assertNotIn("没有找到付款卡或收款卡", reply["text"])

    def test_bound_card_appears_in_profile_settings(self):
        service = AgentSessionService(make_store(), llm_mode="off")
        session_id = service.create_session(user_id="u_001", channel="h5")["session_id"]

        turns = [
            "绑定一张中国银行的银行卡",
            "6216610000000000008",
            "123456",
            "13800138000",
            "确认",
        ]
        reply = None
        for text in turns:
            reply = service.send_message(session_id, text)["reply"]

        self.assertEqual(reply["kind"], "executed")
        self.assertTrue(reply["ok"])
        profile = service.get_profile_settings(session_id)
        bound = [
            card for card in profile["cards"]
            if card["brand"] == "中国银行" and card["tail"] == "0008"
        ]
        self.assertEqual(len(bound), 1)
        self.assertEqual(bound[0]["status"], "active")

    def test_profile_and_birthday_settings_persist(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent.db"
            first = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            session_id = first.create_session(user_id="u_001", channel="h5")["session_id"]
            updated = first.update_profile_settings(
                session_id,
                {
                    "name": "测试用户",
                    "phone": "13800138000",
                    "city": "广州",
                    "age": 32,
                    "occupation": "产品设计师",
                    "monthly_income": 22000,
                    "birthday": "1994-06-18",
                    "spouse_name": "测试爱人",
                    "spouse_birthday": "1996-11-20",
                    "anniversary": "2027-05-20",
                    "favorite_gift": "鲜花,蛋糕",
                    "avoid": "海鲜",
                },
            )
            self.assertEqual(updated["user"]["name"], "测试用户")
            self.assertEqual(updated["preferences"]["spouse_name"], "测试爱人")

            second = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            restored = second.get_profile_settings(session_id)

        self.assertEqual(restored["user"]["name"], "测试用户")
        self.assertEqual(restored["user"]["city"], "广州")
        self.assertEqual(restored["preferences"]["spouse_birthday"], "1996-11-20")
        self.assertIn("蛋糕", restored["preferences"]["favorite_gift"])

    def test_card_settings_can_be_updated_and_added(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent.db"
            first = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            session_id = first.create_session(user_id="u_001", channel="h5")["session_id"]
            profile = first.get_profile_settings(session_id)
            editable_card = next(
                card for card in profile["cards"]
                if card["kind"] == "debit"
            )
            card_id = editable_card["card_id"]

            updated = confirm_card_update(
                first,
                session_id,
                card_id,
                {
                    "kind": "credit",
                    "tail": "0000",
                    "daily_limit": 30000,
                    "single_limit": 8000,
                    "online_enabled": False,
                    "note": "测试修改",
                },
            )
            self.assertEqual(updated["daily_limit"], 30000)
            self.assertFalse(updated["online_enabled"])
            self.assertEqual(updated["kind"], editable_card["kind"])
            self.assertEqual(updated["tail"], editable_card["tail"])

            created = first.create_card_settings(
                session_id,
                {
                    "kind": "virtual",
                    "brand": "测试银行",
                    "tail": "9999",
                    "daily_limit": 3000,
                    "single_limit": 1000,
                    "note": "用户新增虚拟卡",
                },
            )
            self.assertEqual(created["tail"], "9999")

            second = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            restored = second.get_profile_settings(session_id)

        restored_card = next(item for item in restored["cards"] if item["card_id"] == card_id)
        self.assertEqual(restored_card["daily_limit"], 30000)
        self.assertFalse(restored_card["online_enabled"])
        self.assertTrue(any(item["tail"] == "9999" for item in restored["cards"]))

    def test_seed_card_corruption_is_repaired_without_losing_note(self):
        store = make_store()
        savings_card = store.cards["CARD000004"]
        savings_card.kind = "credit"
        savings_card.tail = "****"
        savings_card.account_id = "ACC000003"
        savings_card.status = "pending"
        savings_card.note = "日常消费卡"

        store.ensure_card_ledger()

        self.assertEqual(savings_card.kind, "debit")
        self.assertEqual(savings_card.tail, "6688")
        self.assertEqual(savings_card.account_id, "ACC000002")
        self.assertEqual(savings_card.status, "active")
        self.assertEqual(savings_card.note, "日常消费卡")
        self.assertEqual(store.cards["CARD000003"].tail, "3011")

    def test_birthday_surprise_uses_updated_spouse_date_in_hint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(Path(temp_dir) / "agent.db"),
            )
            session_id = service.create_session(user_id="u_001", channel="h5")["session_id"]
            service.update_profile_settings(session_id, {
                "spouse_name": "新的爱人",
                "spouse_birthday": "1997-12-05",
            })
            reply = service.send_message(
                session_id,
                "我老婆快生日了，帮我准备一下",
                request_id="birthday-after-profile-edit",
            )

        self.assertEqual(reply["reply"]["kind"], "question")
        self.assertIn("新的爱人", reply["reply"]["text"])
        self.assertIn("12-05", reply["reply"]["text"])

    def test_card_can_be_deleted_and_stays_deleted_after_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent.db"
            first = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            session_id = first.create_session(user_id="u_001", channel="h5")["session_id"]
            created = first.create_card_settings(session_id, {
                "kind": "virtual",
                "brand": "待删除银行",
                "tail": "8888",
            })
            result = first.delete_card_settings(session_id, created["card_id"])
            self.assertEqual(result["deleted_card_id"], created["card_id"])

            second = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            restored = second.get_profile_settings(session_id)

        self.assertFalse(any(
            card["card_id"] == created["card_id"]
            for card in restored["cards"]
        ))

    def test_care_people_can_be_added_deleted_and_used_for_birthday(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent.db"
            first = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            session_id = first.create_session(user_id="u_001", channel="h5")["session_id"]
            profile = first.update_profile_settings(session_id, {
                "birthday": "1994-06-18",
                "care_people": [
                    {
                        "person_id": "CARE0001",
                        "name": "小雅",
                        "relation": "配偶",
                        "birthday": "1996-12-05",
                        "favorite_gift": "鲜花,蛋糕",
                        "avoid": "海鲜",
                    },
                    {
                        "person_id": "CARE0002",
                        "name": "妈妈",
                        "relation": "母亲",
                        "birthday": "1968-03-08",
                    },
                ],
            })
            self.assertEqual(profile["user"]["birthday"], "1994-06-18")
            self.assertEqual(len(profile["preferences"]["care_people"]), 2)

            prompt = first.send_message(
                session_id,
                "我老婆快生日了，帮我准备一下",
                request_id="care-birthday-prompt",
            )
            self.assertIn("小雅", prompt["reply"]["text"])
            self.assertIn("12-05", prompt["reply"]["text"])

            reduced = first.update_profile_settings(session_id, {
                "care_people": profile["preferences"]["care_people"][:1],
            })
            self.assertEqual(len(reduced["preferences"]["care_people"]), 1)

            second = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            restored = second.get_profile_settings(session_id)

        self.assertEqual(restored["preferences"]["care_people"][0]["name"], "小雅")
        self.assertEqual(restored["user"]["birthday"], "1994-06-18")


if __name__ == "__main__":
    unittest.main()
