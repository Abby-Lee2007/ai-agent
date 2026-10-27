"""测试公共夹具。"""
from __future__ import annotations

import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from finagent.agent.core import BankingAgent  # noqa: E402
from finagent.data.store import Clock, DataStore  # noqa: E402

TODAY = datetime(2026, 10, 14, 9, 30, 0)


def make_store(today: datetime = TODAY) -> DataStore:
    """构造一份干净的模拟数据。"""
    return DataStore(clock=Clock(today)).seed()


def make_agent(
    today: datetime = TODAY,
    user_id: str = "u_001",
    live_market: bool = False,
    risk_completed: bool = True,
) -> BankingAgent:
    """离线模式的 Agent（默认不依赖大模型，也不访问实时行情）。

    risk_completed=True 表示已完成风险问卷，理财类操作不会再被问卷闸门拦住；
    专门测试闸门本身时传 False。
    """
    agent = BankingAgent(make_store(today), user_id, llm_mode="off")
    agent.market.mode = "live" if live_market else "off"
    if risk_completed:
        agent.risk_level = 3
    return agent


def talk(agent: BankingAgent, *turns: str):
    """连续对话，返回最后一次回复。"""
    reply = None
    for turn in turns:
        reply = agent.handle(turn)
    return reply


def run_to_completion(
    agent: BankingAgent,
    reply,
    confirm_text: str = "确认",
    code: str = "123456",
    payment_password: str = "246810",
    limit: int = 4,
):
    """把"二次确认 / 短信验证"一路走完，返回最终回复。"""
    steps = 0
    while steps < limit:
        pending = agent.session.pending
        if reply.kind == "confirm":
            reply = agent.handle(confirm_text)
        elif reply.kind == "verify":
            reply = agent.handle(code)
        elif pending is not None and pending.kind == "payment_auth":
            reply = agent.handle(payment_password)
        else:
            break
        steps += 1
    return reply


def answer_basic_questionnaire(agent: BankingAgent, answers: str = "b"):
    """按顺序回答基础问卷（6 道计分 + 1 道校验），返回最后一轮回复。"""
    reply = None
    for _ in range(7):
        reply = agent.handle(answers)
        pending = agent.session.pending
        if pending is None or pending.kind != "questionnaire":
            break
    return reply


def send_money(agent: BankingAgent, text: str, channel: str = "微信"):
    """发起转账，自动回答渠道追问并确认关键数据。"""
    reply = agent.handle(text)
    pending = agent.session.pending
    if reply.kind == "question" and pending is not None and "channel" in pending.missing:
        reply = agent.handle(channel)
        pending = agent.session.pending
    if reply.kind == "confirm" and pending is not None and pending.kind == "data_confirm":
        reply = run_to_completion(agent, reply)
    return reply
