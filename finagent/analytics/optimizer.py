"""Constrained long-only portfolio optimizer using transparent estimates."""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, Iterator, List, Tuple

from finagent.analytics.product_risk import (
    ASSET_CORRELATION,
    estimate_product_analytics,
)


DEFAULT_RETURN_BY_RISK = {
    1: 0.015,
    2: 0.030,
    3: 0.050,
    4: 0.080,
    5: 0.110,
}
MAX_SINGLE_PRODUCT_WEIGHT = 0.60
WEIGHT_STEP = 0.10
RISK_AVERSION = 0.30
CVAR_PENALTY = 0.05


def _correlation(left: str, right: str) -> float:
    if left == "cash" or right == "cash":
        return 0.0
    return ASSET_CORRELATION.get(tuple(sorted((left, right))), 0.30)


def _product_asset_class(product: Any) -> str:
    return str(getattr(product, "asset_class", "") or "fund")


def _expected_return(product: Any) -> float:
    value = float(getattr(product, "expected_return", 0.0) or 0.0)
    if value > 0:
        return value
    risk = max(1, min(int(getattr(product, "risk_level", 3) or 3), 5))
    return DEFAULT_RETURN_BY_RISK[risk]


def _liquidity_fits(product: Any, horizon_days: int) -> bool:
    term_days = int(getattr(product, "term_days", 0) or 0)
    if term_days > horizon_days:
        return False
    text = str(
        getattr(product, "redemption_rule", "")
        or getattr(product, "liquidity", "")
    )
    if "封闭" in text or "持有" in text or "锁定" in text:
        digits = ""
        for char in text:
            if char.isdigit():
                digits += char
            elif digits:
                break
        if digits:
            units = int(digits)
            if "年" in text:
                units *= 365
            elif "月" in text:
                units *= 30
            if units > horizon_days:
                return False
    if "T+2" in text.upper() and horizon_days < 2:
        return False
    if "T+1" in text.upper() and horizon_days < 1:
        return False
    return True


def _normal_cvar_loss(mu: float, sigma: float, probability: float = 0.05) -> float:
    if sigma <= 0:
        return max(0.0, -mu)
    # Lower-tail expected shortfall for a normal distribution.
    z = -1.6448536269514722
    density = math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
    return max(0.0, -mu + sigma * density / probability)


def _compositions(total_units: int, parts: int) -> Iterator[Tuple[int, ...]]:
    if parts == 1:
        yield (total_units,)
        return
    for first in range(total_units + 1):
        for remainder in _compositions(total_units - first, parts - 1):
            yield (first,) + remainder


def _portfolio_stats(
    weights: List[float],
    candidates: List[Dict[str, Any]],
    *,
    risk_aversion: float,
    cvar_penalty: float,
) -> Dict[str, float]:
    expected_return = sum(
        weight * item["expected_return"]
        for weight, item in zip(weights, candidates)
    )
    variance = 0.0
    for index, left in enumerate(candidates):
        for other_index, right in enumerate(candidates):
            corr = _correlation(left["asset_class"], right["asset_class"])
            variance += (
                weights[index]
                * weights[other_index]
                * left["volatility"]
                * right["volatility"]
                * corr
            )
    volatility = math.sqrt(max(variance, 0.0))
    weighted_drawdown = sum(
        weight * item["drawdown"]
        for weight, item in zip(weights, candidates)
    )
    concentration = sum(weight * weight for weight in weights)
    drawdown = weighted_drawdown * (0.65 + 0.35 * math.sqrt(max(concentration, 0.0)))
    transaction_cost = sum(
        weight * item["fee_rate"]
        for weight, item in zip(weights, candidates)
    )
    cvar = _normal_cvar_loss(expected_return, volatility)
    objective = (
        expected_return
        - risk_aversion * volatility
        - cvar_penalty * cvar
        - transaction_cost
    )
    return {
        "expected_return": round(expected_return, 6),
        "volatility": round(volatility, 6),
        "cvar_5pct": round(cvar, 6),
        "estimated_drawdown": round(drawdown, 6),
        "transaction_cost": round(transaction_cost, 6),
        "objective_score": round(objective, 8),
    }


