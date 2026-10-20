"""HTTP/H5 entry point for AI Banking Agent."""
from __future__ import annotations

import argparse
import os
import socket
from datetime import datetime
from pathlib import Path

from config import LLMConfig, STORE_FILE
from finagent.api.server import create_server
from finagent.api.service import AgentSessionService
from finagent.data.store import Clock, DataStore


def detect_lan_ip() -> str:
    """Best-effort local IPv4 address without sending network traffic."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


def access_urls(host: str, port: int, lan_ip: str = "") -> list[str]:
    """Return local and LAN URLs for the configured bind address."""
    if host in {"0.0.0.0", "::"}:
        urls = [f"http://127.0.0.1:{port}"]
        address = lan_ip or detect_lan_ip()
        if address not in {"127.0.0.1", "0.0.0.0"}:
            urls.append(f"http://{address}:{port}")
        return urls
    if host in {"127.0.0.1", "localhost"}:
        return [f"http://127.0.0.1:{port}"]
    return [f"http://{host}:{port}"]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="AI Banking Agent HTTP/H5 服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000")
    parser.add_argument("--today", default="", help="固定演示日期，格式 YYYY-MM-DD")
    parser.add_argument(
        "--llm",
        default=LLMConfig.MODE,
        choices=["auto", "on", "off"],
        help="大模型模式，默认读取环境变量",
    )
    return parser.parse_args(argv)


def build_store(today: str = "") -> DataStore:
    clock = Clock()
    if today:
        clock.set(datetime.fromisoformat(today + "T09:00:00"))
    if STORE_FILE.exists() and not today:
        try:
            store = DataStore.load(STORE_FILE, clock=clock)
            if store.users:
                return store
        except Exception:
            pass
    return DataStore(clock=clock, path=STORE_FILE).seed()


def build_session_store(today: str = "") -> DataStore:
    clock = Clock()
    if today:
        clock.set(datetime.fromisoformat(today + "T09:00:00"))
    return DataStore(clock=clock).seed()


def main(argv=None) -> int:
    args = parse_args(argv)
    service = AgentSessionService(
        build_store(args.today),
        llm_mode=args.llm,
        store_factory=lambda: build_session_store(args.today),
    )
    server = create_server(
        service,
        host=args.host,
        port=args.port,
        webhook_secret=os.getenv("FINAGENT_WEBHOOK_SECRET", ""),
        access_password=os.getenv("FINAGENT_WEB_PASSWORD", ""),
    )
    host, port = server.server_address
    print("AI Banking Agent 访问地址：")
    for url in access_urls(str(host), int(port)):
        print(f"  H5:     {url}")
        print(f"  Health: {url}/health")
    if str(host) == "0.0.0.0":
        print("  同一 Wi-Fi 下的手机可以使用上面的局域网地址访问")
        print("  其他设备无法访问时，请检查 Windows 防火墙是否允许该端口")
    print("按 Ctrl+C 停止服务")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
