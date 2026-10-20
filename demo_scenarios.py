"""六大核心场景 + 扩展能力一键演示。

用法：
    python demo_scenarios.py              # 全部场景
    python demo_scenarios.py --list       # 查看场景列表
    python demo_scenarios.py --only 4     # 只跑第 4 个场景
    python demo_scenarios.py --llm on     # 用大模型做意图理解

每个场景都用独立的模拟数据，输出完整的"用户输入 -> Agent 回复 -> 工具调用"轨迹，
方便直接录屏或写进答辩材料。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import DISCLAIMER, LLMConfig  # noqa: E402
from finagent.agent.core import BankingAgent  # noqa: E402
from finagent.cli.app import ensure_utf8  # noqa: E402
from finagent.data.store import Clock, DataStore  # noqa: E402
from finagent.nlu.llm_client import LLMClient  # noqa: E402


# (场景标题, [用户输入...])
SAMPLE_WECHAT_BILL = """2026年10月
全部账单  查找交易  收支统计>
抖音电商商家   -148.72
10月14日13:11
微信红包-发给何心熠   -2.50
10月14日08:47
转账-退款   +6.50
10月14日08:45
转账-转给何心熠   -6.50
10月13日23:33  对方已退还
罗森(深圳大学粤海校    -20.40
10月13日10:00
湘小悦湘菜   -16.00
10月12日17:35
支出¥7283.34 收入¥9577.09"""

SCENARIOS: List[Tuple[str, List[str]]] = [
    (
        "场景一 · 智能转账（按备注转账 / 定时转账 / AA 收款）",
        [
            "给老婆转2000块，备注生日礼物，用微信",
            "确认",
            "下个月5号自动给房东转3800房租，用银行卡",
            "确认",
            "昨晚聚餐960，我、王强、李思琪AA",
            "确认",
            "我有哪些定时转账",
        ],
    ),
    (
        "场景二 · 账单分析（分类统计 / 异常识别 / 账单报告）",
        [
            "分析我这个月的账单",
            "帮我看看有没有异常交易",
            "我今年在星巴克花了多少钱",
            "生成我的年度账单报告",
        ],
    ),
    (
        "场景三 · 理财操作（风险评估 / 产品推荐对比 / 一键申购赎回）",
        [
            "帮我做个风险测评",
            # 强制风险问卷（6 道计分 + 1 道一致性校验）
            "b", "b", "c", "d", "a", "c", "c",
            "我有5万块，稳健一点怎么配置",
            "买1万块稳利债券A",
            "确认",
            "我的理财持仓",
            "赎回稳利债券A 5000元",
            "确认",
        ],
    ),
    (
        "场景四 · 卡片管理（卡列表 / 额度调整 / 交易限制 / 挂失）",
        [
            "我有哪些卡",
            "关闭信用卡的境外交易",
            "确认",
            "信用卡额度提到3万",
            "确认",
            "123456",
            "我的卡丢了，帮我挂失",
            "确认",
        ],
    ),
    (
        "场景五 · 订阅 / 代扣管理（自动识别 / 续费提醒 / 一键取消）",
        [
            "有哪些自动续费",
            "最近有哪些订阅要扣费",
            "取消iCloud订阅",
            "确认",
            "我的订阅都有什么",
        ],
    ),
    (
        "场景六 · 跨场景联动（生日惊喜：锁定资金 + 预约鲜花蛋糕）",
        [
            "我老婆快生日了，帮我准备一下",
            "11月20日",
            "确认",
            "今天有什么要注意的",
            "/advance 40",
        ],
    ),
    (
        "扩展场景 · 账单截图识别（OCR -> 核对 -> 导入 -> 异常）",
        [
            "识别这张账单截图 samples/bill_sample.png",
            "确认",
            "分析我最近7天的账单",
        ],
    ),
    (
        "扩展场景 · 粘贴微信/支付宝账单文本（整段粘贴自动解析）",
        [
            SAMPLE_WECHAT_BILL,
            "确认",
            "最近7天花了多少",
        ],
    ),
]


def build_agent(llm_mode: str, today: str = "2026-10-14") -> BankingAgent:
    clock = Clock(datetime.fromisoformat(today + "T09:30:00"))
    store = DataStore(clock=clock).seed()
    return BankingAgent(store, "u_001", llm=LLMClient(), llm_mode=llm_mode)


def run_scenario(index: int, title: str, turns: List[str], llm_mode: str) -> None:
    agent = build_agent(llm_mode)
    print()
    print("=" * 78)
    print(f"{title}")
    print("=" * 78)
    for text in turns:
        if text.startswith("/"):
            name, _, arg = text.partition(" ")
            if name == "/advance":
                days = int(arg or 1)
                agent.clock.advance(days=days)
                print(f"\n[系统] 时间快进到 {agent.clock.date_str()}")
                messages = agent.run_due_tasks()
                if messages:
                    for message in messages:
                        print(f"Agent> {message}")
                else:
                    print("Agent> 没有到期任务需要执行。")
            continue
        reply = agent.handle(text)
        print(f"\n用户> {text}")
        for line_no, line in enumerate(reply.text.splitlines()):
            print(("Agent> " if line_no == 0 else "       ") + line)
        for item in reply.trace:
            args = ", ".join(f"{k}={v!r}" for k, v in item["args"].items())
            flag = "OK" if item["ok"] else "FAIL"
            print(f"       [{flag}] {item['tool']}({args})")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="六大核心场景演示")
    parser.add_argument("--only", type=int, default=0, help="只运行第 N 个场景（1 起）")
    parser.add_argument("--list", action="store_true", help="列出全部场景")
    parser.add_argument("--llm", default=LLMConfig.MODE, choices=["auto", "on", "off"])
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    ensure_utf8()
    if args.list:
        for idx, (title, _) in enumerate(SCENARIOS, start=1):
            print(f"{idx}. {title}")
        return 0
    print(DISCLAIMER)
    targets = [(args.only, SCENARIOS[args.only - 1])] if args.only else list(enumerate(SCENARIOS, start=1))
    for index, (title, turns) in targets:
        run_scenario(index, title, turns, args.llm)
    print()
    print("=" * 78)
    print("演示结束。提示：加 --llm on 并配置 API Key，可以体验大模型驱动的语义理解。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