def _cash_result(
    *,
    status: str,
    reason: str,
    liquid_assets: float,
    requested_investment: float,
    cash_buffer: float,
    constraints: Dict[str, Any],
) -> Dict[str, Any]:
    cash_amount = max(0.0, min(requested_investment, liquid_assets - cash_buffer))
    return {
        "status": status,
        "reason": reason,
        "liquid_assets": round(liquid_assets, 2),
        "requested_investment": round(requested_investment, 2),
        "cash_buffer": round(cash_buffer, 2),
        "available_budget": round(max(0.0, liquid_assets - cash_buffer), 2),
        "spendable_budget": round(cash_amount, 2),
        "weights": {"cash": 1.0},
        "allocations": [
            {
                "product_id": "cash",
                "name": "现金缓冲",
                "asset_class": "cash",
                "risk_level": 1,
                "weight": 1.0,
                "amount": round(cash_amount, 2),
                "expected_return": 0.015,
                "volatility": 0.0,
                "fee_rate": 0.0,
            }
        ],
        "expected_return": 0.015 if cash_amount else 0.0,
        "volatility": 0.0,
        "cvar_5pct": 0.0,
        "estimated_drawdown": 0.0,
        "transaction_cost": 0.0,
        "objective_score": 0.015 if cash_amount else 0.0,
        "constraints": constraints,
        "rejected_products": [],
        "assumptions": [
            "Cash is the safe fallback when hard constraints cannot be satisfied.",
            "No hard constraint is relaxed automatically.",
        ],
    }


