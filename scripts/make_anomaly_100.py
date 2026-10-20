"""生成 100 条交易的样例数据集（其中恰好 1 条异常），并跑一遍异常识别。

用法：python scripts/make_anomaly_100.py
输出：samples/anomaly_dataset_100.csv
"""
from __future__ import annotations

import csv
import random
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from finagent.data.store import Clock, DataStore
from finagent.risk.engine import RiskEngine

OUT = ROOT / "samples" / "anomaly_dataset_100.csv"
MERCHANTS = ["常去餐厅A", "常去餐厅B", "常去餐厅C"]


def build_rows():
    rng = random.Random(42)
    rows = []
    for i in range(99):
        day = i // 4 + 1
        hour = 7 + (i % 15)
        rows.append({
            "date": f"2026-09-{day:02d}",
            "time": f"{hour:02d}:{rng.randint(0, 59):02d}",
            "merchant": MERCHANTS[i % 3],
            "category": "餐饮",
            "amount": f"{-round(25 + (i % 80) * 1.2, 2):.2f}",
            "channel": "扫码",
        })
    # 唯一的异常：同类别、老商户、白天，仅金额显著离群
    rows.append({
        "date": "2026-10-08", "time": "14:18", "merchant": "常去餐厅A",
        "category": "餐饮", "amount": "-3500.00", "channel": "刷卡",
    })
    return rows


def main() -> int:
    rows = build_rows()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["date", "time", "merchant", "category", "amount", "channel"])
        writer.writeheader()
        writer.writerows(rows)

    clock = Clock(datetime(2026, 10, 20, 9, 0, 0))
    store = DataStore(clock=clock)
    for row in rows:
        store.add_transaction(
            user_id="u_001", account_id="ACC000001",
            amount=float(row["amount"]), merchant=row["merchant"],
            category=row["category"], channel=row["channel"],
            timestamp=f"{row['date']}T{row['time']}:00",
        )
    engine = RiskEngine(store, clock)
    anomalies = engine.detect_anomalies("u_001", include_subscriptions=False)

    print(f"数据集：{OUT}")
    print(f"总交易：{len(rows)} 笔 | 检出异常：{len(anomalies)} 笔")
    for item in anomalies:
        print(f"  - {item.kind} | 风险分 {item.score:.0f} | {item.merchant} {item.amount:,.2f}")
        print(f"    指标：{item.metrics}")
        print(f"    建议：{item.suggestion}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
