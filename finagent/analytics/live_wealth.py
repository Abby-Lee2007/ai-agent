"""Quantitative analysis for public-market investment recommendations."""
from __future__ import annotations

import math
import statistics
from typing import Any, Dict, Iterable, List, Optional


TRADING_DAYS = 252


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _window_return(closes: List[float], days: int) -> Optional[float]:
    if len(closes) <= days or closes[-days - 1] <= 0:
        return None
    return (closes[-1] / closes[-days - 1] - 1) * 100


def calculate_live_metrics(
    history: Dict[str, Any],
    *,
    risk_free_rate_pct: float = 0.0,
) -> Dict[str, Any]:
    """Calculate returns, volatility, drawdown and trend from public history."""
    points = [
        item for item in history.get("points") or []
        if isinstance(item, dict)
        and isinstance(item.get("close"), (int, float))
        and float(item["close"]) > 0
    ]
    if len(points) < 20:
        return {
            "available": False,
            "reason": "公开历史序列少于 20 个交易日",
            "observations": len(points),
        }
    closes = [
        float(item.get("adjusted_close") or item["close"])
        for item in points
    ]
    daily_returns = [
        closes[index] / closes[index - 1] - 1
        for index in range(1, len(closes))
        if closes[index - 1] > 0
    ]
    annualized_volatility = (
        statistics.stdev(daily_returns) * math.sqrt(TRADING_DAYS)
        if len(daily_returns) > 1 else 0.0
    )
    annualized_return = (
        (closes[-1] / closes[0]) ** (TRADING_DAYS / max(len(closes) - 1, 1)) - 1
    )
    risk_free = max(float(risk_free_rate_pct or 0.0), 0.0) / 100
    sharpe = (
        (annualized_return - risk_free) / annualized_volatility
        if annualized_volatility > 0 else 0.0
    )
    peak = closes[0]
    max_drawdown = 0.0
    for close in closes:
        peak = max(peak, close)
        drawdown = close / peak - 1
        max_drawdown = min(max_drawdown, drawdown)
    moving_average_20 = statistics.fmean(closes[-20:])
    moving_average_60 = (
        statistics.fmean(closes[-60:]) if len(closes) >= 60 else moving_average_20
    )
    return_1m = _window_return(closes, 21)
    return_3m = _window_return(closes, 63)
    return_6m = _window_return(closes, 126)
    return_1y = _window_return(closes, 250)
    return {
        "available": True,
        "observations": len(points),
        "start_date": str(points[0].get("date") or ""),
        "end_date": str(points[-1].get("date") or ""),
        "last_price": round(closes[-1], 4),
        "annualized_return_pct": round(annualized_return * 100, 2),
        "return_1m_pct": round(return_1m, 2) if return_1m is not None else None,
        "return_3m_pct": round(return_3m, 2) if return_3m is not None else None,
        "return_6m_pct": round(return_6m, 2) if return_6m is not None else None,
        "return_1y_pct": round(return_1y, 2) if return_1y is not None else None,
        "annualized_volatility_pct": round(annualized_volatility * 100, 2),
        "max_drawdown_pct": round(abs(max_drawdown) * 100, 2),
        "sharpe_ratio": round(sharpe, 2),
        "ma20": round(moving_average_20, 4),
        "ma60": round(moving_average_60, 4),
        "above_ma20": closes[-1] >= moving_average_20,
        "ma20_above_ma60": moving_average_20 >= moving_average_60,
        "source": str(history.get("source") or "公开行情"),
        "risk_free_rate_pct": round(float(risk_free_rate_pct or 0.0), 2),
        "method": "公开历史收盘价：年化收益、年化波动、最大回撤、Sharpe 与均线趋势",
    }