def optimize_portfolio(
    products: Iterable[Any],
    *,
    liquid_assets: float,
    requested_investment: float,
    client_risk: int,
    horizon_days: int,
    cash_buffer: float,
    drawdown_limit: float,
    probability_forecast: Dict[str, Any] | None = None,
    seed: int = 0,
    risk_aversion: float = RISK_AVERSION,
    cvar_penalty: float = CVAR_PENALTY,
) -> Dict[str, Any]:
    """Optimize weights under hard suitability and liquidity constraints."""
    liquid_assets = max(0.0, float(liquid_assets or 0.0))
    requested_investment = max(0.0, float(requested_investment or 0.0))
    cash_buffer = max(0.0, float(cash_buffer or 0.0))
    client_risk = max(1, min(int(client_risk or 1), 5))
    horizon_days = max(0, int(horizon_days or 0))
    drawdown_limit = max(0.0, float(drawdown_limit or 0.0))
    constraints_base = {
        "cash_buffer_satisfied": liquid_assets + 1e-9 >= cash_buffer,
        "product_risk_ok": True,
        "term_and_liquidity_ok": True,
        "max_drawdown_ok": True,
        "concentration_ok": True,
        "budget_ok": requested_investment > 0,
    }
    if not constraints_base["cash_buffer_satisfied"]:
        return _cash_result(
            status="no_feasible_portfolio",
            reason="required cash buffer exceeds liquid assets",
            liquid_assets=liquid_assets,
            requested_investment=requested_investment,
            cash_buffer=cash_buffer,
            constraints=constraints_base,
        )

    available_budget = max(0.0, liquid_assets - cash_buffer)
    spendable_budget = min(requested_investment, available_budget)
    if spendable_budget <= 0:
        constraints_base["budget_ok"] = False
        return _cash_result(
            status="cash_only",
            reason="no spendable budget after preserving the cash buffer",
            liquid_assets=liquid_assets,
            requested_investment=requested_investment,
            cash_buffer=cash_buffer,
            constraints=constraints_base,
        )

    candidates: List[Dict[str, Any]] = []
    rejected: List[Dict[str, str]] = []
    for product in products:
        risk = int(getattr(product, "risk_level", 5) or 5)
        if risk > client_risk:
            rejected.append({
                "product_id": getattr(product, "product_id", ""),
                "reason": f"risk R{risk} exceeds client cap R{client_risk}",
            })
            continue
        if not _liquidity_fits(product, horizon_days):
            rejected.append({
                "product_id": getattr(product, "product_id", ""),
                "reason": "term or liquidity exceeds the investment horizon",
            })
            continue
        analytics = estimate_product_analytics(product)
        candidates.append({
            "product": product,
            "product_id": str(getattr(product, "product_id", "")),
            "name": str(getattr(product, "name", "")),
            "risk_level": risk,
            "asset_class": _product_asset_class(product),
            "expected_return": _expected_return(product),
            "volatility": float(
                analytics["estimated_annual_volatility_pct"]
            ) / 100.0,
            "drawdown": float(
                analytics["estimated_max_drawdown_pct"]
            ) / 100.0,
            "fee_rate": max(float(getattr(product, "fee_rate", 0.0) or 0.0), 0.0),
            "min_amount": max(float(getattr(product, "min_amount", 0.0) or 0.0), 0.0),
        })
    candidates = [
        item
        for item in candidates
        if item["min_amount"] <= spendable_budget
    ]
    candidates.sort(
        key=lambda item: (
            -(item["expected_return"] - 0.5 * item["volatility"]),
            item["risk_level"],
            item["product_id"],
        )
    )
    candidates = candidates[:5]
    if not candidates:
        constraints_base["term_and_liquidity_ok"] = False
        return _cash_result(
            status="cash_only",
            reason="no product satisfied risk, liquidity, and minimum-amount constraints",
            liquid_assets=liquid_assets,
            requested_investment=requested_investment,
            cash_buffer=cash_buffer,
            constraints=constraints_base,
        )

    universe = candidates
    units = int(round(1.0 / WEIGHT_STEP))
    best: Dict[str, Any] | None = None
    for composition in _compositions(units, len(universe)):
        weights = [value / units for value in composition]
        if any(weight > MAX_SINGLE_PRODUCT_WEIGHT + 1e-9 for weight in weights):
            continue
        amounts = [
            weight * spendable_budget
            for weight in weights
        ]
        if any(
            weight > 0 and amount + 1e-9 < item["min_amount"]
            for weight, amount, item in zip(weights, amounts, universe)
        ):
            continue
        stats = _portfolio_stats(
            weights,
            universe,
            risk_aversion=risk_aversion,
            cvar_penalty=cvar_penalty,
        )
        if stats["estimated_drawdown"] > drawdown_limit + 1e-9:
            continue
        if best is None or stats["objective_score"] > best["stats"]["objective_score"]:
            best = {
                "weights": weights,
                "amounts": amounts,
                "stats": stats,
            }

    if best is None:
        constraints_base["max_drawdown_ok"] = False
        return _cash_result(
            status="cash_only",
            reason="no product combination met the drawdown constraint",
            liquid_assets=liquid_assets,
            requested_investment=requested_investment,
            cash_buffer=cash_buffer,
            constraints=constraints_base,
        )

    weights = best["weights"]
    amounts = best["amounts"]
    stats = best["stats"]
    allocations = []
    for weight, amount, item in zip(weights, amounts, universe):
        if weight <= 0:
            continue
        allocations.append({
            "product_id": item["product_id"],
            "name": item["name"],
            "asset_class": item["asset_class"],
            "risk_level": item["risk_level"],
            "weight": round(weight, 4),
            "amount": round(amount, 2),
            "expected_return": round(item["expected_return"], 6),
            "volatility": round(item["volatility"], 6),
            "fee_rate": round(item["fee_rate"], 6),
        })
    allocations.sort(key=lambda item: (-item["weight"], item["product_id"]))
    product_allocations = list(allocations)
    constraints = {
        "cash_buffer_satisfied": True,
        "product_risk_ok": all(
            item["risk_level"] <= client_risk for item in product_allocations
        ),
        "term_and_liquidity_ok": True,
        "max_drawdown_ok": stats["estimated_drawdown"] <= drawdown_limit + 1e-9,
        "concentration_ok": all(
            item["weight"] <= MAX_SINGLE_PRODUCT_WEIGHT + 1e-9
            for item in product_allocations
        ),
        "budget_ok": sum(item["amount"] for item in allocations) <= spendable_budget + 0.01,
    }
    probability_summary = {}
    if probability_forecast:
        probability_summary = {
            "p05_min_balance": probability_forecast["min_balance_quantiles"]["p05"],
            "negative_probability": probability_forecast["negative_probability"],
            "terminal_interval_90": probability_forecast["intervals"]["terminal_90"],
        }
    return {
        "status": "optimized" if product_allocations else "cash_only",
        "reason": "risk-adjusted objective maximized under hard constraints",
        "liquid_assets": round(liquid_assets, 2),
        "requested_investment": round(requested_investment, 2),
        "cash_buffer": round(cash_buffer, 2),
        "available_budget": round(available_budget, 2),
        "spendable_budget": round(spendable_budget, 2),
        "weights": {
            item["product_id"]: round(weight, 4)
            for item, weight in zip(universe, weights)
            if weight > 0
        },
        "allocations": allocations,
        **stats,
        "constraints": constraints,
        "rejected_products": rejected,
        "probability_summary": probability_summary,
        "random_seed": seed,
        "objective_parameters": {
            "risk_aversion": risk_aversion,
            "cvar_penalty": cvar_penalty,
        },
        "assumptions": [
            "Expected returns use product scenario parameters or the risk-band fallback.",
            "Volatility and drawdown come from the transparent product risk model.",
            "CVaR is a normal-tail estimate and the drawdown cap is a scenario proxy.",
            "The optimizer uses 10% weight steps and never relaxes hard constraints.",
        ],
    }
