"""Transparent personal financial health score."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List

from finagent.analytics.cashflow import build_cashflow_forecast
from finagent.analytics.data_quality import build_data_quality
from finagent.data.store import Clock, DataStore


def _piecewise_score(value: float, points: List[tuple[float, float]]) -> float:
    """Linearly interpolate a score from transparent benchmark points."""
    ordered = sorted(points, key=lambda item: item[0])
    if value <= ordered[0][0]:
        return round(ordered[0][1], 1)
    for (x1, y1), (x2, y2) in zip(ordered, ordered[1:]):
        if value <= x2:
            if x2 == x1:
                return round(y2, 1)
            ratio = (value - x1) / (x2 - x1)
            return round(y1 + ratio * (y2 - y1), 1)
    return round(ordered[-1][1], 1)


def _score_band(score: float) -> str:
    if score >= 85:
        return "优秀"
    if score >= 70:
        return "良好"
    if score >= 55:
        return "需关注"
    return "薄弱"


def build_financial_health(
    store: DataStore,
    providers: Any,
    clock: Clock,
    user_id: str,
) -> Dict[str, Any]:
    today = clock.today()
    forecast = build_cashflow_forecast(
        store,
        providers,
        clock,
        user_id,
        months=3,
    )
    user = store.get_user(user_id)
    cards = providers.cards.of(user_id)
    asset_cards = [
        card for card in cards
        if card.kind in {"debit", "virtual"} and card.status == "active"
    ]
    liquid = sum(card.balance for card in asset_cards)
    credit_cards = [card for card in cards if card.kind == "credit"]
    credit_limit = sum(card.credit_limit for card in credit_cards)
    credit_used = sum(card.used for card in credit_cards)
    debt_ratio = credit_used / max(credit_limit, 1.0)
    essential = max(float(forecast.get("essential_monthly") or 1.0), 1.0)
    liquidity_months = liquid / essential

    txns = store.transactions_of(
        user_id,
        (today - timedelta(days=180)).isoformat(),
        today.isoformat(),
    )
    income = sum(txn.amount for txn in txns if txn.amount > 0 and txn.category == "收入")
    expense = sum(abs(txn.amount) for txn in txns if txn.amount < 0)
    savings_rate = (income - expense) / income if income > 0 else 0.0

    holdings = providers.wealth.holdings(user_id, "holding")
    asset_classes = set()
    for holding in holdings:
        product = providers.wealth.get(holding.product_id)
        if product is not None:
            asset_classes.add(getattr(product, "asset_class", product.category))
    diversification_count = len(asset_classes)

    monthly_subscription = store.monthly_subscription_total(user_id)
    monthly_income = max(float(getattr(user, "monthly_income", 0) or 0), income / 6, 1.0)
    subscription_ratio = monthly_subscription / monthly_income

    anomalies = providers.risk_proxy.detect_anomalies(user_id) if hasattr(providers, "risk_proxy") else []
    risky_cards = [
        card for card in cards
        if card.status in {"frozen", "lost"} or not card.online_enabled
    ]
    goals = providers.goals.of(user_id, "active")
    goal_ratio = (
        sum(min(goal.locked_amount, goal.target_amount) for goal in goals)
        / max(sum(goal.target_amount for goal in goals), 1.0)
        if goals else 1.0
    )

    dimensions: List[Dict[str, Any]] = [
        {
            "key": "liquidity",
            "label": "应急流动性",
            "score": _piecewise_score(
                liquidity_months,
                [(0.0, 0.0), (1.0, 35.0), (3.0, 80.0), (6.0, 100.0)],
            ),
            "weight": 20,
            "detail": f"可动用资金覆盖约 {liquidity_months:.1f} 个月必要支出",
            "current": f"{liquidity_months:.1f} 个月",
            "benchmark": "3-6 个月必要支出",
            "standard": "个人理财规划常用应急金经验值",
        },
        {
            "key": "debt",
            "label": "负债健康",
            "score": _piecewise_score(
                debt_ratio,
                [(0.0, 100.0), (0.3, 80.0), (0.5, 50.0), (0.8, 15.0), (1.0, 0.0)],
            ),
            "weight": 15,
            "detail": f"信用卡已用率为 {debt_ratio * 100:.1f}%",
            "current": f"{debt_ratio * 100:.1f}%",
            "benchmark": "信用卡使用率低于 30% 更稳健",
            "standard": "美国消费者金融保护局（CFPB）信用额度使用率参考",
        },
        {
            "key": "savings",
            "label": "储蓄能力",
            "score": _piecewise_score(
                savings_rate,
                [(-0.2, 0.0), (0.0, 40.0), (0.1, 70.0), (0.2, 100.0)],
            ),
            "weight": 20,
            "detail": f"近180天收支结余率约 {savings_rate * 100:.1f}%",
            "current": f"{savings_rate * 100:.1f}%",
            "benchmark": "可投资结余率达到 20%",
            "standard": "50/30/20 个人预算框架中的储蓄比例",
        },
        {
            "key": "diversification",
            "label": "投资分散",
            "score": _piecewise_score(
                float(diversification_count),
                [(0.0, 0.0), (1.0, 35.0), (2.0, 60.0), (3.0, 80.0), (4.0, 100.0)],
            ),
            "weight": 15,
            "detail": f"当前持有 {diversification_count} 类资产",
            "current": f"{diversification_count} 类",
            "benchmark": "至少覆盖 4 类低相关资产",
            "standard": "组合分散与集中度管理原则",
        },
        {
            "key": "subscriptions",
            "label": "订阅负担",
            "score": _piecewise_score(
                subscription_ratio,
                [(0.0, 100.0), (0.03, 80.0), (0.08, 35.0), (0.12, 0.0)],
            ),
            "weight": 10,
            "detail": (
                f"月订阅支出 {monthly_subscription:,.2f} 元，"
                f"占估算月收入 {subscription_ratio * 100:.1f}%"
            ),
            "current": f"{subscription_ratio * 100:.1f}%",
            "benchmark": "固定订阅支出低于月收入 8%",
            "standard": "经常性支出负担率参考",
        },
        {
            "key": "security",
            "label": "账户安全",
            "score": max(0.0, 100.0 - len(risky_cards) * 15 - len(anomalies) * 8),
            "weight": 10,
            "detail": (
                f"{len(risky_cards)} 张卡存在冻结/挂失/交易限制，"
                f"近30天 {len(anomalies)} 项异常"
            ),
            "current": f"{len(risky_cards)} 张风险卡 / {len(anomalies)} 项异常",
            "benchmark": "无异常交易、无冻结或挂失遗留",
            "standard": "账户安全与反欺诈运营基线",
        },
        {
            "key": "goals",
            "label": "目标进度",
            "score": _piecewise_score(
                goal_ratio,
                [(0.0, 0.0), (0.25, 30.0), (0.5, 55.0), (0.8, 85.0), (1.0, 100.0)],
            ) if goals else 60.0,
            "weight": 10,
            "detail": (
                f"目标平均完成度 {goal_ratio * 100:.1f}%"
                if goals else "尚未建立明确资金目标"
            ),
            "current": f"{goal_ratio * 100:.1f}%" if goals else "未建立目标",
            "benchmark": "目标按截止日期持续推进",
            "standard": "目标导向预算与资金分账原则",
        },
    ]
    for item in dimensions:
        item["band"] = _score_band(item["score"])
        item["weighted_contribution"] = round(
            item["score"] * item["weight"] / 100,
            2,
        )
    total_weight = sum(item["weight"] for item in dimensions)
    score = round(
        sum(item["score"] * item["weight"] for item in dimensions)
        / max(total_weight, 1),
        1,
    )
    if score >= 85:
        grade = "优秀"
    elif score >= 70:
        grade = "良好"
    elif score >= 55:
        grade = "需改善"
    else:
        grade = "薄弱"
    weakest = sorted(dimensions, key=lambda item: item["score"])[:3]
    recommendations = []
    actions = {
        "liquidity": {
            "title": "补足应急金",
            "why": "应急金不足时，突发支出容易迫使你赎回理财或使用信用卡。",
            "steps": ["先建立 3 个月必要支出目标", "工资到账后自动划入货币基金或活期专户", "达到 3 个月后再提升到 6 个月"],
            "target": "3-6 个月必要支出",
        },
        "debt": {
            "title": "降低信用卡使用率",
            "why": "信用使用率过高会放大利息负担，也会降低家庭现金流弹性。",
            "steps": ["优先偿还已用额度", "账单日前部分还款", "将信用卡使用率控制在 30% 以下"],
            "target": "信用卡使用率 < 30%",
        },
        "savings": {
            "title": "提高自动储蓄率",
            "why": "先储蓄后消费比月底剩余再存更稳定，可减少非必要支出。",
            "steps": ["设置工资到账日自动转入", "把月储蓄率提高 5 个百分点", "每月复盘大额非必要支出"],
            "target": "储蓄率达到 20%",
        },
        "diversification": {
            "title": "降低投资集中度",
            "why": "资产类别过于集中时，单一市场波动会显著放大回撤。",
            "steps": ["先确认风险等级和投资期限", "逐步增加低相关资产类别", "单一产品占比不超过可投资资产的 40%"],
            "target": "至少覆盖 4 类资产",
        },
        "subscriptions": {
            "title": "压降固定订阅支出",
            "why": "高频小额自动续费会持续侵蚀现金流，并且容易被忽略。",
            "steps": ["核对近 3 个月自动续费", "取消低频服务", "给固定订阅设置月度上限"],
            "target": "订阅支出 < 月收入 8%",
        },
        "security": {
            "title": "处理账户安全风险",
            "why": "冻结、挂失和异常交易会直接影响资金可用性和账户安全。",
            "steps": ["复核异常交易", "处理冻结或挂失卡片", "检查交易开关与免密设置"],
            "target": "风险卡和异常交易清零",
        },
        "goals": {
            "title": "建立可跟踪的资金目标",
            "why": "没有目标和分账，储蓄容易被日常消费挤占。",
            "steps": ["创建 1-3 个近期目标", "设置目标金额和截止日期", "工资到账后自动分配资金"],
            "target": "目标按期完成率 > 80%",
        },
    }
    for item in weakest:
        action = actions[item["key"]]
        expected_gain = round((100 - item["score"]) * item["weight"] / 100, 1)
        recommendations.append({
            "dimension": item["key"],
            "title": action["title"],
            "detail": action["why"],
            "steps": action["steps"],
            "target": action["target"],
            "priority": "高" if item["score"] < 55 else "中",
            "expected_score_gain": expected_gain,
        })
    quality = build_data_quality(store, providers, clock, user_id)
    return {
        "score": score,
        "grade": grade,
        "dimensions": dimensions,
        "weakest": weakest,
        "recommendations": recommendations,
        "metrics": {
            "liquid_assets": round(liquid, 2),
            "liquidity_months": round(liquidity_months, 2),
            "credit_used_ratio": round(debt_ratio, 4),
            "savings_rate": round(savings_rate, 4),
            "subscription_ratio": round(subscription_ratio, 4),
        },
        "method": "7 个可解释维度加权评分，总分 100",
        "methodology": {
            "model": "分段基准映射 + 固定权重加权",
            "score_range": "0-100",
            "benchmarks": [
                "应急金 3-6 个月必要支出",
                "信用卡使用率低于 30%",
                "结余储蓄率 20%",
                "资产类别不少于 4 类",
                "月订阅支出低于收入 8%",
            ],
            "limitations": (
                "评分参考常见个人理财规划与消费金融风险阈值，"
                "但不是监管信用评分，也不替代银行授信审批。"
            ),
        },
        "data_quality": {
            **quality,
            "transaction_days": 180,
            "forecast_months": 3,
            "cards": len(cards),
            "holdings": len(holdings),
            "active_goals": len(goals),
            "source": "当前会话演示账户、流水、卡片、持仓与订阅",
        },
        "is_estimate": True,
    }


def build_event_center(
    store: DataStore,
    providers: Any,
    clock: Clock,
    user_id: str,
    risk_engine: Any = None,
) -> List[Dict[str, Any]]:
    today = clock.today()
    horizon = today + timedelta(days=30)
    forecast = build_cashflow_forecast(
        store,
        providers,
        clock,
        user_id,
        months=1,
    )
    events: List[Dict[str, Any]] = []
    for index, warning in enumerate(forecast["warnings"]):
        events.append({
            "event_id": f"EVT-CASH-{index}",
            "category": "cashflow",
            "level": warning["level"],
            "title": warning["title"],
            "detail": warning["detail"],
            "date": today.isoformat(),
            "action_prompt": "预测我未来90天的现金流",
        })
    for sub in providers.subscriptions.of(user_id, "active"):
        try:
            charge = date.fromisoformat(sub.next_charge_date[:10])
        except ValueError:
            continue
        if today <= charge <= horizon:
            events.append({
                "event_id": f"EVT-SUB-{sub.sub_id}",
                "category": "subscription",
                "level": "info",
                "title": f"{sub.merchant}即将续费",
                "detail": f"{charge.isoformat()} 预计扣款 {sub.amount:,.2f} 元",
                "date": charge.isoformat(),
                "action_prompt": f"取消{sub.merchant}订阅",
            })
    for card in providers.cards.of(user_id):
        if card.kind != "credit" or not card.statement_amount:
            continue
        try:
            due = date.fromisoformat(card.due_date)
        except ValueError:
            continue
        if today <= due <= horizon:
            events.append({
                "event_id": f"EVT-CARD-{card.card_id}",
                "category": "credit_card",
                "level": "warning" if due <= today + timedelta(days=7) else "info",
                "title": f"信用卡尾号{card.tail}还款日临近",
                "detail": f"{due.isoformat()} 本期应还 {card.statement_amount:,.2f} 元",
                "date": due.isoformat(),
                "action_prompt": "查看信用卡账单",
            })
    for task in providers.tasks.of(user_id, "pending"):
        try:
            run_date = date.fromisoformat(task.run_date[:10])
        except ValueError:
            continue
        if today <= run_date <= horizon:
            events.append({
                "event_id": f"EVT-TASK-{task.task_id}",
                "category": "task",
                "level": "info",
                "title": task.title or "待执行任务",
                "detail": f"计划日期 {run_date.isoformat()}",
                "date": run_date.isoformat(),
                "action_prompt": "我有哪些定时转账",
            })
    rows = store.transactions_of(
        user_id,
        (today - timedelta(days=30)).isoformat(),
        today.isoformat(),
    )
    if risk_engine is not None:
        for anomaly in risk_engine.detect_anomalies(user_id, rows)[:3]:
            events.append({
                "event_id": f"EVT-RISK-{anomaly.txn_id}",
                "category": "risk",
                "level": "danger" if anomaly.level == "danger" else "warning",
                "title": f"异常交易：{anomaly.merchant}",
                "detail": f"{anomaly.date} {abs(anomaly.amount):,.2f} 元",
                "date": anomaly.date,
                "action_prompt": f"给我{anomaly.merchant}的风险处置方案",
            })
    order = {"danger": 0, "warning": 1, "info": 2}
    ordered = sorted(
        events,
        key=lambda item: (order.get(item["level"], 3), item["date"]),
    )
    unique = []
    seen = set()
    for event in ordered:
        if event["event_id"] in seen:
            continue
        seen.add(event["event_id"])
        unique.append(event)
    return unique