def score_live_product(
    product: Any,
    metrics: Dict[str, Any],
    *,
    user_risk_level: int,
    drawdown_tolerance_pct: float,
    horizon: str = "mid",
    quote: Any = None,
    already_held: bool = False,
) -> Dict[str, Any]:
    """Build an explainable suitability score from live quantitative metrics."""
    if not metrics.get("available"):
        return {
            "score": 0.0,
            "label": "数据不足",
            "reasons": [str(metrics.get("reason") or "历史数据不足")],
            "components": {},
        }
    risk = int(getattr(product, "risk_level", 3) or 3)
    if risk <= user_risk_level:
        suitability = 25.0
    elif risk == user_risk_level + 1:
        suitability = 15.0
    else:
        suitability = 0.0

    drawdown = float(metrics.get("max_drawdown_pct") or 0.0)
    tolerance = max(float(drawdown_tolerance_pct or 0.0), 1.0)
    drawdown_score = 20.0 if drawdown <= tolerance else 20.0 * _clamp(tolerance / drawdown)

    sharpe = float(metrics.get("sharpe_ratio") or 0.0)
    is_cash_fund = "货币基金" in str(getattr(product, "category", ""))
    reasons = []
    if is_cash_fund:
        # Listed money-market ETFs distribute income separately from price.
        # Price-only Sharpe/trend signals are therefore not meaningful.
        sharpe_score = 10.0
        trend_score = 2.5
        valuation_score = 0.0
        reasons.append("货币ETF价格不包含全部收益分配，收益指标按现金管理工具谨慎处理")
    else:
        sharpe_score = 20.0 * _clamp((sharpe + 0.5) / 2.5)
        trend_score = 0.0
        if metrics.get("above_ma20"):
            trend_score += 5.0
        if metrics.get("ma20_above_ma60"):
            trend_score += 5.0
        if float(metrics.get("return_3m_pct") or 0.0) > 0:
            trend_score += 5.0
        valuation_score = 2.5
    if (
        not is_cash_fund
        and quote is not None
        and getattr(product, "asset_class", "") == "stock"
    ):
        pe = getattr(quote, "pe", None)
        pb = getattr(quote, "pb", None)
        if isinstance(pe, (int, float)) and pe > 0:
            if pe <= 15:
                valuation_score += 2.0
                reasons.append(f"PE {pe:.1f}，估值处于相对克制区间")
            elif pe >= 60:
                valuation_score -= 2.0
                reasons.append(f"PE {pe:.1f}，估值偏高")
        if isinstance(pb, (int, float)) and pb > 0:
            if pb <= 1.5:
                valuation_score += 1.0
            elif pb >= 8:
                valuation_score -= 1.0
    valuation_score = max(0.0, min(5.0, valuation_score))

    liquidity = str(
        getattr(product, "redemption_rule", "")
        or getattr(product, "liquidity", "")
    )
    if "T+0" in liquidity or "随时" in liquidity:
        liquidity_score = 10.0
    elif "T+1" in liquidity:
        liquidity_score = 8.0
    elif "T+2" in liquidity or "T+3" in liquidity:
        liquidity_score = 5.0
    else:
        liquidity_score = 3.0

    observations = int(metrics.get("observations") or 0)
    data_quality_score = 5.0 if observations >= 120 else (3.0 if observations >= 60 else 1.0)

    score = (
        suitability + drawdown_score + sharpe_score
        + trend_score + valuation_score + liquidity_score + data_quality_score
    )
    if horizon == "short":
        if drawdown > 5:
            score -= min(18.0, drawdown)
            reasons.append(f"短期资金与 {drawdown:.2f}% 历史回撤不匹配")
        if float(metrics.get("annualized_volatility_pct") or 0.0) > 15:
            score -= 8.0
            reasons.append("短期波动较高")
    elif horizon == "long" and float(metrics.get("return_1y_pct") or 0.0) > 0:
        score += 3.0
    if already_held:
        score -= 6.0
        reasons.append("已持有该标的，降低重复集中度评分")
    score = round(max(0.0, min(100.0, score)), 1)
    if score >= 75:
        label = "优先匹配"
    elif score >= 60:
        label = "适合关注"
    elif score >= 45:
        label = "谨慎观察"
    else:
        label = "不建议当前配置"
    if not reasons:
        reasons.append(
            f"历史波动 {metrics['annualized_volatility_pct']:.2f}%，"
            f"最大回撤 {metrics['max_drawdown_pct']:.2f}%，"
            f"Sharpe {metrics['sharpe_ratio']:.2f}"
        )
    return {
        "score": score,
        "label": label,
        "reasons": reasons[:3],
        "components": {
            "风险适当性": round(suitability, 1),
            "回撤承受": round(drawdown_score, 1),
            "风险调整收益": round(sharpe_score, 1),
            "趋势": round(trend_score, 1),
            "估值": round(valuation_score, 1),
            "流动性": round(liquidity_score, 1),
            "数据质量": round(data_quality_score, 1),
        },
    }


def diversified_selection(
    ranked: Iterable[Dict[str, Any]],
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Choose high scores while avoiding one-category concentration."""
    items = list(ranked)
    selected: List[Dict[str, Any]] = []
    category_counts: Dict[str, int] = {}
    for item in items:
        category = str(item.get("category") or "其他")
        if category_counts.get(category, 0) >= 2 and len(items) > limit:
            continue
        selected.append(item)
        category_counts[category] = category_counts.get(category, 0) + 1
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        selected_ids = {item["product_id"] for item in selected}
        selected.extend(
            item for item in items
            if item["product_id"] not in selected_ids
        )
        selected = selected[:limit]
    return selected
