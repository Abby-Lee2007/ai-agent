"""Deterministic financial analytics used by Agent tools."""

from finagent.analytics.cashflow import build_cashflow_forecast
from finagent.analytics.financial_health import (
    build_event_center,
    build_financial_health,
)
from finagent.analytics.product_risk import (
    estimate_product_analytics,
    portfolio_risk_summary,
)

__all__ = [
    "build_cashflow_forecast",
    "build_financial_health",
    "build_event_center",
    "estimate_product_analytics",
    "portfolio_risk_summary",
]
