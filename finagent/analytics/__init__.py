"""Deterministic financial analytics used by Agent tools."""

from finagent.analytics.cashflow import build_cashflow_forecast
from finagent.analytics.data_quality import build_data_quality
from finagent.analytics.financial_health import (
    build_event_center,
    build_financial_health,
)
from finagent.analytics.product_risk import (
    estimate_product_analytics,
    portfolio_risk_summary,
)
from finagent.analytics.portfolio_optimizer import optimize_within_constraints
from finagent.analytics.probabilistic_cashflow import (
    simulate_cashflow_paths,
    summarize_probability_paths,
)

__all__ = [
    "build_cashflow_forecast",
    "build_financial_health",
    "build_event_center",
    "estimate_product_analytics",
    "portfolio_risk_summary",
    "build_data_quality",
    "simulate_cashflow_paths",
    "summarize_probability_paths",
    "optimize_within_constraints",
]
