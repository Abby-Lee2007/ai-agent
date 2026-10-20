"""可复现的异常交易样例：专业量化分析演示。

用法：python scripts/demo_anomaly_case.py
构造：12 笔正常餐饮 + 1 笔凌晨新商户大额，观察规则与统计指标如何命中。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from finagent.data.store import Clock, DataStore
from finagent.risk.engine import RiskEngine


def build_case() -> DataStore:
    clock = Clock(datetime(2026, 10, 20, 9, 0, 0))
    store = DataStore(clock=clock).seed()
    store.transactions.clear()
    # 基线：12 笔正常餐饮，金额 60-104 元，中午消费
    for i in range(12):
        store.add_transaction(
            user_id="u_001", account_id="ACC000001", amount=-(60 + i * 4),
            merchant="常去餐厅", category="餐饮", channel="扫码",
            timestamp=f"2026-10-{i + 1:02d}T12:30:00",
        )
    # 异常：凌晨 03:12，陌生商户，单笔 6800 元
    store.add_transaction(
        user_id="u_001", account_id="ACC000003", amount=-6800.0,
        merchant="新商户-深夜餐厅", category="餐饮", channel="刷卡",
        timestamp="2026-10-20T03:12:00",
    )
    return store


def main() -> int:
    store = build_case()
    engine = RiskEngine(store, store.clock)
    anomalies = engine.detect_anomalies("u_001", include_subscriptions=False)
    print("=" * 72)
    print("异常交易量化分析样例")
    print("=" * 72)
    print(f"{'规则':<20}{'风险分':>7}  {'量化依据'}")
    print("-" * 72)
    for item in anomalies:
        print(f"{item.kind:<20}{item.score:>7.1f}  {item.reason}")
        print(f"{'':<28}{item.metrics}")
    print("-" * 72)
    if anomalies:
        top = anomalies[0]
        print(f"最高风险：{top.merchant} {top.amount:,.2f} 元，风险分 {top.score:.0f}/100")
        print(f"处理建议：{top.suggestion}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
