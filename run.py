"""AI Banking Agent 启动入口。

示例：
    python run.py
    python run.py --today 2026-11-08 --llm off
    python run.py --script scripts/demo_transfer.txt
    python run.py --fresh        # 忽略本地数据快照，用全新模拟数据启动
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import DEFAULT_USER_ID, LLMConfig, RUNTIME_DIR, STORE_FILE  # noqa: E402
from finagent.agent.core import BankingAgent  # noqa: E402
from finagent.cli.app import Palette, ensure_utf8, run_repl, run_script  # noqa: E402
from finagent.data.store import Clock, DataStore  # noqa: E402
from finagent.nlu.llm_client import LLMClient  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="AI Banking Agent 智能银行代理系统（赛事原型）")
    parser.add_argument("--user", default=DEFAULT_USER_ID, help="演示用的模拟客户 ID")
    parser.add_argument("--today", default="", help="把'今天'固定为某天，格式 YYYY-MM-DD（演示定时任务很有用）")
    parser.add_argument("--script", default="", help="按脚本文件回放对话")
    parser.add_argument("--llm", default="", choices=["", "auto", "on", "off"],
                        help="大模型模式：auto/on/off，默认读取环境变量")
    parser.add_argument("--fresh", action="store_true", help="忽略本地快照，重新生成模拟数据")
    parser.add_argument("--save", action="store_true", help="退出时把数据写回本地快照")
    parser.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    return parser.parse_args(argv)


def build_store(args) -> DataStore:
    clock = Clock()
    if args.today:
        clock.set(datetime.fromisoformat(args.today + "T09:00:00"))
    if STORE_FILE.exists() and not args.fresh:
        try:
            store = DataStore.load(STORE_FILE, clock=clock)
            if store.users:
                return store
        except Exception:
            pass
    return DataStore(clock=clock, path=STORE_FILE).seed()


def main(argv=None) -> int:
    args = parse_args(argv)
    ensure_utf8()
    store = build_store(args)
    palette = Palette(enabled=(not args.no_color) and sys.stdout.isatty())
    llm_mode = args.llm or LLMConfig.MODE
    agent = BankingAgent(store, args.user, llm=LLMClient(), llm_mode=llm_mode)

    if args.script:
        code = run_script(agent, Path(args.script), palette)
    else:
        run_repl(agent, palette)
        code = 0

    if args.save:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        store.save(STORE_FILE)
        print(f"数据已保存到 {STORE_FILE}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

