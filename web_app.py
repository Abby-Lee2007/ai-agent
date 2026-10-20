"""网页版 Demo（零依赖）。

直接用 Python 标准库起一个 HTTP 服务：
    python web_app.py --host 0.0.0.0 --port 8000

同一 Wi-Fi 下的手机/另一台电脑访问：
    http://<这台电脑的局域网 IP>:8000

说明：这是赛事原型，没有登录与权限体系，只适合在可信网络里演示，
不要直接暴露到公网。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from config import DEFAULT_USER_ID, LLMConfig
from finagent.agent.core import BankingAgent
from finagent.agent.planner import Plan, PlanStep
from finagent.data.store import DataStore
from finagent.vision.receipt_reader import IMAGE_SUFFIXES, TEXT_SUFFIXES

ROOT = Path(__file__).resolve().parent
INDEX_FILE = ROOT / "web" / "index.html"
UPLOAD_DIR = ROOT / "runtime_uploads"
SESSION_TTL = 2 * 60 * 60
MAX_SESSIONS = 60
ACCESS_PASSWORD = os.getenv("FINAGENT_WEB_PASSWORD", "").strip()


class SessionPool:
    """每个浏览器会话一套独立的模拟账户，互不干扰。"""

    def __init__(self) -> None:
        self._items: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> Tuple[str, Dict[str, Any]]:
        with self._lock:
            item = self._items.get(session_id)
            now = time.time()
            if item and now - item["seen"] < SESSION_TTL:
                item["seen"] = now
                return session_id, item
            if len(self._items) >= MAX_SESSIONS:
                oldest = min(self._items.items(), key=lambda kv: kv[1]["seen"])[0]
                self._items.pop(oldest, None)
        return self._create()

    def _create(self) -> Tuple[str, Dict[str, Any]]:
        session_id = secrets.token_urlsafe(16)
        store = DataStore().seed()
        agent = BankingAgent(store, DEFAULT_USER_ID, llm_mode=LLMConfig.MODE)
        item = {"agent": agent, "lock": threading.Lock(), "seen": time.time()}
        with self._lock:
            self._items[session_id] = item
        return session_id, item


POOL = SessionPool()


class Handler(BaseHTTPRequestHandler):
    server_version = "FinAgentWeb/0.1"

    def log_message(self, fmt: str, *args) -> None:  # 静默访问日志
        return

    # ------------------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 2 * 1024 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # ------------------------------------------------------------------
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            if not INDEX_FILE.exists():
                self._send(500, b"web/index.html not found", "text/plain; charset=utf-8")
                return
            self._send(200, INDEX_FILE.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/health":
            self._json(200, {"ok": True, "time": time.strftime("%Y-%m-%d %H:%M:%S")})
            return
        if path.startswith("/api/") and not self._authorized():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self._authorized():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        if path == "/api/chat":
            self._chat()
            return
        if path == "/api/reset":
            self._reset()
            return
        if path == "/api/upload":
            self._upload()
            return
        self._json(404, {"ok": False, "error": "not found"})

    # ------------------------------------------------------------------
    def _upload(self) -> None:
        payload = self._read_json()
        session_id = str(payload.get("session") or "")
        filename = str(payload.get("filename") or "")
        suffix = Path(filename).suffix.lower()
        if suffix not in IMAGE_SUFFIXES and suffix not in TEXT_SUFFIXES:
            self._json(400, {"ok": False, "error": "只支持图片（png/jpg/webp 等）或 txt/csv 账单"})
            return
        try:
            raw = base64.b64decode(str(payload.get("data") or ""), validate=True)
        except (ValueError, TypeError):
            self._json(400, {"ok": False, "error": "文件内容解析失败"})
            return
        if not raw:
            self._json(400, {"ok": False, "error": "文件为空"})
            return
        if len(raw) > 8 * 1024 * 1024:
            self._json(413, {"ok": False, "error": "文件超过 8MB，请压缩后再传"})
            return
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        target = UPLOAD_DIR / f"{secrets.token_hex(8)}{suffix}"
        target.write_bytes(raw)
        session_id, item = POOL.get(session_id) if session_id else POOL.get("")
        agent: BankingAgent = item["agent"]
        plan = Plan(intent="bill.scan_receipt",
                    steps=[PlanStep("bill.scan_receipt", {"image": str(target)})])
        with item["lock"]:
            reply = agent._execute(plan, agent.context())
        self._json(200, {
            "ok": True,
            "session": session_id,
            "reply": reply.text,
            "intent": reply.intent,
            "kind": reply.kind,
            "needs_input": reply.needs_input,
            "trace": [{"tool": t.get("tool"), "ok": t.get("ok"),
                       "summary": t.get("summary"), "args": t.get("args")} for t in reply.trace],
        })

    def _authorized(self) -> bool:
        if not ACCESS_PASSWORD:
            return True
        return self.headers.get("X-Access-Token", "") == ACCESS_PASSWORD

    # ------------------------------------------------------------------
    def _chat(self) -> None:
        payload = self._read_json()
        message = str(payload.get("message") or "").strip()
        if not message:
            self._json(400, {"ok": False, "error": "message is required"})
            return
        session_id = str(payload.get("session") or "")
        session_id, item = POOL.get(session_id) if session_id else POOL.get("")
        agent: BankingAgent = item["agent"]
        with item["lock"]:
            reply = agent.handle(message)
        trace = [
            {"tool": t.get("tool"), "ok": t.get("ok"),
             "summary": t.get("summary"), "args": t.get("args")}
            for t in reply.trace
        ]
        self._json(200, {
            "ok": True,
            "session": session_id,
            "reply": reply.text,
            "intent": reply.intent,
            "kind": reply.kind,
            "needs_input": reply.needs_input,
            "trace": trace,
        })

    def _reset(self) -> None:
        payload = self._read_json()
        session_id = str(payload.get("session") or "")
        if session_id:
            with POOL._lock:
                item = POOL._items.pop(session_id, None)
        self._json(200, {"ok": True})


def local_ip() -> str:
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Banking Agent 网页版")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("=" * 62)
    print("  AI Banking Agent · 网页版 Demo")
    print(f"  本机访问：   http://127.0.0.1:{args.port}")
    if args.host == "0.0.0.0":
        print(f"  局域网访问： http://{local_ip()}:{args.port}")
        print("  （手机和电脑连同一个 Wi-Fi 即可；仅限可信网络）")
    print("  Ctrl+C 停止服务")
    print("=" * 62)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
