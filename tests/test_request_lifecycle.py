"""End-to-end request lifecycle and idempotency tests for H-01."""
from __future__ import annotations

import json
import hashlib
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from finagent.agent.core import BankingAgent
from finagent.api.server import create_server
from finagent.api.service import AgentSessionService, RequestNotFound
from finagent.data.sqlite import SQLiteRepository
from tests.support import make_store


class RequestLifecycleTestCase(unittest.TestCase):
    def make_service(
        self,
        db_path: Path,
        *,
        processing_stale_seconds: int = 300,
    ) -> AgentSessionService:
        return AgentSessionService(
            make_store(),
            llm_mode="off",
            store_factory=make_store,
            repository=SQLiteRepository(db_path),
            processing_stale_seconds=processing_stale_seconds,
        )

    def test_completed_request_replays_original_result_after_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent.db"
            first = self.make_service(db_path)
            session_id = first.create_session()["session_id"]
            completed = first.send_message(
                session_id,
                "我还有多少钱",
                request_id="restart-query-1",
            )

            second = self.make_service(db_path)
            queried = second.get_request_status(
                session_id,
                "restart-query-1",
            )
            replayed = second.send_message(
                session_id,
                "我还有多少钱",
                request_id="restart-query-1",
            )

        self.assertEqual(queried["request_status"], "completed")
        self.assertTrue(queried["replayed"])
        self.assertEqual(
            queried["reply"]["text"],
            completed["reply"]["text"],
        )
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["reply"]["text"], completed["reply"]["text"])

    def test_same_request_id_with_different_fingerprint_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.make_service(Path(temp_dir) / "agent.db")
            session_id = service.create_session()["session_id"]
            service.send_message(session_id, "我还有多少钱", "fingerprint-1")

            with self.assertRaisesRegex(
                ValueError,
                "同一 request_id 不能用于不同消息",
            ):
                service.send_message(session_id, "我有哪些卡", "fingerprint-1")

    def test_atomic_claim_across_service_instances_executes_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent.db"
            first = self.make_service(db_path)
            second = self.make_service(db_path)
            session_id = first.create_session()["session_id"]
            entered = threading.Event()
            release = threading.Event()
            calls = []
            first_result = []
            errors = []
            original_handle = BankingAgent.handle

            def blocking_handle(agent, text, *, request_id=""):
                if text == "我还有多少钱":
                    calls.append(1)
                    entered.set()
                    release.wait(timeout=5)
                return original_handle(agent, text, request_id=request_id)

            def submit(service):
                try:
                    first_result.append(
                        service.send_message(
                            session_id,
                            "我还有多少钱",
                            "cross-process-claim-1",
                        )
                    )
                except Exception as exc:  # pragma: no cover - assertion captures it
                    errors.append(exc)

            with patch.object(BankingAgent, "handle", blocking_handle):
                worker = threading.Thread(target=submit, args=(first,))
                worker.start()
                self.assertTrue(entered.wait(timeout=3))
                processing = second.send_message(
                    session_id,
                    "我还有多少钱",
                    "cross-process-claim-1",
                )
                with self.assertRaises(ValueError):
                    second.send_message(
                        session_id,
                        "我有哪些卡",
                        "cross-process-claim-1",
                    )
                release.set()
                worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(calls), 1)
            self.assertEqual(processing["request_status"], "processing")
            self.assertEqual(
                first_result[0]["request_status"],
                "completed",
            )
            replayed = second.send_message(
                session_id,
                "我还有多少钱",
                "cross-process-claim-1",
            )
            self.assertTrue(replayed["replayed"])
            self.assertEqual(len(calls), 1)

    def test_exception_marks_outcome_unknown_and_retry_does_not_execute(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.make_service(Path(temp_dir) / "agent.db")
            session_id = service.create_session()["session_id"]
            agent = service._sessions[session_id].agent
            call_count = 0

            def failing_handle(_text, *, request_id=""):
                nonlocal call_count
                call_count += 1
                raise RuntimeError("simulated worker failure")

            with patch.object(agent, "handle", failing_handle):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated worker failure",
                ):
                    service.send_message(
                        session_id,
                        "我还有多少钱",
                        "exception-unknown-1",
                    )

            state = service.get_request_status(
                session_id,
                "exception-unknown-1",
            )
            retried = service.send_message(
                session_id,
                "我还有多少钱",
                "exception-unknown-1",
            )

        self.assertEqual(state["request_status"], "outcome_unknown")
        self.assertEqual(retried["request_status"], "outcome_unknown")
        self.assertEqual(call_count, 1)
        self.assertNotIn("reply", retried)

    def test_stale_processing_claim_becomes_unknown_without_execution(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent.db"
            first = self.make_service(
                db_path,
                processing_stale_seconds=30,
            )
            session_id = first.create_session()["session_id"]
            first.repository.claim_idempotency(
                session_id,
                "stale-claim-1",
                hashlib.sha256("我还有多少钱".encode("utf-8")).hexdigest(),
                "2000-01-01T00:00:00+00:00",
                "old-worker",
            )
            called = []
            restarted = self.make_service(
                db_path,
                processing_stale_seconds=30,
            )

            with patch.object(
                BankingAgent,
                "handle",
                side_effect=lambda text: called.append(text),
            ):
                result = restarted.send_message(
                    session_id,
                    "我还有多少钱",
                    "stale-claim-1",
                )

        self.assertEqual(result["request_status"], "outcome_unknown")
        self.assertEqual(called, [])

    def test_unknown_request_raises_lookup_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.make_service(Path(temp_dir) / "agent.db")
            session_id = service.create_session()["session_id"]
            with self.assertRaises(RequestNotFound):
                service.get_request_status(session_id, "missing-request")

    def test_request_status_query_requires_session_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.make_service(Path(temp_dir) / "agent.db")
            created = service.create_session()
            session_id = created["session_id"]
            service.send_message(
                session_id,
                "我还有多少钱",
                "protected-query-1",
            )
            server = create_server(
                service,
                host="127.0.0.1",
                port=0,
                enforce_session_tokens=True,
            )
            server_thread = threading.Thread(
                target=server.serve_forever,
                daemon=True,
            )
            server_thread.start()
            host, port = server.server_address
            url = (
                f"http://{host}:{port}/v1/requests/protected-query-1"
                f"?session_id={session_id}"
            )
            try:
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(url, timeout=5)
                self.assertEqual(raised.exception.code, 403)

                request = urllib.request.Request(
                    url,
                    headers={
                        "X-Session-Token": created["session_token"],
                    },
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    body = json.loads(response.read().decode("utf-8"))
                self.assertEqual(body["request_status"], "completed")
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=3)

    def test_sse_disconnect_after_execution_recovers_original_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.make_service(Path(temp_dir) / "agent.db")
            session_id = service.create_session()["session_id"]
            service.send_message(
                session_id,
                "给王强转1块，用微信",
                "sse-disconnect-init",
            )
            service.send_message(
                session_id,
                "确认",
                "sse-disconnect-confirm",
            )
            agent = service._sessions[session_id].agent
            card = agent.providers.cards.find("u_001", "工资卡")
            balance_before = card.balance

            server = create_server(service, host="127.0.0.1", port=0)
            server_thread = threading.Thread(
                target=server.serve_forever,
                daemon=True,
            )
            server_thread.start()
            host, port = server.server_address
            entered = threading.Event()
            release = threading.Event()
            original_handle = BankingAgent.handle

            def blocking_handle(current_agent, text, *, request_id=""):
                if text == "246810":
                    entered.set()
                    release.wait(timeout=5)
                return original_handle(
                    current_agent,
                    text,
                    request_id=request_id,
                )

            request_body = json.dumps({
                "session_id": session_id,
                "message": "246810",
                "request_id": "sse-disconnect-password",
            }).encode("utf-8")
            request = urllib.request.Request(
                f"http://{host}:{port}/v1/messages/stream",
                data=request_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            response_holder = {}
            request_errors = []

            def submit_stream():
                try:
                    response_holder["response"] = urllib.request.urlopen(
                        request,
                        timeout=5,
                    )
                except Exception as exc:  # pragma: no cover - assertion captures it
                    request_errors.append(exc)

            request_thread = threading.Thread(
                target=submit_stream,
                daemon=True,
            )

            try:
                with patch.object(BankingAgent, "handle", blocking_handle):
                    request_thread.start()
                    self.assertTrue(entered.wait(timeout=3))
                    deadline = time.time() + 3
                    while (
                        "response" not in response_holder
                        and time.time() < deadline
                    ):
                        time.sleep(0.02)
                    self.assertIn("response", response_holder)
                    response_holder["response"].close()
                    release.set()
                request_thread.join(timeout=5)
                self.assertEqual(request_errors, [])

                deadline = time.time() + 5
                state = None
                while time.time() < deadline:
                    try:
                        state = service.get_request_status(
                            session_id,
                            "sse-disconnect-password",
                        )
                        if state["request_status"] == "completed":
                            break
                    except RequestNotFound:
                        pass
                    time.sleep(0.05)
                self.assertIsNotNone(state)
                self.assertEqual(state["request_status"], "completed")
                self.assertTrue(state["replayed"])

                replayed = service.send_message(
                    session_id,
                    "246810",
                    "sse-disconnect-password",
                )
                self.assertTrue(replayed["replayed"])
                self.assertAlmostEqual(card.balance, balance_before - 1.0)

                url = (
                    f"http://{host}:{port}/v1/requests/"
                    f"sse-disconnect-password?session_id={session_id}"
                )
                with urllib.request.urlopen(url, timeout=5) as response:
                    http_state = json.loads(
                        response.read().decode("utf-8")
                    )
                self.assertEqual(http_state["request_status"], "completed")
                self.assertTrue(http_state["replayed"])

                missing_url = (
                    f"http://{host}:{port}/v1/requests/not-found"
                    f"?session_id={session_id}"
                )
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(missing_url, timeout=5)
                self.assertEqual(raised.exception.code, 404)
            finally:
                release.set()
                response = response_holder.get("response")
                if response is not None:
                    response.close()
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
