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

from finagent.data.official_sources import OfficialDataError, official_macro_context
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
    parsed_days = []
    for txn in income:
        parsed = _safe_date(txn.timestamp)
        if parsed is not None:
            parsed_days.append(parsed.day)
    if not parsed_days:
        return None
    days = Counter(parsed_days)
    day = days.most_common(1)[0][0]
    amounts = [abs(float(txn.amount)) for txn in income]
    median_amount = statistics.median(amounts)
    amount_std = (
        statistics.pstdev(amounts)
        if len(amounts) > 1 else 0.0
    )
    return {
        "day": day,
        "amount": round(median_amount, 2),
        "sample_std": round(amount_std, 2),
        "coefficient_of_variation": round(
            amount_std / median_amount,
            4,
        ) if median_amount > 0 else 0.0,
        "missing_probability": round(
            max(0.0, 1.0 - len(income) / 6.0),
            4,
        ),
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
    try:
        official_macro = official_macro_context()
    except (OfficialDataError, OSError, ValueError):
        official_macro = {
            "data_class": "official_public_data",
            "delivery": "unavailable",
            "status": "unavailable",
            "warnings": ["官方宏观快照不可用，本次未用模拟宏观数据替代。"],
        }

    liquid_cards = [
        card for card in providers.cards.of(user_id)
        if card.kind in {"debit", "virtual"} and card.status == "active"
    ]
    liquid_by_account: Dict[str, float] = {}
    for card in liquid_cards:
        account = store.accounts.get(card.account_id)
        balance = (
            float(account.balance)
            if account is not None and account.user_id == user_id
            else float(card.balance)
        )
        key = card.account_id or f"card:{card.card_id}"
        liquid_by_account[key] = max(balance, 0.0)
    start_balance = round(sum(liquid_by_account.values()), 2)
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
        txn
        for txn in recent
        if txn.amount < 0
        and txn.category not in excluded_categories
        and txn.subscription_id not in subscription_ids
        and not txn.is_subscription
    ]
    expense_by_day: Dict[date, float] = {}
    for txn in variable_expenses:
        txn_date = _safe_date(txn.timestamp)
        if txn_date is None:
            continue
        expense_by_day[txn_date] = (
            expense_by_day.get(txn_date, 0.0) + abs(float(txn.amount))
        )
    observed_days = min(max((today - recent_start).days, 0), 90)
    daily_variable_samples = [
        expense_by_day.get(recent_start + timedelta(days=offset), 0.0)
        for offset in range(observed_days)
    ]
    if daily_variable_samples and any(daily_variable_samples):
        variable_daily_base = statistics.fmean(daily_variable_samples)
        variable_daily_std = (
            statistics.pstdev(daily_variable_samples)
            if len(daily_variable_samples) > 1 else 0.0
        )
    else:
        variable_daily_base = 80.0
        variable_daily_std = 0.0
    variable_daily_base = round(
        variable_daily_base
        + max(float(extra_monthly_expense or 0), 0.0) / 30,
        2,
    )
    try:
        cpi_yoy_pct = max(float(official_macro.get("cpi_yoy_pct") or 0.0), 0.0)
    except (TypeError, ValueError):
        cpi_yoy_pct = 0.0
    inflation_adjustment_daily = round(
        variable_daily_base * cpi_yoy_pct / 100,
        2,
    )
    variable_daily = round(variable_daily_base + inflation_adjustment_daily, 2)

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
        if amount is None:
            amount = task.payload.get("budget")
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
    salary_event_by_day: Dict[date, float] = {}
    cursor = today
    salary_days: List[date] = []
    while cursor <= end:
        salary_days.append(cursor)
        cursor += timedelta(days=1)
    for run_date in salary_days:
        salary_event_by_day[run_date] = sum(
            event.amount
            for event in event_by_day.get(run_date, [])
            if event.kind == "income" and event.amount > 0
        )

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
    if not variable_expenses:
        warning_details.append({
            "level": "warning",
            "title": "日常支出样本不足",
            "detail": "近90天没有可用的非固定支出样本，概率模型按保守占位均值计算。",
        })

    confidence = min(
        0.95,
        0.30
        + min(len(daily_variable_samples), 90) / 180
        + min(len(recent), 120) / 240
        + (0.15 if salary else 0),
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
    horizon_days = months * 30
    event_income = round(sum(
        event.amount for event in events
        if event.amount > 0 and event.kind == "income"
    ), 2)
    subscription_outflow = round(sum(
        abs(event.amount) for event in events
        if event.amount < 0 and event.kind == "subscription"
    ), 2)
    debt_outflow = round(sum(
        abs(event.amount) for event in events
        if event.amount < 0 and event.kind == "credit_repayment"
    ), 2)
    scheduled_outflow = round(sum(
        abs(event.amount) for event in events
        if event.amount < 0 and event.kind == "scheduled_task"
    ), 2)
    planned_outflow = round(sum(
        abs(event.amount) for event in events
        if event.amount < 0 and event.kind in {
            "planned_purchase", "planned_investment",
        }
    ), 2)
    variable_outflow = round(variable_daily * horizon_days, 2)
    total_outflow = round(
        subscription_outflow + debt_outflow + scheduled_outflow
        + planned_outflow + variable_outflow,
        2,
    )
    net_cashflow = round(event_income - total_outflow, 2)
    category_totals: Counter = Counter()
    for txn in recent:
        if txn.amount < 0 and txn.category not in excluded_categories:
            category_totals[txn.category] += abs(txn.amount)
    top_categories = [
        {"category": category, "amount": round(amount, 2)}
        for category, amount in category_totals.most_common(5)
    ]
    buffer_gap = round(max(essential_monthly - min_balance, 0.0), 2)
    if buffer_gap > 0:
        daily_safe_budget = round(
            max(0.0, variable_daily - buffer_gap / max(horizon_days, 1)),
            2,
        )
    else:
        daily_safe_budget = round(
            variable_daily
            + max(min_balance - essential_monthly, 0.0)
            / max(horizon_days, 1),
            2,
        )

    actions: List[Dict[str, Any]] = []
    if negative_days or buffer_gap > 0:
        actions.append({
            "priority": "high",
            "title": "先补足现金缓冲",
            "detail": (
                f"最低余额可能降到 {min_balance:,.2f} 元，"
                f"距离一个月必要支出缓冲还差 {buffer_gap:,.2f} 元。"
            ),
            "action": "优先保留还款和必要支出，暂缓新增投资或大额消费。",
        })
        if variable_daily > 0:
            suggested_cut = round(buffer_gap / max(horizon_days, 1), 2)
            actions.append({
                "priority": "high",
                "title": "压缩非必要日常支出",
                "detail": (
                    f"未来 {horizon_days} 天平均每天少花约 "
                    f"{suggested_cut:,.2f} 元，可补上估算缓冲缺口。"
                ),
                "action": "从餐饮、购物和娱乐中优先设置每日预算。",
            })
    if subscription_outflow > 0:
        actions.append({
            "priority": "medium",
            "title": "复核即将续费的订阅",
            "detail": f"预测期内固定订阅支出约 {subscription_outflow:,.2f} 元。",
            "action": "检查低频会员，取消后重新预测。",
        })
    if debt_outflow > 0:
        actions.append({
            "priority": "high",
            "title": "先预留信用卡还款",
            "detail": f"预测期内包含信用卡还款预留 {debt_outflow:,.2f} 元。",
            "action": "这部分资金不要用于新增消费或高风险投资。",
        })
    if safe_to_spend > 0 and not negative_days:
        actions.append({
            "priority": "low",
            "title": "安排可安全使用的结余",
            "detail": f"保留必要缓冲后，可安排约 {safe_to_spend:,.2f} 元。",
            "action": "优先补充应急金，其次按风险等级做长期配置。",
        })
    if not actions:
        actions.append({
            "priority": "low",
            "title": "保持当前现金流节奏",
            "detail": "当前预测期内未发现明显资金缺口。",
            "action": "继续保留每月必要支出缓冲并定期更新账单。",
        })

    from finagent.analytics.data_quality import build_data_quality
    from finagent.analytics.probabilistic_cashflow import simulate_cashflow_paths

    probability = simulate_cashflow_paths(
        series,
        start_balance,
        variable_daily_mean=variable_daily,
        variable_daily_std=variable_daily_std,
        salary_by_day=[
            salary_event_by_day.get(run_date, 0.0)
            for run_date in salary_days
        ],
        salary_cv=float((salary or {}).get("coefficient_of_variation") or 0.0),
        salary_miss_probability=float(
            (salary or {}).get("missing_probability") or 0.0
        ),
        variable_observation_days=len(daily_variable_samples),
    )
    data_quality = build_data_quality(store, providers, clock, user_id)

    return {
        "as_of": today.isoformat(),
        "horizon_days": horizon_days,
        "start_balance": start_balance,
        "end_balance": series[-1]["balance"] if series else start_balance,
        "min_balance": round(min_balance, 2),
        "min_balance_date": min_date.isoformat(),
        "essential_monthly": essential_monthly,
        "safe_to_spend": safe_to_spend,
        "daily_safe_budget": daily_safe_budget,
        "buffer_gap": buffer_gap,
        "negative_days": negative_days,
        "variable_daily_expense": variable_daily,
        "variable_daily_expense_std": round(variable_daily_std, 2),
        "variable_expense_sample_days": len(daily_variable_samples),
        "variable_daily_expense_base": variable_daily_base,
        "official_inflation_adjustment_daily": inflation_adjustment_daily,
        "salary": salary,
        "events": [event.payload() for event in sorted(events, key=lambda item: item.event_date)],
        "series": series,
        "warnings": warning_details,
        "recommendation": recommendation,
        "scenario": {
            "months": months,
            "purchase_amount": round(float(purchase_amount or 0), 2),
            "purchase_date": purchase_date.isoformat() if purchase_date else "",
            "income_change_pct": round(float(income_change_pct or 0), 2),
            "extra_monthly_expense": round(float(extra_monthly_expense or 0), 2),
            "investment_amount": round(float(investment_amount or 0), 2),
            "investment_return_pct": round(float(investment_return_pct or 0), 2),
            "is_baseline": not any([
                float(purchase_amount or 0),
                float(income_change_pct or 0),
                float(extra_monthly_expense or 0),
                float(investment_amount or 0),
            ]),
        },
        "cashflow_breakdown": {
            "income": event_income,
            "subscriptions": subscription_outflow,
            "credit_repayment": debt_outflow,
            "scheduled_tasks": scheduled_outflow,
            "planned_outflows": planned_outflow,
            "variable_expenses": variable_outflow,
            "total_outflow": total_outflow,
            "net_cashflow": net_cashflow,
        },
        "top_spending_categories": top_categories,
        "action_plan": actions,
        "confidence": round(confidence, 2),
        "probability": probability,
        "data_quality": data_quality,
        "model_audit": {
            "deterministic_model": "cashflow-deterministic-v1",
            "probabilistic_model": probability.get("model_version"),
            "as_of": today.isoformat(),
            "horizon_days": horizon_days,
            "data_fingerprint": probability.get("input_fingerprint", ""),
            "data_versions": {
                "official_macro_status": official_macro.get("status", ""),
                "official_macro_delivery": official_macro.get("delivery", ""),
                "official_macro_as_of": official_macro.get("as_of", ""),
                "official_macro_fetched_at": official_macro.get("fetched_at", ""),
                "transaction_count_180d": len(transactions),
                "transaction_count_90d": len(recent),
                "variable_expense_sample_days": len(daily_variable_samples),
            },
            "parameters": {
                "months": months,
                "purchase_amount": round(float(purchase_amount or 0), 2),
                "income_change_pct": round(float(income_change_pct or 0), 2),
                "extra_monthly_expense": round(
                    float(extra_monthly_expense or 0), 2
                ),
                "investment_amount": round(float(investment_amount or 0), 2),
                "investment_return_pct": round(
                    float(investment_return_pct or 0), 2
                ),
                "simulations": probability.get("simulations"),
                "random_seed": probability.get("random_seed"),
            },
            "reproducibility_scope": (
                "相同业务快照、固定时钟、同一官方宏观缓存、相同参数、"
                "模拟次数和 Python 标准库版本下可重复；外部行情或宏观刷新会改变输入。"
            ),
        },
        "assumptions": [
            "工资按近180天最常见入账日与中位金额估算",
            "订阅按当前计划续费",
            "信用卡按当前已用额度做一次还款预留",
            "日常支出按近90天非固定支出均值估算",
            (
                "日常支出按世界银行中国年度 CPI 做温和通胀调整"
                if official_macro.get("cpi_yoy_pct") is not None
                else "官方 CPI 不可用，日常支出未做额外通胀调整"
            ),
            (
                "人民银行7天逆回购利率和中国国债10年期收益率仅作为"
                "利率环境参照，不直接充当理财收益"
            ),
        ],
        "model_limitations": [
            "这是赛事情景模型，不构成真实现金流承诺",
            "突发收入、临时消费和银行账单日变化可能使结果偏离",
            "官方年度 CPI 不反映当月价格变化，宏观情绪与政策变化也未全部量化",
        ],
        "data_scope": data_scope,
        "uploaded_transaction_count": len(imported),
        "uses_uploaded_bills": bool(imported),
        "official_macro": official_macro,
    }
