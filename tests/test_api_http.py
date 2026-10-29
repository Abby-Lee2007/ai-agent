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
from typing import Optional
from unittest.mock import patch

from finagent.api.server import create_server
from finagent.api.service import AgentSessionService
from finagent.data.sqlite import SQLiteRepository
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

    def post_json(self, path: str, payload: dict, headers: Optional[dict] = None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=5)

    def get_json(self, path: str, headers: Optional[dict] = None):
        request = urllib.request.Request(
            self.base_url + path,
            headers=headers or {},
            method="GET",
        )
        return urllib.request.urlopen(request, timeout=5)

    def delete_json(self, path: str, headers: Optional[dict] = None):
        request = urllib.request.Request(
            self.base_url + path,
            headers=headers or {},
            method="DELETE",
        )
        return urllib.request.urlopen(request, timeout=5)

    def patch_json(self, path: str, payload: dict, headers: Optional[dict] = None):
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
        workflow = body["reply"]["data"]["workflow"]
        self.assertTrue(workflow["workflow_id"].startswith("WF-"))
        self.assertEqual(workflow["status"], "completed")

    def test_official_macro_endpoint_returns_sources_and_indicators(self):
        with self.get_json("/v1/official-data") as response:
            body = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, 200)
        self.assertEqual(body["data_class"], "official_public_data")
        self.assertIn("government_bond_10y_yield_pct", body["indicators"])
        self.assertGreaterEqual(len(body["sources"]), 3)
        self.assertTrue(all(source.get("url") for source in body["sources"]))

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

    def test_json_api_rejects_non_finite_numbers(self):
        request = urllib.request.Request(
            self.base_url + "/v1/forecast/simulate",
            data=b'{"session_id":"SESSION-test","months":NaN}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        body = json.loads(raised.exception.read().decode("utf-8"))

        self.assertEqual(raised.exception.code, 400)
        self.assertIn("JSON", body["message"])

    def test_forecast_simulation_and_event_center_endpoints(self):
        with self.post_json("/v1/sessions", {"user_id": "u_001", "channel": "h5"}) as response:
            session_id = json.loads(response.read().decode("utf-8"))["session_id"]
        with self.post_json(
            "/v1/forecast/simulate",
            {
                "session_id": session_id,
                "months": 3,
                "purchase_amount": 8000,
                "income_change_pct": -10,
                "extra_monthly_expense": 500,
                "investment_amount": 10000,
                "investment_return_pct": 3,
            },
        ) as response:
            forecast = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 200)
        self.assertTrue(forecast["reply"]["data"]["forecast"]["baseline_series"])
        self.assertLess(
            forecast["reply"]["data"]["forecast"]["scenario_delta_end"],
            0,
        )
        with self.get_json(f"/v1/events?session_id={session_id}") as response:
            events = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 200)
        self.assertGreaterEqual(len(events["events"]), 1)

    def test_market_overview_endpoint_returns_realtime_index_payload(self):
        session_id = self.service.create_session(
            user_id="u_001",
            channel="h5",
        )["session_id"]
        overview = {
            "available": True,
            "status": "live",
            "market_state": "已收盘",
            "observed_at": "2026-10-28 15:00:00",
            "indices": [{
                "symbol": "SH000001",
                "name": "上证指数",
                "price": 3681.47,
                "change": 33.17,
                "change_pct": 0.91,
                "timestamp": "2026-10-28 15:00:00",
                "source": "腾讯财经",
                "available": True,
            }],
            "turnover_100m_cny": 16284.87,
            "sources": ["腾讯财经"],
            "is_simulated": False,
        }
        with patch(
            "finagent.api.service.build_market_overview",
            return_value=overview,
        ):
            with self.get_json(
                f"/v1/market/overview?session_id={session_id}"
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 200)
        self.assertEqual(body["market_state"], "已收盘")
        self.assertEqual(body["indices"][0]["name"], "上证指数")
        self.assertFalse(body["is_simulated"])

    def test_transaction_detail_endpoint_returns_lifecycle(self):
        session_id = self.service.create_session(user_id="u_001", channel="h5")["session_id"]
        self.service.send_message(session_id, "给王强转1块，用微信", "txn-detail-1")
        self.service.send_message(session_id, "确认", "txn-detail-2")
        done = self.service.send_message(session_id, "246810", "txn-detail-3")
        txn_id = done["reply"]["data"]["txn_id"]
        with self.get_json(
            f"/v1/transactions/{txn_id}?session_id={session_id}"
        ) as response:
            body = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 200)
        self.assertEqual(body["transaction"]["lifecycle_status"], "completed")
        self.assertTrue(body["transaction"]["bank_reference"])

    def test_signed_sandbox_callback_updates_transaction_lifecycle(self):
        session_id = self.service.create_session(user_id="u_001", channel="h5")["session_id"]
        self.service.send_message(session_id, "给王强转1块，用微信", "sandbox-callback-1")
        self.service.send_message(session_id, "确认", "sandbox-callback-2")
        done = self.service.send_message(session_id, "246810", "sandbox-callback-3")
        execution_id = done["reply"]["data"]["sandbox_execution_id"]
        txn_id = done["reply"]["data"]["txn_id"]

        payload = {
            "session_id": session_id,
            "execution_id": execution_id,
            "status": "returned",
            "reason": "模拟支付渠道退回",
        }
        with self.post_json(
            "/v1/sandbox/callbacks",
            payload,
            headers=self.webhook_headers(payload),
        ) as response:
            body = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, 200)
        self.assertEqual(body["execution"]["status"], "returned")
        self.assertEqual(body["transaction"]["txn_id"], txn_id)
        self.assertEqual(body["transaction"]["lifecycle_status"], "returned")

        with self.get_json(f"/v1/sandbox/status?session_id={session_id}") as response:
            status = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 200)
        self.assertEqual(status["mode"], "sandbox")
        self.assertTrue(status["executions"])

        with self.get_json(f"/v1/market/status?session_id={session_id}") as response:
            market = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 200)
        self.assertIn("licensed", market)
        self.assertIn("public", market)

    def test_pending_receipt_can_be_loaded_and_corrected(self):
        session_id = self.service.create_session(user_id="u_001", channel="h5")["session_id"]
        sample = Path("samples/bill_sample.txt").read_bytes()
        with self.post_json(
            "/v1/uploads",
            {
                "session_id": session_id,
                "filename": "bill_sample.txt",
                "data": base64.b64encode(sample).decode("ascii"),
            },
        ) as response:
            uploaded = json.loads(response.read().decode("utf-8"))
        records = uploaded["reply"]["data"]["records"]
        self.assertTrue(records)
        records[0]["merchant"] = "人工核对商户"
        with self.patch_json(
            "/v1/receipts/pending",
            {"session_id": session_id, "records": records},
        ) as response:
            updated = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 200)
        self.assertEqual(updated["records"][0]["merchant"], "人工核对商户")
        self.assertIn("quality", updated)

    def test_root_serves_h5_chat_page(self):
        with urllib.request.urlopen(self.base_url + "/", timeout=5) as response:
            html = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.headers.get("Content-Type", ""))
        self.assertIn(
            "default-src 'self'",
            response.headers.get("Content-Security-Policy", ""),
        )
        self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")
        self.assertIn("<title>AI Banking Agent</title>", html)
        self.assertIn("/v1/messages/stream", html)

    def test_capability_catalog_endpoint_returns_business_navigation(self):
        with self.get_json("/v1/capabilities") as response:
            body = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, 200)
        self.assertEqual(len(body["categories"]), 6)
        self.assertEqual(body["categories"][0]["label"], "智能转账")
        self.assertIn(
            "crossscene.daily_brief",
            {action["intent"] for action in body["global_actions"]},
        )

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

    def test_upload_rejects_content_that_contradicts_extension(self):
        payload = {
            "filename": "fake.png",
            "data": base64.b64encode(b"not-a-real-image").decode("ascii"),
        }

        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.post_json("/v1/uploads", payload)
        body = json.loads(raised.exception.read().decode("utf-8"))

        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(body["error"], "file_type_mismatch")

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
        card_id = next(
            card["card_id"]
            for card in profile["cards"]
            if card["kind"] == "debit" and card["status"] == "active"
        )

        with self.patch_json("/v1/profile", {
            "session_id": session_id,
            "name": "新名字",
            "spouse_name": "新爱人",
            "spouse_birthday": "1995-12-01",
        }) as response:
            updated = json.loads(response.read().decode("utf-8"))
        self.assertEqual(updated["user"]["name"], "新名字")
        self.assertEqual(updated["preferences"]["spouse_name"], "新爱人")

        card_values = {
            "session_id": session_id,
            "daily_limit": 60000,
            "online_enabled": False,
        }
        try:
            response = self.patch_json(f"/v1/cards/{card_id}", card_values)
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 428)
            challenge = json.loads(exc.read().decode("utf-8"))
            response = self.patch_json(f"/v1/cards/{card_id}", {
                **card_values,
                "confirmation_token": challenge["confirmation_token"],
                "payment_password": "246810",
            })
        with response:
            card = json.loads(response.read().decode("utf-8"))
        self.assertEqual(card["daily_limit"], 60000)
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

    def test_profile_goals_can_be_edited(self):
        with self.post_json("/v1/sessions", {"user_id": "u_001", "channel": "h5"}) as response:
            session_id = json.loads(response.read().decode("utf-8"))["session_id"]
        with self.get_json(f"/v1/profile?session_id={session_id}") as response:
            profile = json.loads(response.read().decode("utf-8"))
        self.assertGreaterEqual(len(profile["goals"]), 1)
        goal = profile["goals"][0]

        with self.patch_json(f"/v1/goals/{goal['goal_id']}", {
            "session_id": session_id,
            "name": "毕业旅行基金",
            "target_amount": 12000,
            "deadline": "2027-01-31",
            "status": "active",
            "note": "毕业后旅行",
            "locked_amount": 1,
        }) as response:
            updated = json.loads(response.read().decode("utf-8"))

        self.assertEqual(updated["name"], "毕业旅行基金")
        self.assertEqual(updated["target_amount"], 12000.0)
        self.assertEqual(updated["deadline"], "2027-01-31")
        self.assertEqual(updated["locked_amount"], goal["locked_amount"])

    def test_profile_goals_can_be_created_and_deleted(self):
        with self.post_json("/v1/sessions", {"user_id": "u_001", "channel": "h5"}) as response:
            session_id = json.loads(response.read().decode("utf-8"))["session_id"]

        with self.post_json("/v1/goals", {
            "session_id": session_id,
            "name": "毕业旅行基金",
            "target_amount": 6000,
            "deadline": "2027-06-30",
            "status": "active",
            "note": "毕业后出发",
        }) as response:
            created = json.loads(response.read().decode("utf-8"))

        self.assertEqual(created["locked_amount"], 0.0)
        self.assertEqual(created["source"], "user:profile")

        with self.delete_json(
            f"/v1/goals/{created['goal_id']}?session_id={session_id}"
        ) as response:
            deleted = json.loads(response.read().decode("utf-8"))

        self.assertEqual(deleted["deleted_goal_id"], created["goal_id"])
        self.assertEqual(deleted["released_amount"], 0.0)

        with self.get_json(f"/v1/profile?session_id={session_id}") as response:
            profile = json.loads(response.read().decode("utf-8"))
        self.assertFalse(any(
            goal["goal_id"] == created["goal_id"]
            for goal in profile["goals"]
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

    def test_api_rate_limit_returns_429_but_health_remains_available(self):
        service = AgentSessionService(make_store(), llm_mode="off")
        server = create_server(
            service,
            host="127.0.0.1",
            port=0,
            rate_limit_per_minute=2,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        base_url = f"http://{host}:{port}"
        try:
            for _ in range(2):
                with urllib.request.urlopen(base_url + "/v1/capabilities", timeout=5):
                    pass
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(base_url + "/v1/capabilities", timeout=5)
            self.assertEqual(raised.exception.code, 429)
            with urllib.request.urlopen(base_url + "/health", timeout=5) as response:
                self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


class SessionTokenApiTests(unittest.TestCase):
    def setUp(self):
        self.webhook_secret = "token-test-secret"
        self.servers = []

    def tearDown(self):
        for server, thread in reversed(self.servers):
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def start_server(
        self,
        service: AgentSessionService,
        access_password: str = "",
    ) -> str:
        server = create_server(
            service,
            host="127.0.0.1",
            port=0,
            webhook_secret=self.webhook_secret,
            access_password=access_password,
            enforce_session_tokens=True,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        host, port = server.server_address
        return f"http://{host}:{port}"

    @staticmethod
    def request_json(
        base_url: str,
        path: str,
        payload: Optional[dict] = None,
        headers: Optional[dict] = None,
        method: str = "GET",
    ):
        data = None
        request_headers = dict(headers or {})
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            base_url + path,
            data=data,
            headers=request_headers,
            method=method,
        )
        return urllib.request.urlopen(request, timeout=5)

    def create_session(self, base_url: str) -> dict:
        with self.request_json(
            base_url,
            "/v1/sessions",
            {"user_id": "u_001", "channel": "h5"},
            method="POST",
        ) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_session_token_is_required_for_profile_access(self):
        base_url = self.start_server(
            AgentSessionService(make_store(), llm_mode="off")
        )
        created = self.create_session(base_url)
        session_id = created["session_id"]
        token = created["session_token"]
        path = f"/v1/profile?session_id={session_id}"

        with self.assertRaises(urllib.error.HTTPError) as missing:
            self.request_json(base_url, path)
        self.assertEqual(missing.exception.code, 403)

        with self.assertRaises(urllib.error.HTTPError) as wrong:
            self.request_json(
                base_url,
                path,
                headers={"X-Session-Token": "not-the-token"},
            )
        self.assertEqual(wrong.exception.code, 403)

        with self.request_json(
            base_url,
            path,
            headers={"X-Session-Token": token},
        ) as response:
            profile = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 200)
        self.assertEqual(profile["user"]["user_id"], "u_001")

    def test_upload_without_session_returns_new_session_token(self):
        base_url = self.start_server(
            AgentSessionService(make_store(), llm_mode="off")
        )
        bill = "2026-10-12 10:05 星巴克咖啡 -38.00"
        payload = {
            "filename": "bill.txt",
            "data": base64.b64encode(bill.encode("utf-8")).decode("ascii"),
        }

        with self.request_json(
            base_url,
            "/v1/uploads",
            payload,
            method="POST",
        ) as response:
            body = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, 200)
        self.assertTrue(body["session_id"].startswith("SESSION-"))
        self.assertTrue(body["session_token"])
        self.assertEqual(body["reply"]["intent"], "bill.scan_receipt")

    def test_signed_sandbox_callback_uses_webhook_auth_not_session_token(self):
        service = AgentSessionService(make_store(), llm_mode="off")
        base_url = self.start_server(service)
        created = self.create_session(base_url)
        session_id = created["session_id"]
        token_header = {"X-Session-Token": created["session_token"]}
        for request_id, message in (
            ("token-transfer-1", "给王强转1块，用微信"),
            ("token-transfer-2", "确认"),
            ("token-transfer-3", "246810"),
        ):
            with self.request_json(
                base_url,
                "/v1/messages",
                {
                    "session_id": session_id,
                    "message": message,
                    "request_id": request_id,
                },
                headers=token_header,
                method="POST",
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
        execution_id = result["reply"]["data"]["sandbox_execution_id"]
        payload = {
            "session_id": session_id,
            "execution_id": execution_id,
            "status": "returned",
        }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        signature = hmac.new(
            self.webhook_secret.encode("utf-8"),
            raw,
            hashlib.sha256,
        ).hexdigest()
        request = urllib.request.Request(
            base_url + "/v1/sandbox/callbacks",
            data=raw,
            headers={
                "Content-Type": "application/json",
                "X-FinAgent-Signature": f"sha256={signature}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            callback = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, 200)
        self.assertEqual(callback["execution"]["status"], "returned")

    def test_session_token_hash_survives_server_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent.db"
            repository = SQLiteRepository(db_path)
            first = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=repository,
            )
            base_url = self.start_server(first)
            created = self.create_session(base_url)

            second = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            restarted_url = self.start_server(second)
            with self.request_json(
                restarted_url,
                f"/v1/profile?session_id={created['session_id']}",
                headers={"X-Session-Token": created["session_token"]},
            ) as response:
                profile = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, 200)
        self.assertEqual(profile["user"]["user_id"], "u_001")

    def test_webhook_signature_works_without_web_access_password(self):
        service = AgentSessionService(make_store(), llm_mode="off")
        base_url = self.start_server(service, access_password="web-password")
        payload = {
            "from_user": "wecom-access-password-test",
            "content": "我有哪些卡",
            "msg_id": "wecom-access-password-msg",
        }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        signature = hmac.new(
            self.webhook_secret.encode("utf-8"),
            raw,
            hashlib.sha256,
        ).hexdigest()
        request = urllib.request.Request(
            base_url + "/v1/channels/wecom/events",
            data=raw,
            headers={
                "Content-Type": "application/json",
                "X-FinAgent-Signature": f"sha256={signature}",
            },
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, 200)
        self.assertEqual(body["channel"], "wecom")

    def test_webhooks_fail_closed_when_no_secret_is_configured(self):
        server = create_server(
            AgentSessionService(make_store(), llm_mode="off"),
            host="127.0.0.1",
            port=0,
            enforce_session_tokens=True,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        try:
            request = urllib.request.Request(
                f"http://{host}:{port}/v1/sandbox/callbacks",
                data=b"{}",
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
