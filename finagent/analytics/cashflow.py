"""Cash-flow digital twin for the prototype customer.

The forecast is deterministic and intentionally conservative. It uses the
customer's current liquid-card balances, recent income pattern, active
subscriptions, pending scheduled tasks and an estimated variable-spend rate.
"""
from __future__ import annotations

import calendar
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional

from finagent.data.store import Clock, DataStore


@dataclass(frozen=True)
class ForecastEvent:
    event_date: date
    kind: str
    label: str
    amount: float
    source: str

    def payload(self) -> Dict[str, Any]:
        return {
            "date": self.event_date.isoformat(),
            "kind": self.kind,
            "label": self.label,
            "amount": round(self.amount, 2),
            "source": self.source,
        }


def _month_add(value: date, months: int) -> date:
    index = value.year * 12 + value.month - 1 + months
    year, month_index = divmod(index, 12)
    month = month_index + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _safe_date(raw: str) -> Optional[date]:
    try:
        return date.fromisoformat(str(raw or "")[:10])
    except ValueError:
        return None


def _salary_plan(transactions: Iterable[Any]) -> Optional[Dict[str, Any]]:
    income = [
        txn for txn in transactions
        if txn.amount >= 1000
        and (
            txn.category == "收入"
            or "工资" in txn.merchant
            or "工资" in (txn.note or "")
        )
    ]
    if not income:
        return None
    days = Counter(int(txn.timestamp[8:10]) for txn in income if len(txn.timestamp) >= 10)
    day = days.most_common(1)[0][0]
    return {
        "day": day,
        "amount": round(statistics.median(abs(txn.amount) for txn in income), 2),
        "samples": len(income),
    }


def _subscription_dates(start: date, end: date, first_raw: str, cycle: str) -> List[date]:
    current = _safe_date(first_raw)
    if current is None:
        return []
    while current < start:
        if cycle == "weekly":
            current += timedelta(days=7)
        elif cycle == "yearly":
            current = _month_add(current, 12)
        else:
            current = _month_add(current, 1)
    result: List[date] = []
    while current <= end:
        result.append(current)
        if cycle == "weekly":
            current += timedelta(days=7)
        elif cycle == "yearly":
            current = _month_add(current, 12)
        else:
            current = _month_add(current, 1)
    return result


