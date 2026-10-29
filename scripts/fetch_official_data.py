"""Refresh official public macro data for the local prototype."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from finagent.data.official_sources import SNAPSHOT_FILE, refresh_official_data

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="刷新人民银行、中国债券信息网与世界银行公开数据",
    )
    parser.add_argument(
        "--update-snapshot",
        action="store_true",
        help="仅在所有数据源成功时更新项目随附快照",
    )
    args = parser.parse_args()

    payload = refresh_official_data()
    for source in payload.get("sources", []):
        state = source.get("status", "unknown")
        title = source.get("title") or source.get("name")
        error = source.get("last_attempt_error") or source.get("error")
        print(f"[{state}] {title}" + (f" - {error}" if error else ""))
    print(
        f"状态：{payload.get('status')}；数据时点：{payload.get('as_of')}；"
        f"缓存：{payload.get('fetched_at')}"
    )
    if args.update_snapshot:
        errors = payload.get("refresh_errors") or []
        if errors:
            print("存在刷新失败，未覆盖项目随附快照。")
            print("\n".join(errors))
            return 2
        snapshot_payload = dict(payload)
        snapshot_payload["original_status"] = payload.get("status")
        snapshot_payload["status"] = "snapshot"
        snapshot_payload["delivery"] = "bundled_snapshot"
        snapshot_payload["warnings"] = [
            "这是随项目发布的官方公开数据快照，不是实时行情。",
            *list(payload.get("warnings") or []),
        ]
        snapshot_payload["freshness"] = {
            **dict(payload.get("freshness") or {}),
            "state": "snapshot",
            "label": "随项目发布的官方数据快照",
            "live_network_call_this_request": False,
        }
        SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_FILE.write_text(
            json.dumps(snapshot_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"已更新随附快照：{SNAPSHOT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
