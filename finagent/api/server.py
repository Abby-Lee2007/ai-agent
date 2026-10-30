"""Zero-dependency HTTP API for the banking agent."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional, Type
from urllib.parse import parse_qs, unquote, urlparse

from finagent.api.service import (
    AgentSessionService,
    RequestNotFound,
    SessionAuthError,
    SessionNotFound,
    StepUpRequired,
)
from finagent.data.official_sources import OfficialDataError
from finagent.data.sqlite import OptimisticConcurrencyError
from finagent.nlu.capability_catalog import capability_catalog_payload

ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = ROOT / "web"
UPLOAD_ROOT = ROOT / "runtime_uploads"
MAX_BODY_BYTES = 12_000_000
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
UPLOAD_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif",
    ".txt", ".csv", ".xlsx", ".xlsm",
}
UPLOAD_PARSER_VERSION = "3"
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)
SESSION_TOKEN_EXEMPT_POST_PATHS = {
    "/v1/sessions",
    "/v1/official-data/refresh",
    "/v1/sandbox/callbacks",
    "/v1/channels/wecom/events",
}
WEBHOOK_CALLBACK_PATHS = {
    "/v1/sandbox/callbacks",
    "/v1/channels/wecom/events",
}


def _upload_matches_suffix(suffix: str, raw: bytes) -> bool:
    """Reject files whose content clearly contradicts the claimed extension."""
    if suffix == ".png":
        return raw.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return raw.startswith(b"\xff\xd8\xff")
    if suffix == ".gif":
        return raw.startswith((b"GIF87a", b"GIF89a"))
    if suffix == ".webp":
        return (
            len(raw) >= 12
            and raw.startswith(b"RIFF")
            and raw[8:12] == b"WEBP"
        )
    if suffix == ".bmp":
        return raw.startswith(b"BM")
    if suffix in {".xlsx", ".xlsm"}:
        return raw.startswith(b"PK\x03\x04")
    if suffix in {".txt", ".csv"}:
        return b"\x00" not in raw[:4096]
    return False


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"不支持的 JSON 数值：{value}")


class ApiHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args: Any, rate_limit_per_minute: int = 120, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.rate_limit_per_minute = max(1, int(rate_limit_per_minute))
        self.rate_limit_lock = threading.RLock()
        self.rate_limit_windows: Dict[str, deque] = defaultdict(deque)


class ApiRequestHandler(BaseHTTPRequestHandler):
    server_version = "FinAgentAPI/0.1"
    protocol_version = "HTTP/1.1"
    service: AgentSessionService
    webhook_secret: str = ""
    access_password: str = ""
    enforce_session_tokens: bool = False
    _raw_body: bytes = b""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._serve_file(self.server.web_root / "index.html", "text/html; charset=utf-8")  # type: ignore[attr-defined]
            return
        if path == "/health":
            self._send_json(200, {"ok": True, "service": "AI Banking Agent"})
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self._within_rate_limit():
            self._send_json(429, {"error": "rate_limited", "message": "请求过于频繁，请稍后重试"})
            return
        query_session_id = parse_qs(parsed.query).get("session_id", [""])[0]
        if (
            path not in {"/v1/capabilities", "/v1/official-data"}
            and query_session_id
            and not self._require_session(query_session_id)
        ):
            return
        if path == "/v1/capabilities":
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized", "message": "访问口令无效"})
                return
            self._send_json(200, capability_catalog_payload())
            return
        if path == "/v1/uploads":
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized", "message": "访问口令无效"})
                return
            session_id = parse_qs(parsed.query).get("session_id", [""])[0]
            if not session_id:
                self._send_json(400, {"error": "bad_request", "message": "session_id 不能为空"})
                return
            try:
                self._send_json(200, self.service.list_uploads(session_id))
            except SessionNotFound as exc:
                self._send_json(404, {"error": "session_not_found", "message": f"会话不存在：{exc}"})
            return
        if path == "/v1/profile":
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized", "message": "访问口令无效"})
                return
            session_id = parse_qs(parsed.query).get("session_id", [""])[0]
            if not session_id:
                self._send_json(400, {"error": "bad_request", "message": "session_id 不能为空"})
                return
            try:
                self._send_json(200, self.service.get_profile_settings(session_id))
            except SessionNotFound as exc:
                self._send_json(404, {"error": "session_not_found", "message": f"会话不存在：{exc}"})
            return
        if path == "/v1/events":
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized", "message": "访问口令无效"})
                return
            session_id = parse_qs(parsed.query).get("session_id", [""])[0]
            if not session_id:
                self._send_json(400, {"error": "bad_request", "message": "session_id 不能为空"})
                return
            try:
                self._send_json(200, self.service.get_financial_events(session_id))
            except SessionNotFound as exc:
                self._send_json(404, {"error": "session_not_found", "message": f"会话不存在：{exc}"})
            return
        if path == "/v1/sandbox/status":
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized", "message": "访问口令无效"})
                return
            session_id = parse_qs(parsed.query).get("session_id", [""])[0]
            if not session_id:
                self._send_json(400, {"error": "bad_request", "message": "session_id 不能为空"})
                return
            try:
                self._send_json(200, self.service.get_sandbox_status(session_id))
            except SessionNotFound as exc:
                self._send_json(404, {"error": "session_not_found", "message": f"会话不存在：{exc}"})
            return
        if path == "/v1/market/status":
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized", "message": "访问口令无效"})
                return
            session_id = parse_qs(parsed.query).get("session_id", [""])[0]
            if not session_id:
                self._send_json(400, {"error": "bad_request", "message": "session_id 不能为空"})
                return
            try:
                self._send_json(200, self.service.get_market_status(session_id))
            except SessionNotFound as exc:
                self._send_json(404, {"error": "session_not_found", "message": f"会话不存在：{exc}"})
            return
        if path == "/v1/market/overview":
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized", "message": "访问口令无效"})
                return
            session_id = parse_qs(parsed.query).get("session_id", [""])[0]
            if not session_id:
                self._send_json(400, {"error": "bad_request", "message": "session_id 不能为空"})
                return
            try:
                self._send_json(200, self.service.get_market_overview(session_id))
            except SessionNotFound as exc:
                self._send_json(404, {"error": "session_not_found", "message": f"会话不存在：{exc}"})
            return
        if path == "/v1/official-data":
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized", "message": "访问口令无效"})
                return
            try:
                self._send_json(200, self.service.get_official_data())
            except OfficialDataError as exc:
                self._send_json(
                    502,
                    {"error": "official_data_unavailable", "message": str(exc)},
                )
            return
        if path == "/v1/receipts/pending":
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized", "message": "访问口令无效"})
                return
            session_id = parse_qs(parsed.query).get("session_id", [""])[0]
            if not session_id:
                self._send_json(400, {"error": "bad_request", "message": "session_id 不能为空"})
                return
            try:
                self._send_json(200, self.service.get_pending_receipt(session_id))
            except SessionNotFound as exc:
                self._send_json(404, {"error": "session_not_found", "message": f"会话不存在：{exc}"})
            return
        request_prefix = "/v1/requests/"
        if path.startswith(request_prefix):
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized", "message": "访问口令无效"})
                return
            request_id = unquote(path[len(request_prefix):]).strip("/")
            session_id = parse_qs(parsed.query).get("session_id", [""])[0]
            if not request_id or not session_id:
                self._send_json(
                    400,
                    {
                        "error": "bad_request",
                        "message": "session_id 和 request_id 不能为空",
                    },
                )
                return
            try:
                self._send_json(
                    200,
                    self.service.get_request_status(session_id, request_id),
                )
            except RequestNotFound as exc:
                self._send_json(
                    404,
                    {
                        "error": "request_not_found",
                        "request_status": "not_found",
                        "request_id": str(exc),
                    },
                )
            except SessionNotFound as exc:
                self._send_json(404, {"error": "session_not_found", "message": f"会话不存在：{exc}"})
            return
        transaction_prefix = "/v1/transactions/"
        if path.startswith(transaction_prefix):
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized", "message": "访问口令无效"})
                return
            txn_id = unquote(path[len(transaction_prefix):]).strip("/")
            session_id = parse_qs(parsed.query).get("session_id", [""])[0]
            if not txn_id or not session_id:
                self._send_json(400, {"error": "bad_request", "message": "session_id 和 txn_id 不能为空"})
                return
            try:
                self._send_json(200, self.service.get_transaction_detail(session_id, txn_id))
            except SessionNotFound as exc:
                self._send_json(404, {"error": "session_not_found", "message": f"会话不存在：{exc}"})
            except ValueError as exc:
                self._send_json(404, {"error": "resource_not_found", "message": str(exc)})
            return
        self._send_json(404, {"error": "not_found", "message": f"未知资源：{path}"})

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._within_rate_limit():
            self._send_json(429, {"error": "rate_limited", "message": "请求过于频繁，请稍后重试"})
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized", "message": "访问口令无效"})
            return
        session_id = parse_qs(parsed.query).get("session_id", [""])[0]
        if session_id and not self._require_session(session_id):
            return
        try:
            upload_prefix = "/v1/uploads/"
            goal_prefix = "/v1/goals/"
            card_prefix = "/v1/cards/"
            if parsed.path.startswith(upload_prefix):
                item_id = unquote(parsed.path[len(upload_prefix):]).strip("/")
                if not item_id or not session_id:
                    raise ValueError("session_id 和 upload_id 不能为空")
                self._send_json(200, self.service.delete_upload(session_id, item_id))
                return
            if parsed.path.startswith(goal_prefix):
                item_id = unquote(parsed.path[len(goal_prefix):]).strip("/")
                if not item_id or not session_id:
                    raise ValueError("session_id 和 goal_id 不能为空")
                self._send_json(200, self.service.delete_goal_settings(session_id, item_id))
                return
            if parsed.path.startswith(card_prefix):
                item_id = unquote(parsed.path[len(card_prefix):]).strip("/")
                if not item_id or not session_id:
                    raise ValueError("session_id 和 card_id 不能为空")
                self._send_json(200, self.service.delete_card_settings(session_id, item_id))
                return
            self._send_json(404, {"error": "not_found", "message": f"未知接口：{parsed.path}"})
        except SessionNotFound as exc:
            self._send_json(404, {"error": "session_not_found", "message": f"会话不存在：{exc}"})
        except OptimisticConcurrencyError as exc:
            self._send_json(409, {"error": "session_conflict", "message": str(exc)})
        except ValueError as exc:
            self._send_json(404, {"error": "resource_not_found", "message": str(exc)})

    def do_PATCH(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._within_rate_limit():
            self._send_json(429, {"error": "rate_limited", "message": "请求过于频繁，请稍后重试"})
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized", "message": "访问口令无效"})
            return
        try:
            payload = self._read_json()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("session_id 不能为空")
            if not self._require_session(session_id):
                return
            if parsed.path == "/v1/profile":
                values = {key: value for key, value in payload.items() if key != "session_id"}
                self._send_json(200, self.service.update_profile_settings(session_id, values))
                return
            if parsed.path == "/v1/receipts/pending":
                self._send_json(
                    200,
                    self.service.update_pending_receipt(
                        session_id,
                        payload.get("records") or [],
                    ),
                )
                return
            goal_prefix = "/v1/goals/"
            if parsed.path.startswith(goal_prefix):
                goal_id = unquote(parsed.path[len(goal_prefix):]).strip("/")
                if not goal_id:
                    raise ValueError("goal_id 不能为空")
                values = {key: value for key, value in payload.items() if key != "session_id"}
                self._send_json(200, self.service.update_goal_settings(
                    session_id,
                    goal_id,
                    values,
                ))
                return
            card_prefix = "/v1/cards/"
            if parsed.path.startswith(card_prefix):
                card_id = unquote(parsed.path[len(card_prefix):]).strip("/")
                if not card_id:
                    raise ValueError("card_id 不能为空")
                values = {key: value for key, value in payload.items() if key != "session_id"}
                self._send_json(200, self.service.update_card_settings(
                    session_id,
                    card_id,
                    values,
                ))
                return
            self._send_json(404, {"error": "not_found", "message": f"未知接口：{parsed.path}"})
        except StepUpRequired as exc:
            self._send_json(428, {
                "error": "step_up_required",
                "message": str(exc),
                **exc.challenge,
            })
        except SessionNotFound as exc:
            self._send_json(404, {"error": "session_not_found", "message": f"会话不存在：{exc}"})
        except OptimisticConcurrencyError as exc:
            self._send_json(409, {"error": "session_conflict", "message": str(exc)})
        except ValueError as exc:
            self._send_json(400, {"error": "bad_request", "message": str(exc)})
        except Exception as exc:
            self._send_json(500, {"error": "internal_error", "message": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._within_rate_limit():
            self._send_json(429, {"error": "rate_limited", "message": "请求过于频繁，请稍后重试"})
            return
        if path not in WEBHOOK_CALLBACK_PATHS and not self._authorized():
            self._send_json(401, {"error": "unauthorized", "message": "访问口令无效"})
            return
        try:
            payload = self._read_json()
            session_id = str(payload.get("session_id") or "")
            if (
                path not in SESSION_TOKEN_EXEMPT_POST_PATHS
                and session_id
                and not self._require_session(session_id)
            ):
                return
            if path == "/v1/sessions":
                body = self.service.create_session(
                    user_id=str(payload.get("user_id") or "u_001"),
                    channel=str(payload.get("channel") or "h5"),
                    external_user_id=str(payload.get("external_user_id") or ""),
                )
                self._send_json(201, body)
                return
            if path == "/v1/messages":
                body = self.service.send_message(
                    session_id=str(payload.get("session_id") or ""),
                    message=str(payload.get("message") or ""),
                    request_id=str(payload.get("request_id") or ""),
                )
                self._send_json(200, body)
                return
            if path == "/v1/messages/stream":
                self._stream_message(payload)
                return
            if path == "/v1/forecast/simulate":
                self._send_json(
                    200,
                    self.service.simulate_forecast(
                        str(payload.get("session_id") or ""),
                        payload,
                    ),
                )
                return
            if path == "/v1/official-data/refresh":
                try:
                    self._send_json(
                        200,
                        self.service.get_official_data(refresh=True),
                    )
                except OfficialDataError as exc:
                    self._send_json(
                        502,
                        {"error": "official_data_refresh_failed", "message": str(exc)},
                    )
                return
            if path == "/v1/events/action":
                self._send_json(
                    200,
                    self.service.update_financial_event(
                        str(payload.get("session_id") or ""),
                        str(payload.get("event_id") or ""),
                        str(payload.get("action") or "list"),
                    ),
                )
                return
            if path == "/v1/sandbox/callbacks":
                if not self._valid_webhook_signature():
                    self._send_json(
                        401,
                        {"error": "invalid_signature", "message": "sandbox callback 签名校验失败"},
                    )
                    return
                self._send_json(
                    200,
                    self.service.handle_sandbox_callback(
                        str(payload.get("session_id") or ""),
                        payload,
                    ),
                )
                return
            if path == "/v1/channels/wecom/events":
                if not self._valid_webhook_signature():
                    self._send_json(
                        401,
                        {"error": "invalid_signature", "message": "webhook 签名校验失败"},
                    )
                    return
                body = self.service.handle_channel_message(
                    channel="wecom",
                    external_user_id=str(payload.get("from_user") or ""),
                    message=str(payload.get("content") or payload.get("message") or ""),
                    message_id=str(payload.get("msg_id") or payload.get("message_id") or ""),
                    user_id=str(payload.get("user_id") or "u_001"),
                )
                self._send_json(200, body)
                return
            if path in {"/v1/uploads", "/api/upload"}:
                self._upload(payload)
                return
            if path == "/v1/goals":
                session_id = str(payload.get("session_id") or "")
                if not session_id:
                    raise ValueError("session_id 不能为空")
                self._send_json(201, self.service.create_goal_settings(session_id, payload))
                return
            if path == "/v1/cards":
                self._send_json(200, self.service.create_card_settings(
                    str(payload.get("session_id") or ""),
                    payload,
                ))
                return
            self._send_json(404, {"error": "not_found", "message": f"未知接口：{path}"})
        except SessionNotFound as exc:
            self._send_json(404, {"error": "session_not_found", "message": f"会话不存在：{exc}"})
        except OptimisticConcurrencyError as exc:
            self._send_json(409, {"error": "session_conflict", "message": str(exc)})
        except ValueError as exc:
            self._send_json(400, {"error": "bad_request", "message": str(exc)})
        except OfficialDataError as exc:
            self._send_json(502, {"error": "official_data_unavailable", "message": str(exc)})
        except Exception as exc:
            self._send_json(500, {"error": "internal_error", "message": str(exc)})

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        self._raw_body = raw
        try:
            data = json.loads(
                raw.decode("utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体必须是 UTF-8 JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON 顶层必须是对象")
        return data

    def _valid_webhook_signature(self) -> bool:
        if not self.webhook_secret:
            return False
        supplied = self.headers.get("X-FinAgent-Signature", "")
        expected = "sha256=" + hmac.new(
            self.webhook_secret.encode("utf-8"),
            self._raw_body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(supplied, expected)

    def _authorized(self) -> bool:
        if not self.access_password:
            return True
        return hmac.compare_digest(
            self.headers.get("X-Access-Token", ""),
            self.access_password,
        )

    def _require_session(self, session_id: str) -> bool:
        if not self.enforce_session_tokens:
            return True
        if not session_id:
            return True
        try:
            self.service.authorize_session(
                session_id,
                self.headers.get("X-Session-Token", ""),
            )
            return True
        except SessionAuthError:
            self._send_json(
                403,
                {
                    "error": "session_token_invalid",
                    "message": "会话令牌缺失或无效，请重新创建会话",
                },
            )
            return False
        except SessionNotFound as exc:
            self._send_json(
                404,
                {"error": "session_not_found", "message": f"会话不存在：{exc}"},
            )
            return False

    def _within_rate_limit(self) -> bool:
        server = self.server
        address = str(self.client_address[0]) if self.client_address else "unknown"
        now = time.monotonic()
        with server.rate_limit_lock:  # type: ignore[attr-defined]
            window = server.rate_limit_windows[address]  # type: ignore[attr-defined]
            cutoff = now - 60
            while window and window[0] < cutoff:
                window.popleft()
            limit = server.rate_limit_per_minute  # type: ignore[attr-defined]
            if len(window) >= limit:
                return False
            window.append(now)
            return True

    def _upload(self, payload: dict[str, Any]) -> None:
        session_id = str(payload.get("session_id") or payload.get("session") or "")
        session_token = ""
        filename = str(payload.get("filename") or "")
        suffix = Path(filename).suffix.lower()
        if suffix not in UPLOAD_SUFFIXES:
            self._send_json(400, {
                "error": "unsupported_file",
                "message": "只支持图片、TXT、CSV 或 Excel（xlsx/xlsm）账单",
            })
            return
        try:
            raw = base64.b64decode(str(payload.get("data") or ""), validate=True)
        except (ValueError, TypeError):
            self._send_json(400, {"error": "bad_file", "message": "文件内容解析失败"})
            return
        if not raw:
            self._send_json(400, {"error": "empty_file", "message": "文件为空"})
            return
        if len(raw) > MAX_UPLOAD_BYTES:
            self._send_json(413, {"error": "file_too_large", "message": "文件超过 8MB"})
            return
        if not _upload_matches_suffix(suffix, raw):
            self._send_json(400, {
                "error": "file_type_mismatch",
                "message": "文件内容与扩展名不一致，请上传真实的图片、TXT、CSV 或 Excel 账单",
            })
            return
        if not session_id:
            created = self.service.create_session(
                user_id="u_001",
                channel="h5",
            )
            session_id = created["session_id"]
            session_token = str(created.get("session_token") or "")
        UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        target = UPLOAD_ROOT / f"{uuid.uuid4().hex}{suffix}"
        target.write_bytes(raw)
        request_id = f"upload-v{UPLOAD_PARSER_VERSION}-" + hashlib.sha256(raw).hexdigest()
        try:
            try:
                body = self.service.send_message(
                    session_id=session_id,
                    message=f"识别这张账单截图 {target}",
                    request_id=request_id,
                )
            except SessionNotFound:
                # A browser can keep a session id across a server restart.
                # Recreate it and retry once instead of failing the upload.
                created = self.service.create_session(
                    user_id="u_001",
                    channel="h5",
                )
                session_id = created["session_id"]
                session_token = str(created.get("session_token") or "")
                body = self.service.send_message(
                    session_id=session_id,
                    message=f"识别这张账单截图 {target}",
                    request_id=request_id,
                )
        finally:
            target.unlink(missing_ok=True)
        body["session_id"] = session_id
        if session_token:
            body["session_token"] = session_token
        self._send_json(200, body)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self._send_json(404, {"error": "not_found", "message": "页面文件不存在"})
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _stream_message(self, payload: dict[str, Any]) -> None:
        session_id = str(payload.get("session_id") or "")
        request_id = str(payload.get("request_id") or "")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        if not self._safe_write_sse(
            "start",
            {"session_id": session_id, "request_id": request_id},
        ):
            return
        try:
            body = self.service.send_message(
                session_id=session_id,
                message=str(payload.get("message") or ""),
                request_id=request_id,
            )
            request_status = str(body.get("request_status") or "completed")
            if request_status == "completed" and isinstance(
                body.get("reply"),
                dict,
            ):
                if not self._safe_write_sse("message", body):
                    return
                self._safe_write_sse(
                    "done",
                    {"ok": True, "request_status": "completed"},
                )
            elif request_status in {
                "processing",
                "outcome_unknown",
                "failed",
            }:
                if not self._safe_write_sse("status", body):
                    return
                self._safe_write_sse(
                    "done",
                    {
                        "ok": request_status in {"processing", "failed"},
                        "request_status": request_status,
                    },
                )
            else:
                self._safe_write_sse(
                    "error",
                    {
                        "error": "invalid_request_result",
                        "message": "服务返回了无法识别的请求状态",
                    },
                )
        except SessionNotFound as exc:
            self._safe_write_sse(
                "error",
                {"error": "session_not_found", "message": f"会话不存在：{exc}"},
            )
        except OptimisticConcurrencyError as exc:
            self._safe_write_sse(
                "error",
                {"error": "session_conflict", "message": str(exc)},
            )
        except Exception as exc:
            self._safe_write_sse(
                "error",
                {"error": "stream_error", "message": str(exc)},
            )

    def _safe_write_sse(self, event: str, payload: dict[str, Any]) -> bool:
        try:
            self._write_sse(event, payload)
            return True
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
            return False

    def _write_sse(self, event: str, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        chunk = f"event: {event}\ndata: {data}\n\n".encode("utf-8")
        self.wfile.write(chunk)
        self.wfile.flush()

    def log_message(self, format: str, *args: Any) -> None:
        return


def create_server(
    service: AgentSessionService,
    host: str = "127.0.0.1",
    port: int = 8000,
    web_root: Optional[Path] = None,
    webhook_secret: str = "",
    access_password: str = "",
    rate_limit_per_minute: Optional[int] = None,
    enforce_session_tokens: bool = False,
) -> ApiHTTPServer:
    handler: Type[ApiRequestHandler] = type(
        "BoundApiRequestHandler",
        (ApiRequestHandler,),
        {
            "service": service,
            "webhook_secret": webhook_secret,
            "access_password": access_password,
            "enforce_session_tokens": enforce_session_tokens,
        },
    )
    if rate_limit_per_minute is None:
        try:
            rate_limit_per_minute = int(os.getenv(
                "FINAGENT_RATE_LIMIT_PER_MINUTE",
                "120",
            ))
        except ValueError:
            rate_limit_per_minute = 120
    server = ApiHTTPServer(
        (host, port),
        handler,
        rate_limit_per_minute=rate_limit_per_minute,
    )
    server.web_root = Path(web_root) if web_root else WEB_ROOT  # type: ignore[attr-defined]
    return server
