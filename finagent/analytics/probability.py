"""Seeded empirical cash-flow distribution for the financial digital twin."""
from __future__ import annotations

import hashlib
import math
import random
import statistics
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List

from finagent.analytics.cashflow import build_cashflow_forecast
from finagent.data.store import Clock, DataStore


QUANTILE_KEYS = ("p05", "p25", "p50", "p75", "p95")


def _safe_date(raw: str) -> date | None:
    try:
        return date.fromisoformat(str(raw or "")[:10])
    except ValueError:
        return None


def _percentile(values: Iterable[float], q: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return round(ordered[0], 2)
    position = (len(ordered) - 1) * q
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return round(ordered[lower], 2)
    ratio = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * ratio, 2)


def _quantiles(values: Iterable[float]) -> Dict[str, float]:
    rows = list(values)
    return {
        "p05": _percentile(rows, 0.05),
        "p25": _percentile(rows, 0.25),
        "p50": _percentile(rows, 0.50),
        "p75": _percentile(rows, 0.75),
        "p95": _percentile(rows, 0.95),
    }


def _stable_seed(
    user_id: str,
    as_of: str,
    months: int,
    scenario_values: Iterable[float],
) -> int:
    payload = "|".join(
        [
            user_id,
            as_of,
            str(months),
            ",".join(f"{float(value):.6f}" for value in scenario_values),
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _daily_variable_expenses(
    transactions: Iterable[Any],
    today: date,
    subscription_ids: set[str],
) -> List[float]:
    start = today - timedelta(days=89)
    totals: Dict[date, float] = {
        start + timedelta(days=offset): 0.0 for offset in range(90)
    }
    excluded = {"收入", "转账", "信用卡还款", "理财"}
    for txn in transactions:
        txn_date = _safe_date(getattr(txn, "timestamp", ""))
        if txn_date is None or txn_date < start or txn_date > today:
            continue
        if txn.amount >= 0 or txn.category in excluded:
            continue
        if txn.subscription_id in subscription_ids or getattr(txn, "is_subscription", False):
            continue
        totals[txn_date] = totals.get(txn_date, 0.0) + abs(float(txn.amount))
    return [round(totals[day], 2) for day in sorted(totals)]


def _salary_samples(transactions: Iterable[Any]) -> List[float]:
    samples = []
    for txn in transactions:
        if txn.amount < 1000:
            continue
        is_salary = (
            txn.category == "收入"
            or "工资" in txn.merchant
            or "工资" in (txn.note or "")
        )
        if is_salary:
            samples.append(abs(float(txn.amount)))
    return samples


def _event_map(forecast: Dict[str, Any]) -> Dict[date, List[Dict[str, Any]]]:
    result: Dict[date, List[Dict[str, Any]]] = {}
    for event in forecast.get("events") or []:
        event_date = _safe_date(str(event.get("date") or ""))
        if event_date is None:
            continue
        result.setdefault(event_date, []).append(event)
    return result


def build_probability_forecast(
    store: DataStore,
    providers: Any,
    clock: Clock,
    user_id: str,
    *,
    months: int = 3,
    simulations: int = 400,
    seed: int | None = None,
    purchase_amount: float = 0.0,
    purchase_date: date | None = None,
    income_change_pct: float = 0.0,
    extra_monthly_expense: float = 0.0,
    investment_amount: float = 0.0,
    investment_return_pct: float = 0.0,
) -> Dict[str, Any]:
    """Build deterministic-seed empirical paths around the point forecast."""
    months = max(1, min(int(months or 3), 12))
    simulations = max(50, min(int(simulations or 400), 2000))
    baseline = build_cashflow_forecast(
        store,
        providers,
        clock,
        user_id,
        months=months,
        purchase_amount=purchase_amount,
        purchase_date=purchase_date,
        income_change_pct=income_change_pct,
        extra_monthly_expense=extra_monthly_expense,
        investment_amount=investment_amount,
        investment_return_pct=investment_return_pct,
    )
    today = clock.today()
    lookback_start = today - timedelta(days=180)
    transactions = store.transactions_of(
        user_id,
        lookback_start.isoformat(),
        today.isoformat(),
    )
    subscription_ids = {
        sub.sub_id for sub in providers.subscriptions.of(user_id, "active")
    }
    expense_samples = _daily_variable_expenses(
        transactions,
        today,
        subscription_ids,
    )
    salary_samples = _salary_samples(transactions)
    if not expense_samples:
        expense_samples = [float(baseline.get("variable_daily_expense") or 0.0)]
    if not salary_samples and baseline.get("salary"):
        salary_samples = [float(baseline["salary"].get("amount") or 0.0)]

    scenario_values = [
        float(purchase_amount or 0),
        float(income_change_pct or 0),
        float(extra_monthly_expense or 0),
        float(investment_amount or 0),
        float(investment_return_pct or 0),
    ]
    actual_seed = (
        int(seed)
        if seed is not None
        else _stable_seed(user_id, today.isoformat(), months, scenario_values)
    )
    rng = random.Random(actual_seed)
    events_by_day = _event_map(baseline)
    start_balance = float(baseline.get("start_balance") or 0.0)
    horizon_days = int(baseline.get("horizon_days") or months * 30)
    dates = [today + timedelta(days=offset) for offset in range(horizon_days + 1)]

    salary_median = statistics.median(salary_samples) if salary_samples else 0.0
    path_balances: List[List[float]] = []
    path_minima: List[float] = []
    negative_paths = 0
    for _ in range(simulations):
        balance = start_balance
        minimum = balance
        salary_ratio = 1.0
        if salary_samples and salary_median > 0:
            salary_ratio = rng.choice(salary_samples) / salary_median
        elif salary_samples:
            salary_ratio = rng.choice(salary_samples)
        else:
            salary_ratio = rng.uniform(0.90, 1.10)
        balances: List[float] = [round(balance, 2)]
        for current in dates[:-1]:
            daily_income = 0.0
            daily_expense = 0.0
            for event in events_by_day.get(current, []):
                amount = float(event.get("amount") or 0.0)
                if event.get("kind") == "income":
                    amount *= salary_ratio
                balance += amount
                if amount >= 0:
                    daily_income += amount
                else:
                    daily_expense += abs(amount)
            variable_spend = rng.choice(expense_samples)
            balance -= variable_spend
            daily_expense += variable_spend
            balance = round(balance, 2)
            balances.append(balance)
            minimum = min(minimum, balance)
        path_balances.append(balances)
        path_minima.append(round(minimum, 2))
        if minimum < 0:
            negative_paths += 1

    daily_quantiles: List[Dict[str, Any]] = []
    for index, current in enumerate(dates):
        values = [path[index] for path in path_balances]
        row = {"date": current.isoformat(), **_quantiles(values)}
        daily_quantiles.append(row)

    terminal_values = [path[-1] for path in path_balances]
    terminal_quantiles = _quantiles(terminal_values)
    min_quantiles = _quantiles(path_minima)
    tail_count = max(1, int(math.ceil(len(terminal_values) * 0.05)))
    worst_terminal = sorted(terminal_values)[:tail_count]
    worst_minima = sorted(path_minima)[:tail_count]

    return {
        "as_of": today.isoformat(),
        "horizon_days": horizon_days,
        "scenario_count": simulations,
        "random_seed": actual_seed,
        "method": "seeded empirical bootstrap of daily expenses and salary amounts",
        "baseline": baseline,
        "daily_quantiles": daily_quantiles,
        "terminal_quantiles": terminal_quantiles,
        "min_balance_quantiles": min_quantiles,
        "intervals": {
            "terminal_50": [terminal_quantiles["p25"], terminal_quantiles["p75"]],
            "terminal_90": [terminal_quantiles["p05"], terminal_quantiles["p95"]],
            "minimum_50": [min_quantiles["p25"], min_quantiles["p75"]],
            "minimum_90": [min_quantiles["p05"], min_quantiles["p95"]],
        },
        "mean_terminal_balance": round(statistics.fmean(terminal_values), 2),
        "negative_probability": round(negative_paths / simulations, 4),
        "worst_5pct": {
            "mean_terminal_balance": round(statistics.fmean(worst_terminal), 2),
            "mean_min_balance": round(statistics.fmean(worst_minima), 2),
        },
        "sample_support": {
            "expense_days": len(expense_samples),
            "salary_samples": len(salary_samples),
        },
        "assumptions": [
            "Daily variable spending is bootstrapped from the last 90 days.",
            "Salary amounts vary within observed historical samples.",
            "Subscriptions, scheduled tasks, and card repayments remain fixed.",
            "Quantile ranges are simulated estimates, not future guarantees.",
        ],
    }


def probability_risk_budget(
    forecast: Dict[str, Any],
    *,
    base_risk_aversion: float = 0.30,
    scale: float = 2.50,
    max_extra: float = 0.30,
) -> Dict[str, Any]:
    """Translate downside interval width into a transparent risk-aversion adjustment."""
    p50 = float(forecast["terminal_quantiles"]["p50"])
    p05 = float(forecast["terminal_quantiles"]["p05"])
    tail_loss_ratio = max(0.0, (p50 - p05) / max(abs(p50), 1.0))
    risk_aversion = base_risk_aversion + min(
        max_extra,
        tail_loss_ratio * scale,
    )
    return {
        "tail_loss_pct": round(tail_loss_ratio, 4),
        "base_risk_aversion": round(base_risk_aversion, 4),
        "risk_aversion": round(risk_aversion, 4),
        "method": "base risk aversion plus P50-to-P05 downside width",
    }
