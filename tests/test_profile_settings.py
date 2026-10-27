"""Editable profile, birthday surprise, and card settings tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from finagent.api.service import AgentSessionService
from finagent.data.sqlite import SQLiteRepository
from tests.support import make_store


class TestProfileSettings(unittest.TestCase):
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
            card_id = profile["cards"][0]["card_id"]

            updated = first.update_card_settings(
                session_id,
                card_id,
                {
                    "daily_limit": 30000,
                    "single_limit": 8000,
                    "online_enabled": False,
                    "note": "测试修改",
                },
            )
            self.assertEqual(updated["daily_limit"], 30000)
            self.assertFalse(updated["online_enabled"])

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
