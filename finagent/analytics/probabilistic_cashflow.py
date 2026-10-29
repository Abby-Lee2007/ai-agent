"""Seeded probabilistic cash-flow paths and portfolio-level percentiles."""
from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
import sys
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from finagent import __version__


MODEL_VERSION = "probabilistic-cashflow-v1"
DEFAULT_SIMULATIONS = 600
DEFAULT_SEED = 20261029
QUANTILES = (0.05, 0.25, 0.50, 0.75, 0.95)


def _quantile(sorted_values: Sequence[float], probability: float) -> float:
    """Linearly interpolated empirical quantile with inclusive endpoints."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return round(float(sorted_values[0]), 2)
    position = (len(sorted_values) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return round(float(sorted_values[lower]), 2)
    fraction = position - lower
    value = (
        float(sorted_values[lower]) * (1.0 - fraction)
        + float(sorted_values[upper]) * fraction
    )
    return round(value, 2)


def _distribution(values: Sequence[float]) -> Dict[str, float]:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {}
    result = {
        f"p{int(probability * 100):02d}": _quantile(ordered, probability)
        for probability in QUANTILES
    }
    result.update({
        "mean": round(statistics.fmean(ordered), 2),
        "std": round(statistics.pstdev(ordered), 2) if len(ordered) > 1 else 0.0,
        "min": round(ordered[0], 2),
        "max": round(ordered[-1], 2),
    })
    return result


def summarize_probability_paths(
    paths: Sequence[Sequence[float]],
    dates: Sequence[date],
    start_balance: float,
    *,
    simulations_requested: Optional[int] = None,
    seed: int = DEFAULT_SEED,
    input_fingerprint: str = "",
) -> Dict[str, Any]:
    """Summarize terminal and whole-path liquidity distributions.

    Terminal percentiles describe the final-day balance only. Minimum-balance
    percentiles first take the minimum of each simulated path and then compare
    those path minima. They must not be treated as the same distribution.
    """
    if not paths:
        raise ValueError("至少需要一条模拟路径")
    horizon = len(dates)
    if horizon == 0:
        raise ValueError("预测日期不能为空")
    normalized_paths: List[List[float]] = []
    for path in paths:
        if len(path) != horizon:
            raise ValueError("每条模拟路径长度必须与日期数量一致")
        values = [float(value) for value in path]
        if any(not math.isfinite(value) for value in values):
            raise ValueError("模拟路径包含 NaN 或无穷大")
        normalized_paths.append(values)

    terminal = [path[-1] for path in normalized_paths]
    path_minima = [min(path) for path in normalized_paths]
    path_count = len(normalized_paths)
    tail_count = max(1, int(math.ceil(path_count * 0.05)))
    ordered_minima = sorted(path_minima)
    worst_liquidity_indices = sorted(
        range(path_count),
        key=lambda index: (path_minima[index], terminal[index]),
    )[:tail_count]
    terminal_losses = [
        max(float(start_balance) - value, 0.0)
        for value in terminal
    ]
    worst_loss_indices = sorted(
        range(path_count),
        key=lambda index: (terminal_losses[index], -path_minima[index]),
        reverse=True,
    )[:tail_count]
    liquidity_tail_minima = [
        path_minima[index] for index in worst_liquidity_indices
    ]
    liquidity_tail_terminal = [
        terminal[index] for index in worst_liquidity_indices
    ]
    loss_tail_losses = [
        terminal_losses[index] for index in worst_loss_indices
    ]
    loss_tail_minima = [
        path_minima[index] for index in worst_loss_indices
    ]
    negative_end = sum(1 for value in terminal if value < 0) / path_count
    any_negative = sum(1 for value in path_minima if value < 0) / path_count
    daily_negative = [
        sum(1 for path in normalized_paths if path[index] < 0) / path_count
        for index in range(horizon)
    ]
    daily_balance_quantiles = []
    for index, item_date in enumerate(dates):
        distribution = _distribution([
            path[index] for path in normalized_paths
        ])
        daily_balance_quantiles.append({
            "date": item_date.isoformat(),
            **{
                key: distribution[key]
                for key in ("p05", "p25", "p50", "p75", "p95")
            },
        })
    return {
        "model_version": MODEL_VERSION,
        "simulations": path_count,
        "simulations_requested": simulations_requested or path_count,
        "random_seed": int(seed),
        "quantile_method": "sorted empirical distribution; linear interpolation at (n-1)q",
        "time_dimension": (
            "每日收盘余额；终点分位使用最后一日余额，最低余额分位先取每条路径的全周期最小值"
        ),
        "terminal_balance": _distribution(terminal),
        "minimum_balance": {
            **_distribution(path_minima),
            "cvar_5": round(statistics.fmean(
                sorted(path_minima)[:tail_count]
            ), 2),
        },
        "intervals": {
            "p50_range_50pct": [
                _quantile(sorted(terminal), 0.25),
                _quantile(sorted(terminal), 0.75),
            ],
            "p90_range_90pct": [
                _quantile(sorted(terminal), 0.05),
                _quantile(sorted(terminal), 0.95),
            ],
            "definition": "终点余额的 50% 与 90% 分位区间",
        },
        "negative_balance": {
            "probability_negative_end": round(negative_end, 6),
            "probability_any_negative_during_period": round(any_negative, 6),
            "daily_probability": [
                {
                    "date": dates[index].isoformat(),
                    "probability": round(daily_negative[index], 6),
                }
                for index in range(horizon)
            ],
            "max_daily_probability": round(max(daily_negative), 6),
        },
        "daily_balance_quantiles": daily_balance_quantiles,
        "worst_tail": {
            "tail_fraction": 0.05,
            "path_count": tail_count,
            "selection_criterion": (
                "按 max(期初余额-期末余额,0) 降序，损失相同再按路径最低余额升序"
            ),
            "mean_min_balance": round(statistics.fmean(loss_tail_minima), 2),
            "mean_terminal_loss": round(statistics.fmean(loss_tail_losses), 2),
            "cvar_5_terminal_loss": round(
                statistics.fmean(loss_tail_losses),
                2,
            ),
        },
        "worst_liquidity_tail": {
            "tail_fraction": 0.05,
            "path_count": tail_count,
            "selection_criterion": (
                "按每条路径全周期最低余额升序，余额相同再按终点余额升序"
            ),
            "mean_min_balance": round(
                statistics.fmean(liquidity_tail_minima),
                2,
            ),
            "mean_terminal_balance": round(
                statistics.fmean(liquidity_tail_terminal),
                2,
            ),
            "cvar_5_terminal_loss": round(statistics.fmean([
                max(float(start_balance) - value, 0.0)
                for value in liquidity_tail_terminal
            ]), 2),
        },
        "calibration": {
            "terminal_vs_path_minimum": (
                "终点 P05 与最低余额 P05 是不同统计量；负余额概率同时返回"
                "终点、整条路径和逐日三种口径"
            ),
            "tail_count_formula": "ceil(模拟次数 × 5%)，至少 1 条",
            "cvar_definition": (
                "最差 5% 路径的终点损失 max(期初余额-期末余额,0) 的算术平均"
            ),
        },
        "input_fingerprint": input_fingerprint,
    }


def _gamma_expense_sample(
    rng: random.Random,
    mean: float,
    standard_deviation: float,
) -> float:
    if mean <= 0:
        return 0.0
    if standard_deviation <= 0:
        return mean
    shape = max((mean / standard_deviation) ** 2, 0.25)
    scale = (standard_deviation ** 2) / mean
    return max(rng.gammavariate(shape, scale), 0.0)


def simulate_cashflow_paths(
    deterministic_series: Sequence[Dict[str, Any]],
    start_balance: float,
    *,
    variable_daily_mean: float,
    variable_daily_std: float,
    salary_by_day: Optional[Sequence[float]] = None,
    salary_cv: float = 0.0,
    salary_miss_probability: float = 0.0,
    simulations: int = DEFAULT_SIMULATIONS,
    seed: int = DEFAULT_SEED,
    variable_observation_days: int = 0,
) -> Dict[str, Any]:
    """Simulate daily paths while holding contractual events deterministic."""
    if len(deterministic_series) == 0:
        raise ValueError("确定性现金流序列不能为空")
    simulations = max(100, min(int(simulations), 20_000))
    variable_mean = max(float(variable_daily_mean), 0.0)
    variable_std = max(float(variable_daily_std), 0.0)
    salary_values = list(salary_by_day or [0.0] * len(deterministic_series))
    if len(salary_values) != len(deterministic_series):
        raise ValueError("工资序列长度与现金流序列不一致")

    balances = [float(item["balance"]) for item in deterministic_series]
    daily_deltas = []
    previous = float(start_balance)
    for balance in balances:
        daily_deltas.append(balance - previous)
        previous = balance

    dates = [date.fromisoformat(str(item["date"])) for item in deterministic_series]
    rng = random.Random(int(seed))
    paths: List[List[float]] = []
    for _ in range(simulations):
        balance = float(start_balance)
        path: List[float] = []
        for index, deterministic_delta in enumerate(daily_deltas):
            salary_amount = max(float(salary_values[index]), 0.0)
            other_events = deterministic_delta - salary_amount + variable_mean
            salary_sample = salary_amount
            if salary_amount > 0:
                if salary_miss_probability > 0 and rng.random() < salary_miss_probability:
                    salary_sample = 0.0
                elif salary_cv > 0:
                    salary_sample *= max(
                        0.50,
                        min(1.50, rng.gauss(1.0, salary_cv)),
                    )
            variable_expense = _gamma_expense_sample(
                rng,
                variable_mean,
                variable_std,
            )
            balance += other_events + salary_sample - variable_expense
            path.append(round(balance, 2))
        paths.append(path)

    fingerprint_payload = {
        "dates": [item.isoformat() for item in dates],
        "start_balance": round(float(start_balance), 2),
        "daily_deltas": [round(value, 8) for value in daily_deltas],
        "variable_daily_mean": round(variable_mean, 8),
        "variable_daily_std": round(variable_std, 8),
        "salary_by_day": [round(value, 8) for value in salary_values],
        "salary_cv": round(float(salary_cv), 8),
        "salary_miss_probability": round(float(salary_miss_probability), 8),
        "simulations": simulations,
        "seed": int(seed),
        "model_version": MODEL_VERSION,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    summary = summarize_probability_paths(
        paths,
        dates,
        start_balance,
        simulations_requested=simulations,
        seed=seed,
        input_fingerprint=fingerprint,
    )
    summary["audit"] = {
        "model_version": MODEL_VERSION,
        "project_version": __version__,
        "input_fingerprint": fingerprint,
        "random_engine": "Python random.Random (MT19937)",
        "python_version": sys.version.split()[0],
        "simulation_count": simulations,
        "random_seed": int(seed),
        "parameter_range": {
            "variable_daily_mean": round(variable_mean, 2),
            "variable_daily_std": round(variable_std, 2),
            "salary_cv": round(float(salary_cv), 4),
            "salary_miss_probability": round(float(salary_miss_probability), 4),
        },
        "reproducibility_scope": (
            "相同数据快照、参数、模拟次数、种子、Python/标准库版本可重复；"
            "跨 Python 版本或浮点平台不承诺逐位一致"
        ),
    }
    summary["calibration"]["variable_expense"] = (
        "按历史每日非固定支出的均值与标准差拟合 Gamma 分布；"
        "无足够样本时标准差为 0，并在数据质量中降低可信度"
    )
    summary["calibration"]["salary"] = (
        "工资事件按历史工资变异系数做截断正态扰动，缺发概率按可用月份覆盖率估计"
    )
    summary["calibration"]["variable_observation_days"] = int(variable_observation_days)
    summary["assumptions"] = [
        "合同型订阅、信用卡还款、定时任务和用户情景事件保持确定性。",
        "日常变动支出是独立同分布的 Gamma 抽样，未建模商户间相关。",
        "工资扰动仅施加于识别出的工资事件，不改变其他收入事件。",
    ]
    return summary
