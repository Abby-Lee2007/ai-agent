"""Care-person contact accounts and transfer resolution."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from finagent.api.service import AgentSessionService
from finagent.data.sqlite import SQLiteRepository
from tests.support import make_store


CONTACTS = {
    "person_id": "CARE9001",
    "name": "小雅",
    "relation": "朋友",
    "birthday": "1996-12-05",
    "phone": "13800138000",
    "wechat": "xiaoya_wx",
    "alipay": "13800138000",
    "bank_name": "工商银行",
    "bank_card": "6222020000000007",
}


class TestCarePeopleTransfer(unittest.TestCase):
    def _service(self, temp_dir: str) -> tuple[AgentSessionService, str]:
        service = AgentSessionService(
            make_store(),
            llm_mode="off",
            store_factory=make_store,
            repository=SQLiteRepository(Path(temp_dir) / "agent.db"),
        )
        session_id = service.create_session(user_id="u_001", channel="h5")["session_id"]
        return service, session_id

    def test_care_person_contact_accounts_persist(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service, session_id = self._service(temp_dir)
            updated = service.update_profile_settings(
                session_id,
                {"care_people": [CONTACTS]},
            )

            person = updated["preferences"]["care_people"][0]

            self.assertEqual(person["phone"], "13800138000")
            self.assertEqual(person["wechat"], "xiaoya_wx")
            self.assertEqual(person["alipay"], "13800138000")
            self.assertEqual(person["bank_name"], "工商银行")
            self.assertEqual(person["bank_card"], "")
            self.assertEqual(person["bank_card_tail"], "0007")
            self.assertEqual(person["bank_card_masked"], "**** **** **** 0007")
            stored = service.repository.load_session(session_id)
            self.assertNotIn(
                "6222020000000007",
                json.dumps(stored, ensure_ascii=False),
            )

            restarted = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(Path(temp_dir) / "agent.db"),
            )
            restored = restarted.get_profile_settings(session_id)

        restored_person = restored["preferences"]["care_people"][0]
        self.assertEqual(restored_person["wechat"], "xiaoya_wx")
        self.assertEqual(restored_person["bank_name"], "工商银行")
        self.assertEqual(restored_person["bank_card_masked"], "**** **** **** 0007")

    def test_care_person_bank_name_is_required_with_card(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service, session_id = self._service(temp_dir)

            with self.assertRaises(ValueError) as raised:
                service.update_profile_settings(
                    session_id,
                    {"care_people": [{**CONTACTS, "bank_name": ""}]},
                )

        self.assertIn("开户银行", str(raised.exception))

    def test_care_person_card_error_explains_luhn_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service, session_id = self._service(temp_dir)

            with self.assertRaises(ValueError) as raised:
                service.update_profile_settings(
                    session_id,
                    {
                        "care_people": [{
                            **CONTACTS,
                            "bank_card": "623478567898787656",
                        }]
                    },
                )

        message = str(raised.exception)
        self.assertIn("18 位", message)
        self.assertIn("Luhn", message)

    def test_care_person_can_be_used_for_all_transfer_channels(self):
        cases = [
            ("微信", "xiaoya_wx"),
            ("支付宝", "13800138000"),
            ("银行卡", "**** **** **** 0007"),
        ]
        for channel, expected_account in cases:
            with self.subTest(channel=channel), tempfile.TemporaryDirectory() as temp_dir:
                service, session_id = self._service(temp_dir)
                service.update_profile_settings(
                    session_id,
                    {"care_people": [CONTACTS]},
                )

                review = service.send_message(
                    session_id,
                    f"给小雅转500块，用{channel}",
                    request_id=f"care-transfer-review-{channel}",
                )
                self.assertEqual(review["reply"]["kind"], "confirm")
                self.assertIn(expected_account, review["reply"]["text"])

                done = service.send_message(
                    session_id,
                    "确认",
                    request_id=f"care-transfer-confirm-{channel}",
                )
                done = service.send_message(
                    session_id,
                    "246810",
                    request_id=f"care-transfer-password-{channel}",
                )
                self.assertTrue(done["reply"]["ok"])
                self.assertIn("转账成功", done["reply"]["text"])

    def test_missing_selected_channel_is_explained_before_confirmation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service, session_id = self._service(temp_dir)
            service.update_profile_settings(
                session_id,
                {
                    "care_people": [{
                        **CONTACTS,
                        "alipay": "",
                        "bank_card": "",
                    }]
                },
            )

            review = service.send_message(
                session_id,
                "给小雅转500块，用支付宝",
                request_id="care-transfer-missing-channel",
            )

        self.assertEqual(review["reply"]["kind"], "question")
        self.assertIn("尚未配置支付宝账号", review["reply"]["text"])
        self.assertIn("确认前不会发起转账", review["reply"]["text"])

    def test_repeated_profile_save_does_not_duplicate_payee(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service, session_id = self._service(temp_dir)
            for index in range(2):
                service.update_profile_settings(
                    session_id,
                    {"care_people": [{**CONTACTS, "wechat": f"xiaoya_{index}"}]},
                )
                service.send_message(
                    session_id,
                    "给小雅转10块，用微信",
                    request_id=f"care-transfer-sync-{index}",
                )

            agent = service._sessions[session_id].agent
            matches = [
                payee for payee in agent.store.payees_of("u_001")
                if payee.name == "小雅"
            ]

        self.assertEqual(len(matches), 1)

    def test_blank_contact_fields_do_not_erase_existing_payee_accounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service, session_id = self._service(temp_dir)
            service.update_profile_settings(
                session_id,
                {
                    "care_people": [{
                        "person_id": "CARE-SPOUSE",
                        "name": "林雅雯",
                        "relation": "配偶",
                        "phone": "",
                        "wechat": "",
                        "alipay": "",
                        "bank_card": "",
                    }]
                },
            )

            agent = service._sessions[session_id].agent
            payee = agent.providers.payees.find("u_001", "林雅雯")

        self.assertEqual(payee.wechat, "linyawen_0929")
        self.assertEqual(payee.alipay, "13900002222")
        self.assertEqual(payee.tail, "2222")


if __name__ == "__main__":
    unittest.main()
