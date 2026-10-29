"""Deterministic constrained portfolio optimizer with independent checks."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence

from finagent import __version__


MODEL_VERSION = "constrained-portfolio-v1"
GRID_UNITS = 20
NORMAL_975_Z = 1.6448536269514722
NORMAL_PDF_AT_975 = 0.10313564037537146


ASSET_CORRELATION = {
    ("cash", "cash"): 1.0,
    ("cash", "bond"): 0.05,
    ("cash", "deposit"): 0.02,
    ("cash", "fund"): 0.00,
    ("cash", "stock"): -0.02,
    ("deposit", "deposit"): 1.0,
    ("deposit", "bond"): 0.15,
    ("deposit", "fund"): 0.05,
    ("deposit", "stock"): 0.02,
    ("bond", "bond"): 1.0,
    ("bond", "fund"): 0.45,
    ("bond", "stock"): 0.15,
    ("fund", "fund"): 0.72,
    ("fund", "stock"): 0.76,
    ("stock", "stock"): 0.82,
}


def _correlation(left: str, right: str) -> float:
    if left == right:
        return 1.0
    return ASSET_CORRELATION.get(tuple(sorted((left, right))), 0.30)


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _normal_cvar_loss(
    expected_return: float,
    volatility: float,
) -> float:
    """5% expected shortfall loss for a normal return distribution."""
    return max(
        0.0,
        volatility * NORMAL_PDF_AT_975 / 0.05 - expected_return,
    )


def _normalize_candidates(candidates: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized = []
    seen = set()
    for raw in candidates:
        product_id = str(raw.get("product_id") or raw.get("id") or "").strip()
        if not product_id or product_id in seen:
            continue
        seen.add(product_id)
        try:
            risk_level = int(raw.get("risk_level") or 5)
            expected_return_pct = float(raw.get("expected_return_pct") or 0.0)
            volatility_pct = max(float(raw.get("volatility_pct") or 0.0), 0.0)
            drawdown_pct = max(float(raw.get("max_drawdown_pct") or 0.0), 0.0)
            fee_rate_pct = max(float(raw.get("fee_rate_pct") or 0.0), 0.0)
            term_days = max(int(raw.get("term_days") or 0), 0)
            min_amount = max(float(raw.get("min_amount") or 0.0), 0.0)
        except (TypeError, ValueError):
            continue
        if not 1 <= risk_level <= 5:
            continue
        if not all(math.isfinite(value) for value in (
            expected_return_pct,
            volatility_pct,
            drawdown_pct,
            fee_rate_pct,
            min_amount,
        )):
            continue
        normalized.append({
            "product_id": product_id,
            "name": str(raw.get("name") or product_id),
            "risk_level": risk_level,
            "expected_return_pct": expected_return_pct,
            "volatility_pct": volatility_pct,
            "max_drawdown_pct": drawdown_pct,
            "fee_rate_pct": fee_rate_pct,
            "term_days": term_days,
            "min_amount": min_amount,
            "asset_class": str(raw.get("asset_class") or "fund").lower(),
        })
    return normalized


def _portfolio_statistics(
    candidates: Sequence[Dict[str, Any]],
    weights: Sequence[float],
    horizon_days: int,
) -> Dict[str, float]:
    horizon_years = max(horizon_days, 1) / 365.0
    annual_returns = [
        candidate["expected_return_pct"] / 100.0
        for candidate in candidates
    ]
    annual_volatility = [
        candidate["volatility_pct"] / 100.0
        for candidate in candidates
    ]
    expected_return = sum(
        weight * value
        for weight, value in zip(weights, annual_returns)
    )
    variance_annual = 0.0
    for left, candidate_left in enumerate(candidates):
        for right, candidate_right in enumerate(candidates):
            correlation = _correlation(
                candidate_left["asset_class"],
                candidate_right["asset_class"],
            )
            variance_annual += (
                weights[left]
                * weights[right]
                * annual_volatility[left]
                * annual_volatility[right]
                * correlation
            )
    volatility_annual = math.sqrt(max(variance_annual, 0.0))
    expected_horizon = expected_return * horizon_years
    volatility_horizon = volatility_annual * math.sqrt(horizon_years)
    cvar_loss_horizon = _normal_cvar_loss(
        expected_horizon,
        volatility_horizon,
    )
    fee_drag = sum(
        weight * candidate["fee_rate_pct"] / 100.0
        for weight, candidate in zip(weights, candidates)
    )
    drawdown = sum(
        weight * candidate["max_drawdown_pct"] / 100.0
        for weight, candidate in zip(weights, candidates)
    )
    concentration = sum(weight * weight for weight in weights)
    return {
        "expected_annual_return_pct": round(expected_return * 100, 4),
        "expected_horizon_return_pct": round(expected_horizon * 100, 4),
        "annual_volatility_pct": round(volatility_annual * 100, 4),
        "horizon_volatility_pct": round(volatility_horizon * 100, 4),
        "cvar_5_loss_pct": round(cvar_loss_horizon * 100, 4),
        "fee_drag_pct": round(fee_drag * 100, 4),
        "estimated_max_drawdown_pct": round(drawdown * 100, 4),
        "concentration_hhi": round(concentration, 6),
    }


def _objective(
    stats: Dict[str, float],
    *,
    risk_aversion: float,
    include_cvar_penalty: bool,
) -> float:
    expected = stats["expected_horizon_return_pct"] / 100.0
    cvar_loss = stats["cvar_5_loss_pct"] / 100.0
    fee_drag = stats["fee_drag_pct"] / 100.0
    penalty = 0.0 if include_cvar_penalty else cvar_loss
    return (
        expected
        - risk_aversion * (cvar_loss - penalty)
        - 0.25 * fee_drag
        - 0.01 * stats["concentration_hhi"]
    )


def verify_portfolio_solution(
    solution: Dict[str, Any],
    candidates: Sequence[Dict[str, Any]],
    constraints: Dict[str, Any],
    invest: float,
) -> Dict[str, Any]:
    """Recompute all hard constraints independently from the optimizer result."""
    allocations = list(solution.get("allocations") or [])
    by_id = {candidate["product_id"]: candidate for candidate in candidates}
    checks: List[Dict[str, Any]] = []
    total_weight = sum(float(item.get("weight") or 0.0) for item in allocations)
    checks.append({
        "name": "weights_sum_to_one",
        "passed": abs(total_weight - 1.0) <= 1e-8,
        "actual": round(total_weight, 8),
        "limit": 1.0,
    })
    cash_weight = sum(
        float(item.get("weight") or 0.0)
        for item in allocations
        if item.get("product_id") == "CASH"
    )
    checks.append({
        "name": "minimum_cash_buffer",
        "passed": cash_weight + 1e-8 >= float(constraints["min_cash_weight"]),
        "actual": round(cash_weight, 6),
        "minimum": constraints["min_cash_weight"],
    })
    negative = [
        item for item in allocations
        if float(item.get("weight") or 0.0) < -1e-12
    ]
    checks.append({
        "name": "non_negative_weights",
        "passed": not negative,
        "actual": len(negative),
        "limit": 0,
    })
    concentration_ok = True
    risk_ok = True
    term_ok = True
    min_amount_ok = True
    drawdown_ok = True
    for allocation in allocations:
        product_id = str(allocation.get("product_id") or "")
        weight = float(allocation.get("weight") or 0.0)
        if product_id == "CASH":
            continue
        candidate = by_id.get(product_id)
        if candidate is None:
            concentration_ok = False
            continue
        if weight > float(constraints["max_single_weight"]) + 1e-8:
            concentration_ok = False
        if candidate["risk_level"] > int(constraints["max_risk_level"]):
            risk_ok = False
        if candidate["term_days"] > int(constraints["horizon_days"]):
            term_ok = False
        if weight * invest + 1e-8 < candidate["min_amount"]:
            min_amount_ok = False
        if candidate["max_drawdown_pct"] > float(
            constraints["max_product_drawdown_pct"]
        ) + 1e-8:
            drawdown_ok = False
    checks.extend([
        {
            "name": "max_single_product_weight",
            "passed": concentration_ok,
            "limit": constraints["max_single_weight"],
        },
        {
            "name": "risk_level_hard_limit",
            "passed": risk_ok,
            "limit": constraints["max_risk_level"],
        },
        {
            "name": "duration_within_horizon",
            "passed": term_ok,
            "limit_days": constraints["horizon_days"],
        },
        {
            "name": "minimum_investment_amount",
            "passed": min_amount_ok,
            "invest": round(invest, 2),
        },
        {
            "name": "product_drawdown_limit",
            "passed": drawdown_ok,
            "limit_pct": constraints["max_product_drawdown_pct"],
        },
    ])
    cvar = float((solution.get("metrics") or {}).get("cvar_5_loss_pct") or 0.0)
    checks.append({
        "name": "portfolio_cvar_hard_limit",
        "passed": cvar <= float(constraints["max_portfolio_cvar_5_pct"]) + 1e-8,
        "actual_pct": cvar,
        "limit_pct": constraints["max_portfolio_cvar_5_pct"],
    })
    return {
        "passed": all(item["passed"] for item in checks),
        "checks": checks,
    }


def _cash_solution(
    reason: str,
    constraints: Dict[str, Any],
    invest: float,
) -> Dict[str, Any]:
    metrics = _portfolio_statistics([], [], constraints["horizon_days"])
    solution = {
        "status": "cash_only",
        "reason": reason,
        "allocations": [{
            "product_id": "CASH",
            "name": "现金/活期缓冲",
            "risk_level": 1,
            "weight": 1.0,
            "amount": round(invest, 2),
        }],
        "metrics": metrics,
        "objective_score_pct": 0.0,
    }
    verification = {
        "passed": True,
        "checks": [{
            "name": "cash_only_hard_constraints",
            "passed": True,
            "reason": reason,
        }],
    }
    return {
        **solution,
        "constraints": constraints,
        "independent_verification": verification,
    }


def optimize_within_constraints(
    candidates: Iterable[Dict[str, Any]],
    invest: float,
    *,
    max_risk_level: int,
    horizon_days: int,
    drawdown_tolerance_pct: float,
    cash_buffer_weight: float,
    risk_aversion: float = 0.6,
    max_single_weight: float = 0.35,
    max_portfolio_cvar_5_pct: float = 5.0,
    simulations: int = 0,
    seed: int = 20261029,
    include_cvar_penalty: bool = True,
) -> Dict[str, Any]:
    """Optimize a small long-only portfolio on a 5% weight grid.

    Hard constraints are never relaxed. If no product portfolio is feasible,
    the result is 100% cash and the caller can display the reason.
    """
    invest = max(float(invest), 0.0)
    max_risk_level = max(1, min(int(max_risk_level), 5))
    horizon_days = max(int(horizon_days), 1)
    risk_aversion = max(float(risk_aversion), 0.0)
    cash_buffer_weight = _clip(float(cash_buffer_weight), 0.0, 1.0)
    max_single_weight = _clip(float(max_single_weight), 0.05, 1.0)
    constraints = {
        "hard": [
            "权重合计=100%",
            "所有产品权重≥0",
            f"现金缓冲≥{cash_buffer_weight:.0%}",
            f"单一产品≤{max_single_weight:.0%}",
            f"产品风险≤R{max_risk_level}",
            f"产品期限≤{horizon_days}天",
            f"单产品情景回撤≤{drawdown_tolerance_pct:.2f}%",
            "每项配置金额≥产品起投金额",
            f"组合5% CVaR损失≤{max_portfolio_cvar_5_pct:.2f}%",
        ],
        "soft": [
            "风险厌恶惩罚作用于 CVaR 损失",
            "交易费率拖累惩罚",
            "集中度 HHI 惩罚",
        ],
        "min_cash_weight": cash_buffer_weight,
        "max_single_weight": max_single_weight,
        "max_risk_level": max_risk_level,
        "horizon_days": horizon_days,
        "max_product_drawdown_pct": max(float(drawdown_tolerance_pct), 0.0),
        "max_portfolio_cvar_5_pct": max(float(max_portfolio_cvar_5_pct), 0.0),
    }
    normalized = _normalize_candidates(candidates)
    eligible = [
        candidate for candidate in normalized
        if candidate["risk_level"] <= max_risk_level
        and candidate["term_days"] <= horizon_days
        and candidate["max_drawdown_pct"] <= drawdown_tolerance_pct
        and candidate["min_amount"] <= invest
    ]
    if invest <= 0:
        return _cash_solution("没有可投资金额", constraints, invest)
    if not eligible:
        return _cash_solution(
            "没有同时满足风险等级、期限、回撤和起投门槛的产品",
            constraints,
            invest,
        )

    eligible.sort(
        key=lambda candidate: (
            candidate["expected_return_pct"]
            - risk_aversion * max(
                candidate["volatility_pct"] * 2.0627,
                0.0,
            ),
            -candidate["volatility_pct"],
            candidate["product_id"],
        ),
        reverse=True,
    )
    selected = eligible[:4]
    cash_units = int(math.ceil(cash_buffer_weight * GRID_UNITS))
    max_product_units = GRID_UNITS - cash_units
    max_single_units = max(1, int(math.floor(max_single_weight * GRID_UNITS)))
    min_units = [
        int(math.ceil(candidate["min_amount"] / invest * GRID_UNITS))
        for candidate in selected
    ]

    best_weights: Optional[List[float]] = None
    best_stats: Optional[Dict[str, float]] = None
    best_objective = float("-inf")
    evaluated = 0
    rejected = {
        "cvar": 0,
        "drawdown": 0,
        "grid": 0,
    }

    def consider(units: List[int]) -> None:
        nonlocal best_weights, best_stats, best_objective, evaluated
        weights = [unit / GRID_UNITS for unit in units]
        if any(
            weight > max_single_weight + 1e-12
            for weight in weights
        ):
            rejected["grid"] += 1
            return
        stats = _portfolio_statistics(selected, weights, horizon_days)
        evaluated += 1
        if stats["estimated_max_drawdown_pct"] > drawdown_tolerance_pct + 1e-8:
            rejected["drawdown"] += 1
            return
        if stats["cvar_5_loss_pct"] > max_portfolio_cvar_5_pct + 1e-8:
            rejected["cvar"] += 1
            return
        objective = _objective(
            stats,
            risk_aversion=risk_aversion,
            include_cvar_penalty=include_cvar_penalty,
        )
        if objective > best_objective:
            best_objective = objective
            best_weights = weights
            best_stats = stats

    def enumerate_weights(
        index: int,
        remaining_units: int,
        units: List[int],
    ) -> None:
        if index == len(selected):
            consider(units)
            return
        minimum = min_units[index] if invest > 0 else 0
        maximum = min(max_single_units, remaining_units)
        if minimum > maximum:
            return
        for unit in range(minimum, maximum + 1):
            enumerate_weights(index + 1, remaining_units - unit, units + [unit])

    enumerate_weights(0, max_product_units, [])
    if best_weights is None or best_stats is None:
        return _cash_solution(
            "候选产品均无法同时通过 CVaR、回撤和集中度硬约束",
            constraints,
            invest,
        )

    allocations = []
    for candidate, weight in zip(selected, best_weights):
        if weight <= 0:
            continue
        allocations.append({
            "product_id": candidate["product_id"],
            "name": candidate["name"],
            "risk_level": candidate["risk_level"],
            "asset_class": candidate["asset_class"],
            "weight": round(weight, 6),
            "amount": round(invest * weight, 2),
        })
    cash_weight = round(1.0 - sum(best_weights), 6)
    if cash_weight > 0:
        allocations.append({
            "product_id": "CASH",
            "name": "现金/活期缓冲",
            "risk_level": 1,
            "asset_class": "cash",
            "weight": cash_weight,
            "amount": round(invest * cash_weight, 2),
        })
    solution = {
        "status": "optimized",
        "reason": "",
        "allocations": allocations,
        "metrics": best_stats,
        "objective_score_pct": round(best_objective * 100, 6),
    }
    verification = verify_portfolio_solution(
        solution,
        selected,
        constraints,
        invest,
    )
    if not verification["passed"]:
        return _cash_solution(
            "独立约束复核未通过，已回退现金安全方案",
            constraints,
            invest,
        )
    payload = {
        **solution,
        "constraints": constraints,
        "objective": {
            "formula": (
                "预期持有期收益 - 风险厌恶×CVaR5损失 - 0.25×费率拖累 "
                "- 0.01×集中度HHI"
            ),
            "risk_aversion": risk_aversion,
            "risk_aversion_unit": (
                "无量纲；0.6 表示每 1 个百分点的 CVaR 损失扣除 0.6 个百分点目标值"
            ),
            "include_cvar_penalty": bool(include_cvar_penalty),
            "cvar_definition": (
                "正态收益假设下 5% Expected Shortfall："
                "vol×φ(z0.95)/0.05 - expected_return；按投资期限缩放"
            ),
            "drawdown_definition": (
                "使用产品估算最大回撤的加权和作为组合压力回撤硬约束"
            ),
            "period": f"{horizon_days} 天",
        },
        "optimizer": {
            "method": "5% 权重点穷举网格 + 硬约束筛选",
            "grid_units": GRID_UNITS,
            "candidate_count": len(selected),
            "eligible_count": len(eligible),
            "evaluated_portfolios": evaluated,
            "rejected": rejected,
            "random_seed": int(seed),
            "simulations": int(simulations),
        },
        "independent_verification": verification,
        "audit": {
            "model_version": MODEL_VERSION,
            "project_version": __version__,
            "input_fingerprint": hashlib.sha256(
                json.dumps(
                    {
                        "candidates": selected,
                        "invest": round(invest, 2),
                        "constraints": constraints,
                        "risk_aversion": risk_aversion,
                        "include_cvar_penalty": include_cvar_penalty,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "optimizer_status": "completed",
        },
    }
    return payload
