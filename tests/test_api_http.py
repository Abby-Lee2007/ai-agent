"""HTTP API integration tests."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from finagent.api.server import create_server
from finagent.api.service import AgentSessionService
from tests.support import make_store
from tests.test_vision import make_xlsx


class ApiServerTestCase(unittest.TestCase):
    def setUp(self):
        self.service = AgentSessionService(make_store(), llm_mode="off")
        self.webhook_secret = "test-secret"
        self.server = create_server(
            self.service,
            host="127.0.0.1",
            port=0,
            webhook_secret=self.webhook_secret,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def post_json(self, path: str, payload: dict, headers: dict | None = None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=5)

    def get_json(self, path: str, headers: dict | None = None):
        request = urllib.request.Request(
            self.base_url + path,
            headers=headers or {},
            method="GET",
        )
        return urllib.request.urlopen(request, timeout=5)

    def delete_json(self, path: str, headers: dict | None = None):
        request = urllib.request.Request(
            self.base_url + path,
            headers=headers or {},
            method="DELETE",
        )
        return urllib.request.urlopen(request, timeout=5)

    def patch_json(self, path: str, payload: dict, headers: dict | None = None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
            method="PATCH",
        )
        return urllib.request.urlopen(request, timeout=5)

    def webhook_headers(self, payload: dict) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        digest = hmac.new(self.webhook_secret.encode("utf-8"), data, hashlib.sha256).hexdigest()
        return {"X-FinAgent-Signature": f"sha256={digest}"}

    def test_create_session_returns_201_and_session_payload(self):
        with self.post_json("/v1/sessions", {"user_id": "u_001", "channel": "h5"}) as response:
            body = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, 201)
        self.assertTrue(body["session_id"].startswith("SESSION-"))
        self.assertEqual(body["user_id"], "u_001")

    def test_message_returns_agent_reply(self):
        with self.post_json("/v1/sessions", {"user_id": "u_001", "channel": "h5"}) as response:
            session_id = json.loads(response.read().decode("utf-8"))["session_id"]

        with self.post_json(
            "/v1/messages",
            {"session_id": session_id, "message": "我有哪些卡", "request_id": "http-msg-001"},
        ) as response:
            body = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, 200)
        self.assertEqual(body["reply"]["intent"], "card.list")
        self.assertIn("信用卡", body["reply"]["text"])

    def test_stream_endpoint_returns_sse_events(self):
        with self.post_json("/v1/sessions", {"user_id": "u_001", "channel": "h5"}) as response:
            session_id = json.loads(response.read().decode("utf-8"))["session_id"]

        with self.post_json(
            "/v1/messages/stream",
            {"session_id": session_id, "message": "我还有多少钱", "request_id": "http-stream-001"},
        ) as response:
            content_type = response.headers.get("Content-Type", "")
            stream_text = response.read().decode("utf-8")

        self.assertTrue(content_type.startswith("text/event-stream"))
        self.assertIn("event: start", stream_text)
        self.assertIn("event: message", stream_text)
        self.assertIn('"intent": "account.balance"', stream_text)
        self.assertIn("event: done", stream_text)

    def test_root_serves_h5_chat_page(self):
        with urllib.request.urlopen(self.base_url + "/", timeout=5) as response:
            html = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.headers.get("Content-Type", ""))
        self.assertIn("<title>AI Banking Agent</title>", html)
        self.assertIn("/v1/messages/stream", html)

    def test_wecom_webhook_returns_reply_and_replays_same_message(self):
        payload = {
            "from_user": "wx-user-001",
            "content": "我有哪些卡",
            "msg_id": "wecom-msg-001",
        }
        headers = self.webhook_headers(payload)
        with self.post_json("/v1/channels/wecom/events", payload, headers=headers) as response:
            first = json.loads(response.read().decode("utf-8"))
        with self.post_json("/v1/channels/wecom/events", payload, headers=headers) as response:
            second = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, 200)
        self.assertEqual(first["channel"], "wecom")
        self.assertIn("信用卡", first["reply"]["text"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["session_id"], second["session_id"])

    def test_wecom_webhook_rejects_missing_signature(self):
        payload = {
            "from_user": "wx-user-002",
            "content": "我还有多少钱",
            "msg_id": "wecom-msg-unsigned",
        }
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.post_json("/v1/channels/wecom/events", payload)

        self.assertEqual(raised.exception.code, 401)

    def test_upload_endpoint_scans_bill_file(self):
        with self.post_json("/v1/sessions", {"user_id": "u_001", "channel": "h5"}) as response:
            session_id = json.loads(response.read().decode("utf-8"))["session_id"]
        bill = "2026-10-12 10:05 星巴克咖啡 -38.00\n2026-10-13 20:00 京东商城 -899.00"
        payload = {
            "session_id": session_id,
            "filename": "bill.txt",
            "data": base64.b64encode(bill.encode("utf-8")).decode("ascii"),
        }

        with self.post_json("/v1/uploads", payload) as response:
            body = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, 200)
        self.assertEqual(body["reply"]["intent"], "bill.scan_receipt")
        self.assertEqual(body["reply"]["kind"], "confirm")
        self.assertEqual(body["reply"]["data"]["count"], 2)

    def test_upload_with_stale_session_recreates_session(self):
        raw = (Path(__file__).resolve().parents[1] / "samples" / "bill_sample.txt").read_bytes()
        payload = {
            "session_id": "SESSION-stale-after-restart",
            "filename": "bill_sample.txt",
            "data": base64.b64encode(raw).decode("ascii"),
        }

        with self.post_json("/v1/uploads", payload) as response:
            self.assertEqual(response.status, 200)
            body = json.loads(response.read().decode("utf-8"))

        self.assertTrue(body["session_id"].startswith("SESSION-"))
        self.assertNotEqual(body["session_id"], "SESSION-stale-after-restart")
        self.assertEqual(body["reply"]["intent"], "bill.scan_receipt")
        self.assertEqual(body["reply"]["kind"], "confirm")

    def test_upload_parser_version_does_not_replay_old_csv_cache(self):
        with self.post_json("/v1/sessions", {"user_id": "u_001", "channel": "h5"}) as response:
            session_id = json.loads(response.read().decode("utf-8"))["session_id"]
        bill = "日期,商户,金额\n2026-10-20,星巴克咖啡,-38.00\n"
        raw = bill.encode("gb18030")
        old_request_id = "upload-" + hashlib.sha256(raw).hexdigest()
        self.service._idempotency[(session_id, old_request_id)] = {
            "session_id": session_id,
            "request_id": old_request_id,
            "reply": {
                "ok": True,
                "kind": "confirm",
                "text": "旧缓存：错误金额 920,261,007",
                "data": {"count": 55},
            },
            "replayed": False,
        }

        payload = {
            "session_id": session_id,
            "filename": "alipay.csv",
            "data": base64.b64encode(raw).decode("ascii"),
        }
        with self.post_json("/v1/uploads", payload) as response:
            self.assertEqual(response.status, 200)
            body = json.loads(response.read().decode("utf-8"))

        self.assertEqual(body["reply"]["data"]["count"], 1)
        self.assertNotIn("旧缓存", body["reply"]["text"])

    def test_upload_excel_file_scans_transactions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bill.xlsx"
            make_xlsx(path, [
                ["日期", "商户", "金额", "类别"],
                ["2026-10-12", "星巴克咖啡", -38.0, "餐饮"],
                ["2026-10-13", "滴滴出行", -42.5, "交通"],
            ])
            raw = path.read_bytes()

        with self.post_json("/v1/sessions", {"user_id": "u_001", "channel": "h5"}) as response:
            session_id = json.loads(response.read().decode("utf-8"))["session_id"]
        payload = {
            "session_id": session_id,
            "filename": "bill.xlsx",
            "data": base64.b64encode(raw).decode("ascii"),
        }
        with self.post_json("/v1/uploads", payload) as response:
            self.assertEqual(response.status, 200)
            body = json.loads(response.read().decode("utf-8"))

        self.assertEqual(body["reply"]["intent"], "bill.scan_receipt")
        self.assertEqual(body["reply"]["kind"], "confirm")
        self.assertEqual(body["reply"]["data"]["count"], 2)

    def test_upload_history_can_be_listed_and_deleted(self):
        with self.post_json("/v1/sessions", {"user_id": "u_001", "channel": "h5"}) as response:
            session_id = json.loads(response.read().decode("utf-8"))["session_id"]
        raw = (Path(__file__).resolve().parents[1] / "samples" / "bill_sample.txt").read_bytes()
        upload = {
            "session_id": session_id,
            "filename": "bill_sample.txt",
            "data": base64.b64encode(raw).decode("ascii"),
        }
        with self.post_json("/v1/uploads", upload) as response:
            self.assertEqual(response.status, 200)
        with self.post_json(
            "/v1/messages",
            {"session_id": session_id, "message": "确认", "request_id": "history-confirm"},
        ) as response:
            imported = json.loads(response.read().decode("utf-8"))
        self.assertEqual(imported["reply"]["data"]["imported"], 6)

        with self.get_json(f"/v1/uploads?session_id={session_id}") as response:
            history = json.loads(response.read().decode("utf-8"))
        self.assertEqual(len(history["uploads"]), 1)
        batch_id = history["uploads"][0]["batch_id"]

        with self.delete_json(f"/v1/uploads/{batch_id}?session_id={session_id}") as response:
            deleted = json.loads(response.read().decode("utf-8"))
        self.assertEqual(deleted["deleted_transactions"], 6)

        with self.get_json(f"/v1/uploads?session_id={session_id}") as response:
            history = json.loads(response.read().decode("utf-8"))
        self.assertEqual(history["uploads"], [])

    def test_profile_and_cards_can_be_viewed_and_edited(self):
        with self.post_json("/v1/sessions", {"user_id": "u_001", "channel": "h5"}) as response:
            session_id = json.loads(response.read().decode("utf-8"))["session_id"]
        with self.get_json(f"/v1/profile?session_id={session_id}") as response:
            profile = json.loads(response.read().decode("utf-8"))
        self.assertEqual(profile["user"]["name"], "陈晓明")
        card_id = profile["cards"][0]["card_id"]

        with self.patch_json("/v1/profile", {
            "session_id": session_id,
            "name": "新名字",
            "spouse_name": "新爱人",
            "spouse_birthday": "1995-12-01",
        }) as response:
            updated = json.loads(response.read().decode("utf-8"))
        self.assertEqual(updated["user"]["name"], "新名字")
        self.assertEqual(updated["preferences"]["spouse_name"], "新爱人")

        with self.patch_json(f"/v1/cards/{card_id}", {
            "session_id": session_id,
            "daily_limit": 12345,
            "online_enabled": False,
        }) as response:
            card = json.loads(response.read().decode("utf-8"))
        self.assertEqual(card["daily_limit"], 12345)
        self.assertFalse(card["online_enabled"])

        with self.post_json("/v1/cards", {
            "session_id": session_id,
            "kind": "virtual",
            "brand": "测试银行",
            "tail": "9999",
            "daily_limit": 3000,
        }) as response:
            created = json.loads(response.read().decode("utf-8"))
        self.assertEqual(created["tail"], "9999")

        with self.delete_json(
            f"/v1/cards/{created['card_id']}?session_id={session_id}"
        ) as response:
            deleted = json.loads(response.read().decode("utf-8"))
        self.assertEqual(deleted["deleted_card_id"], created["card_id"])

        with self.get_json(f"/v1/profile?session_id={session_id}") as response:
            after_delete = json.loads(response.read().decode("utf-8"))
        self.assertFalse(any(
            card["card_id"] == created["card_id"]
            for card in after_delete["cards"]
        ))

    def test_access_password_protects_api_but_not_health(self):
        service = AgentSessionService(make_store(), llm_mode="off")
        server = create_server(
            service,
            host="127.0.0.1",
            port=0,
            access_password="test-password",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        base_url = f"http://{host}:{port}"
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=5) as response:
                self.assertEqual(response.status, 200)
            data = json.dumps({"user_id": "u_001", "channel": "h5"}).encode("utf-8")
            request = urllib.request.Request(
                base_url + "/v1/sessions",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(raised.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