def build_cashflow_forecast(
    store: DataStore,
    providers: Any,
    clock: Clock,
    user_id: str,
    *,
    months: int = 3,
    purchase_amount: float = 0.0,
    purchase_date: Optional[date] = None,
    income_change_pct: float = 0.0,
    extra_monthly_expense: float = 0.0,
    investment_amount: float = 0.0,
    investment_return_pct: float = 0.0,
) -> Dict[str, Any]:
    """Build a daily 30/60/90-day liquidity projection."""
    months = max(1, min(int(months or 3), 12))
    today = clock.today()
    end = today + timedelta(days=months * 30)
    lookback_start = today - timedelta(days=180)
    transactions = store.transactions_of(
        user_id,
        lookback_start.isoformat(),
        today.isoformat(),
    )
    recent_start = today - timedelta(days=90)
    recent = [
        txn for txn in transactions
        if _safe_date(txn.timestamp) and _safe_date(txn.timestamp) >= recent_start
    ]
    imported = [
        txn for txn in transactions
        if "receipt_import" in txn.tags or txn.source.startswith("upload:")
    ]

    liquid_cards = [
        card for card in providers.cards.of(user_id)
        if card.kind in {"debit", "virtual"} and card.status == "active"
    ]
    start_balance = round(sum(max(card.balance, 0.0) for card in liquid_cards), 2)
    salary = _salary_plan(transactions)
    if salary:
        salary["amount"] = round(
            salary["amount"] * (1 + float(income_change_pct or 0) / 100),
            2,
        )

    subscription_ids = {
        sub.sub_id for sub in providers.subscriptions.of(user_id, "active")
    }
    excluded_categories = {"收入", "转账", "信用卡还款", "理财"}
    variable_expenses = [
        abs(txn.amount)
        for txn in recent
        if txn.amount < 0
        and txn.category not in excluded_categories
        and txn.subscription_id not in subscription_ids
        and not txn.is_subscription
    ]
    variable_daily = round(
        (sum(variable_expenses) / 90 if variable_expenses else 80.0)
        + max(float(extra_monthly_expense or 0), 0.0) / 30,
        2,
    )

    events: List[ForecastEvent] = []
    if salary:
        day = min(int(salary["day"]), 28)
        cursor = date(today.year, today.month, day)
        if cursor < today:
            cursor = _month_add(cursor, 1)
        while cursor <= end:
            events.append(ForecastEvent(
                cursor,
                "income",
                "预计工资入账",
                float(salary["amount"]),
                "近180天工资流水模式",
            ))
            cursor = _month_add(cursor, 1)

    for sub in providers.subscriptions.of(user_id, "active"):
        for run_date in _subscription_dates(
            today,
            end,
            sub.next_charge_date,
            sub.cycle,
        ):
            events.append(ForecastEvent(
                run_date,
                "subscription",
                f"{sub.merchant}自动续费",
                -abs(float(sub.amount)),
                "订阅计划",
            ))

    for task in providers.tasks.of(user_id, "pending"):
        run_date = _safe_date(task.run_date)
        if run_date is None or run_date < today or run_date > end:
            continue
        amount = task.payload.get("amount")
        if isinstance(amount, (int, float)):
            events.append(ForecastEvent(
                run_date,
                "scheduled_task",
                task.title or "定时任务",
                -abs(float(amount)),
                "待执行任务",
            ))

    credit_cards = [
        card for card in providers.cards.of(user_id)
        if card.kind == "credit" and card.used > 0
    ]
    if credit_cards:
        due = date(today.year, today.month, 15)
        if due < today:
            due = _month_add(due, 1)
        if due <= end:
            events.append(ForecastEvent(
                due,
                "credit_repayment",
                "信用卡账单预留",
                -round(sum(card.used for card in credit_cards), 2),
                "当前已用额度",
            ))

    if purchase_amount and purchase_amount > 0:
        scenario_date = purchase_date or today
        if today <= scenario_date <= end:
            events.append(ForecastEvent(
                scenario_date,
                "planned_purchase",
                "计划消费",
                -float(purchase_amount),
                "用户情景参数",
            ))

    if investment_amount and investment_amount > 0:
        events.append(ForecastEvent(
            today,
            "planned_investment",
            "计划转入投资账户",
            -float(investment_amount),
            "用户情景参数",
        ))
        monthly_return = (
            float(investment_amount)
            * max(float(investment_return_pct), 0.0)
            / 100
            / 12
        )
        if monthly_return > 0:
            cursor = _month_add(today, 1)
            while cursor <= end:
                events.append(ForecastEvent(
                    cursor,
                    "investment_return",
                    "投资情景收益测算",
                    round(monthly_return, 2),
                    "情景预期年化",
                ))
                cursor = _month_add(cursor, 1)

    event_by_day: Dict[date, List[ForecastEvent]] = {}
    for event in events:
        event_by_day.setdefault(event.event_date, []).append(event)

    balance = start_balance
    min_balance = balance
    min_date = today
    negative_days = 0
    series: List[Dict[str, Any]] = []
    cursor = today
    while cursor <= end:
        income = 0.0
        expense = 0.0
        labels: List[str] = []
        for event in event_by_day.get(cursor, []):
            balance += event.amount
            if event.amount >= 0:
                income += event.amount
            else:
                expense += abs(event.amount)
            labels.append(event.label)
        balance -= variable_daily
        expense += variable_daily
        balance = round(balance, 2)
        if balance < min_balance:
            min_balance = balance
            min_date = cursor
        if balance < 0:
            negative_days += 1
        series.append({
            "date": cursor.isoformat(),
            "balance": balance,
            "income": round(income, 2),
            "expense": round(expense, 2),
            "events": labels,
            "negative": balance < 0,
        })
        cursor += timedelta(days=1)

    fixed_monthly = sum(
        abs(event.amount)
        for event in events
        if event.kind == "subscription"
    ) / max(months, 1)
    essential_monthly = round(variable_daily * 30 + fixed_monthly, 2)
    warning_details: List[Dict[str, Any]] = []
    safe_to_spend = round(max(min_balance - essential_monthly, 0.0), 2)
    if negative_days:
        warning_details.append({
            "level": "danger",
            "title": "预计出现流动性缺口",
            "detail": f"未来{months * 30}天有 {negative_days} 天余额可能低于 0 元。",
        })
    elif min_balance < essential_monthly:
        warning_details.append({
            "level": "warning",
            "title": "应急金缓冲不足",
            "detail": (
                f"最低余额约 {min_balance:,.2f} 元，"
                f"低于建议的 1 个月必要支出 {essential_monthly:,.2f} 元。"
            ),
        })
    if not salary:
        warning_details.append({
            "level": "warning",
            "title": "未识别稳定收入",
            "detail": "预测按无固定工资处理，结论偏保守。",
        })

    confidence = min(
        0.95,
        0.45 + min(len(recent), 90) / 180 + (0.15 if salary else 0),
    )
    recommendation = (
        "建议先保留必要支出和信用卡还款资金，再考虑新增投资或大额消费。"
        if safe_to_spend <= 0
        else f"在保留应急缓冲后，可安排约 {safe_to_spend:,.2f} 元用于目标储蓄或投资。"
    )
    data_scope = "演示账户 + 历史流水 + 订阅/任务计划"
    if imported:
        data_scope = (
            f"演示账户 + 已导入账单 {len(imported)} 笔 + "
            "历史流水 + 订阅/任务计划"
        )
    return {
        "as_of": today.isoformat(),
        "horizon_days": months * 30,
        "start_balance": start_balance,
        "end_balance": series[-1]["balance"] if series else start_balance,
        "min_balance": round(min_balance, 2),
        "min_balance_date": min_date.isoformat(),
        "essential_monthly": essential_monthly,
        "safe_to_spend": safe_to_spend,
        "negative_days": negative_days,
        "variable_daily_expense": variable_daily,
        "salary": salary,
        "events": [event.payload() for event in sorted(events, key=lambda item: item.event_date)],
        "series": series,
        "warnings": warning_details,
        "recommendation": recommendation,
        "confidence": round(confidence, 2),
        "assumptions": [
            "工资按近180天最常见入账日与中位金额估算",
            "订阅按当前计划续费",
            "信用卡按当前已用额度做一次还款预留",
            "日常支出按近90天非固定支出均值估算",
        ],
        "model_limitations": [
            "这是赛事情景模型，不构成真实现金流承诺",
            "突发收入、临时消费和银行账单日变化可能使结果偏离",
        ],
        "data_scope": data_scope,
        "uploaded_transaction_count": len(imported),
        "uses_uploaded_bills": bool(imported),
    }
