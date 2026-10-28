"""Transparent risk analytics for investment products.

The values are model estimates based on product risk level and asset class,
not historical realized performance. They are labelled as such by callers.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List


RISK_VOLATILITY = {1: 0.01, 2: 0.04, 3: 0.09, 4: 0.18, 5: 0.30}
RISK_DRAWDOWN = {1: 0.005, 2: 0.03, 3: 0.10, 4: 0.22, 5: 0.38}
ASSET_CORRELATION = {
    ("deposit", "deposit"): 0.05,
    ("deposit", "bond"): 0.15,
    ("deposit", "fund"): 0.05,
    ("deposit", "stock"): 0.02,
    ("bond", "bond"): 0.65,
    ("bond", "fund"): 0.45,
    ("bond", "stock"): 0.15,
    ("fund", "fund"): 0.72,
    ("fund", "stock"): 0.76,
    ("stock", "stock"): 0.82,
}


def _asset_class(product: Any) -> str:
    value = str(getattr(product, "asset_class", "") or "").lower()
    if value:
        return value
    category = str(getattr(product, "category", ""))
    if "股" in category or "指数" in category:
        return "stock"
    if "债" in category:
        return "bond"
    if "存单" in category:
        return "deposit"
    return "fund"


def estimate_product_analytics(product: Any) -> Dict[str, Any]:
    level = max(1, min(int(getattr(product, "risk_level", 3) or 3), 5))
    asset_class = _asset_class(product)
    volatility = RISK_VOLATILITY[level]
    drawdown = RISK_DRAWDOWN[level]
    fee_rate = max(float(getattr(product, "fee_rate", 0.0) or 0.0), 0.0)
    liquidity = str(
        getattr(product, "redemption_rule", "")
        or getattr(product, "liquidity", "")
        or "以产品规则为准"
    )
    return {
        "asset_class": asset_class,
        "estimated_annual_volatility_pct": round(volatility * 100, 2),
        "estimated_max_drawdown_pct": round(drawdown * 100, 2),
        "stress_loss_pct": {
            "mild": round(volatility * 100, 2),
            "moderate": round(volatility * 2 * 100, 2),
            "severe": round(drawdown * 100, 2),
        },
        "fee_drag_1y_pct": round(fee_rate * 100, 2),
        "liquidity": liquidity,
        "risk_method": "风险等级 + 资产类别的透明情景估计",
        "is_estimate": True,
    }


def _correlation(left: str, right: str) -> float:
    key = tuple(sorted((left, right)))
    return ASSET_CORRELATION.get(key, 0.3)


def portfolio_risk_summary(products: Iterable[Any]) -> Dict[str, Any]:
    items = list(products)
    analytics = [estimate_product_analytics(product) for product in items]
    if not analytics:
        return {
            "product_count": 0,
            "average_volatility_pct": 0.0,
            "worst_drawdown_pct": 0.0,
            "diversification": "无产品",
            "correlation_matrix": [],
        }
    classes = [item["asset_class"] for item in analytics]
    average_volatility = sum(
        float(item["estimated_annual_volatility_pct"]) for item in analytics
    ) / len(analytics)
    worst_drawdown = max(
        float(item["estimated_max_drawdown_pct"]) for item in analytics
    )
    unique_classes = sorted(set(classes))
    matrix: List[List[Any]] = [[""] + unique_classes]
    for left in unique_classes:
        matrix.append([left] + [
            round(_correlation(left, right), 2) for right in unique_classes
        ])
    if len(unique_classes) >= 3:
        diversification = "较高"
    elif len(unique_classes) == 2:
        diversification = "中等"
    else:
        diversification = "较低"
    return {
        "product_count": len(analytics),
        "average_volatility_pct": round(average_volatility, 2),
        "worst_drawdown_pct": round(worst_drawdown, 2),
        "diversification": diversification,
        "asset_classes": unique_classes,
        "correlation_matrix": matrix,
        "analytics": analytics,
        "is_estimate": True,
    }
