"""风险画像：把问卷评分结果包装成可展示的 RiskProfile。

打分逻辑全部在 `finagent/risk/questionnaire.py`（14 题 / 5 维度 / 100 分），
这个模块只负责三件事：
1. 把问卷明细转成统一的 ScoreItem，方便工具层渲染；
2. 按客户等级给出建议配置比例；
3. 做回撤压力测试（按当前金融资产测算浮亏）。

注意命名：客户等级是 C1-C5，产品风险是 R1-R5，两者不是一回事。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Tuple

from finagent.data.models import User
from finagent.data.store import DataStore
from finagent.risk.questionnaire import (
    CUSTOMER_LABELS,
    LEVEL_THRESHOLDS,
    expand_basic_answers,
    evaluate as evaluate_questionnaire,
)

LEVEL_LABELS = CUSTOMER_LABELS
LEVEL_ALLOCATION = {
    1: [("货币基金/大额存单", "80%"), ("债券基金", "20%"), ("权益类", "0%")],
    2: [("货币基金/大额存单", "55%"), ("债券基金", "35%"), ("权益类", "10%")],
    3: [("货币基金/大额存单", "35%"), ("债券基金", "35%"), ("权益类", "30%")],
    4: [("货币基金/大额存单", "20%"), ("债券基金", "30%"), ("权益类", "50%")],
    5: [("货币基金/大额存单", "10%"), ("债券基金", "20%"), ("权益类", "70%")],
}
DRAWDOWN_TOLERANCE = {1: 0.05, 2: 0.10, 3: 0.20, 4: 0.30, 5: 0.40}


@dataclass
class ScoreItem:
    """一个打分项（对应问卷的一道题）。"""

    dimension: str
    name: str
    score: float
    max_score: float
    detail: str


@dataclass
class RiskProfile:
    """风险测评结果。"""

    total: float = 0.0
    max_total: float = 100.0
    level: int = 3
    label: str = CUSTOMER_LABELS[3]
    items: List[ScoreItem] = field(default_factory=list)
    dimension_totals: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    total_assets: float = 0.0
    monthly_expense: float = 0.0
    drawdown_tolerance: float = 0.20
    stress_loss: float = 0.0
    stress_ok: bool = True
    allocation: List[Tuple[str, str]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    answered: int = 0
    inferred: int = 0

    def reasons(self) -> List[str]:
        return [f"{dimension} {score:.0f}/{max_score:.0f}"
                for dimension, (score, max_score) in self.dimension_totals.items()]

    @property
    def max_product_risk(self) -> int:
        return min(self.level, 5)

    @property
    def confirm_product_risk(self) -> int:
        return min(self.level + 1, 5)


def _monthly_expense(store: DataStore, user_id: str, today: date) -> float:
    start = (today - timedelta(days=90)).isoformat()
    txns = store.transactions_of(user_id, start, today.isoformat())
    expense = sum(-t.amount for t in txns if t.amount < 0 and t.category not in {"理财", "转账"})
    return round(expense / 3, 2) if expense else 0.0


def assess_risk_profile(user: User, store: DataStore, today: date,
                        level_override: int = None,
                        answers: dict = None) -> RiskProfile:
    """执行监管口径评分问卷，返回可解释的风险画像。

    level_override：用户已完成交互式问卷时，用实测等级覆盖推断等级。
    """
    result = evaluate_questionnaire(
        expand_basic_answers(answers) if answers else None,
        user=user, store=store, today=today,
    )
    accounts = store.accounts_of(user.user_id)
    total_assets = store.total_assets(accounts)
    monthly_expense = _monthly_expense(store, user.user_id, today) or max(user.monthly_income * 0.6, 1.0)

    items = [
        ScoreItem(
            dimension=detail.dimension,
            name=detail.question,
            score=detail.score,
            max_score=detail.max_score,
            detail=detail.answer + ("（由账户数据推断）" if detail.inferred else ""),
        )
        for detail in result.details
    ]
    profile = RiskProfile(
        total=result.total,
        max_total=result.max_total,
        level=result.level,
        label=result.label,
        items=items,
        dimension_totals=result.dimension_totals,
        total_assets=round(total_assets, 2),
        monthly_expense=monthly_expense,
        drawdown_tolerance=DRAWDOWN_TOLERANCE[result.level],
        allocation=LEVEL_ALLOCATION[result.level],
        answered=result.answered,
        inferred=result.inferred,
    )
    if level_override:
        profile.level = max(1, min(5, int(level_override)))
        profile.label = CUSTOMER_LABELS[profile.level]
        profile.allocation = LEVEL_ALLOCATION[profile.level]
        profile.drawdown_tolerance = DRAWDOWN_TOLERANCE[profile.level]
        profile.notes.append(f"本次采用问卷实测等级 {profile.label}（优先于账户数据推断）。")
    profile.stress_loss = round(total_assets * profile.drawdown_tolerance, 2)
    profile.notes.append(
        f"回撤压力测试：若市场下跌 {profile.drawdown_tolerance:.0%}，"
        f"您的金融资产将浮亏约 {profile.stress_loss:,.2f} 元。"
    )
    profile.notes.append(
        f"产品匹配：可买 R1-R{profile.max_product_risk}；"
        f"R{profile.confirm_product_risk} 属超出承受能力一级，需签风险不匹配警示书并二次确认；"
        f"R{min(profile.level + 2, 5)} 及以上禁止销售。"
    )
    profile.notes.append("问卷题目与分值为原型自拟，框架对齐《证券期货投资者适当性管理办法》的评分思路。")
    return profile
