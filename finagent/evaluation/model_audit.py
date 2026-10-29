"""P2 fairness, stability, and ablation checks for decision outputs."""
from __future__ import annotations

import copy
import statistics
from pathlib import Path
from typing import Any, Dict, Iterable, List

from finagent.analytics.financial_health import build_financial_health
from finagent.analytics.optimizer import optimize_portfolio
from finagent.analytics.probability import (
    build_probability_forecast,
    probability_risk_budget,
)
from finagent.data.providers import build_data_providers
from finagent.data.store import Clock, DataStore
from finagent.risk.scorecard import assess_risk_profile


MODEL_AUDIT_VERSION = "1.0"

PERSONAS = [
    {
        "persona_id": "young_conservative",
        "age": 24,
        "age_band": "18-29",
        "income_band": "<15k",
        "income_multiplier": 0.70,
        "balance_multiplier": 0.75,
        "risk_profile": "保守型",
    },
    {
        "persona_id": "young_growth",
        "age": 27,
        "age_band": "18-29",
        "income_band": "15k-30k",
        "income_multiplier": 1.00,
        "balance_multiplier": 0.90,
        "risk_profile": "成长型",
    },
    {
        "persona_id": "mid_balanced",
        "age": 35,
        "age_band": "30-44",
        "income_band": ">30k",
        "income_multiplier": 1.35,
        "balance_multiplier": 1.15,
        "risk_profile": "平衡型",
    },
    {
        "persona_id": "mid_steady",
        "age": 42,
        "age_band": "30-44",
        "income_band": "<15k",
        "income_multiplier": 0.80,
        "balance_multiplier": 0.85,
        "risk_profile": "稳健型",
    },
    {
        "persona_id": "senior_conservative",
        "age": 52,
        "age_band": "45+",
        "income_band": "15k-30k",
        "income_multiplier": 1.00,
        "balance_multiplier": 1.05,
        "risk_profile": "保守型",
    },
    {
        "persona_id": "senior_steady",
        "age": 58,
        "age_band": "45+",
        "income_band": ">30k",
        "income_multiplier": 1.30,
        "balance_multiplier": 1.25,
        "risk_profile": "稳健型",
    },
]


def _persona_store(
    store: DataStore,
    persona: Dict[str, Any],
    user_id: str,
) -> DataStore:
    cloned = copy.deepcopy(store)
    user = cloned.get_user(user_id)
    if user is None:
        raise ValueError(f"unknown user: {user_id}")
    income_multiplier = float(persona.get("income_multiplier", 1.0))
    balance_multiplier = float(persona.get("balance_multiplier", 1.0))
    user.age = int(persona.get("age", user.age))
    user.monthly_income = round(user.monthly_income * income_multiplier, 2)
    user.risk_profile = str(persona.get("risk_profile", user.risk_profile))

    for account in cloned.accounts.values():
        if account.user_id == user_id:
            account.balance = round(account.balance * balance_multiplier, 2)
    for card in cloned.cards.values():
        if card.user_id != user_id:
            continue
        if card.kind in {"debit", "virtual"}:
            card.balance = round(card.balance * balance_multiplier, 2)
        elif card.kind == "credit":
            card.used = round(card.used * balance_multiplier, 2)
    for txn in cloned.transactions.values():
        if txn.user_id == user_id and txn.amount > 0:
            txn.amount = round(txn.amount * income_multiplier, 2)
    return cloned


def _liquid_assets(store: DataStore, providers: Any, user_id: str) -> float:
    return round(
        sum(
            max(card.balance, 0.0)
            for card in providers.cards.of(user_id)
            if card.kind in {"debit", "virtual"} and card.status == "active"
        ),
        2,
    )


def _is_equity(product: Any) -> bool:
    text = f"{getattr(product, 'asset_class', '')} {getattr(product, 'category', '')}"
    return (
        "stock" in text
        or "股票" in text
        or "指数" in text
        or "权益" in text
    )


def _is_risk_asset(product: Any) -> bool:
    return _is_equity(product) or int(getattr(product, "risk_level", 1) or 1) >= 3


def _metrics_for_store(
    store: DataStore,
    clock: Clock,
    user_id: str,
    *,
    simulations: int,
    seed: int,
) -> Dict[str, Any]:
    providers = build_data_providers(store)
    user = store.get_user(user_id)
    if user is None:
        raise ValueError(f"unknown user: {user_id}")
    health = build_financial_health(store, providers, clock, user_id)
    risk = assess_risk_profile(user, store, clock.today())
    probability = build_probability_forecast(
        store,
        providers,
        clock,
        user_id,
        months=3,
        simulations=simulations,
        seed=seed,
    )
    risk_budget = probability_risk_budget(probability)
    liquid = _liquid_assets(store, providers, user_id)
    essential = float(probability["baseline"]["essential_monthly"])
    p05 = float(probability["min_balance_quantiles"]["p05"])
    cash_buffer = round(essential + max(0.0, -p05), 2)
    requested = max(0.0, liquid - cash_buffer)
    products = [
        product
        for product in providers.wealth.products()
        if product.risk_level <= risk.level
    ]
    optimization = optimize_portfolio(
        products,
        liquid_assets=liquid,
        requested_investment=requested,
        client_risk=risk.level,
        horizon_days=365,
        cash_buffer=cash_buffer,
        drawdown_limit=risk.drawdown_tolerance,
        probability_forecast=probability,
        seed=seed,
        risk_aversion=risk_budget["risk_aversion"],
    )
    product_map = {
        product.product_id: product
        for product in providers.wealth.products()
    }
    equity_share = round(
        sum(
            item["weight"]
            for item in optimization["allocations"]
            if item["product_id"] in product_map
            and _is_equity(product_map[item["product_id"]])
        ),
        4,
    )
    risk_asset_share = round(
        sum(
            item["weight"]
            for item in optimization["allocations"]
            if item["product_id"] in product_map
            and _is_risk_asset(product_map[item["product_id"]])
        ),
        4,
    )
    return {
        "health_score": health["score"],
        "risk_level": risk.level,
        "p05_min_balance": round(p05, 2),
        "cash_buffer": cash_buffer,
        "equity_share": equity_share,
        "risk_asset_share": risk_asset_share,
    }


def _group_means(
    rows: Iterable[Dict[str, Any]],
    group_key: str,
    metric_key: str,
) -> Dict[str, float]:
    grouped: Dict[str, List[float]] = {}
    for row in rows:
        grouped.setdefault(str(row[group_key]), []).append(float(row[metric_key]))
    return {
        key: round(statistics.fmean(values), 4)
        for key, values in sorted(grouped.items())
    }


def _fairness_section(
    store: DataStore,
    clock: Clock,
    user_id: str,
    simulations: int,
) -> Dict[str, Any]:
    rows = []
    for index, persona in enumerate(PERSONAS):
        persona_store = _persona_store(store, persona, user_id)
        metrics = _metrics_for_store(
            persona_store,
            clock,
            user_id,
            simulations=simulations,
            seed=4100 + index,
        )
        rows.append({**persona, **metrics})
    metric_reports = {}
    for metric_key in (
        "health_score",
        "risk_level",
        "equity_share",
        "risk_asset_share",
    ):
        by_age = _group_means(rows, "age_band", metric_key)
        by_income = _group_means(rows, "income_band", metric_key)
        values = list(by_age.values()) + list(by_income.values())
        metric_reports[metric_key] = {
            "by_age_band": by_age,
            "by_income_band": by_income,
            "max_delta": round(max(values) - min(values), 4) if values else 0.0,
        }
    return {
        "disclaimer": (
            "Synthetic fairness stress test only. It does not prove regulatory "
            "fairness on real customers."
        ),
        "personas": rows,
        "metrics": metric_reports,
    }


def _stability_section(
    store: DataStore,
    clock: Clock,
    user_id: str,
    simulations: int,
) -> Dict[str, Any]:
    base_user = store.get_user(user_id)
    if base_user is None:
        raise ValueError(f"unknown user: {user_id}")
    variants = [
        ("base", 1.0, 1.0),
        ("balance_down_5pct", 0.95, 1.0),
        ("balance_up_5pct", 1.05, 1.0),
        ("income_down_5pct", 1.0, 0.95),
        ("income_up_5pct", 1.0, 1.05),
    ]
    rows = []
    for index, (label, balance_multiplier, income_multiplier) in enumerate(variants):
        variant_store = _persona_store(
            store,
            {
                "age": base_user.age,
                "income_multiplier": income_multiplier,
                "balance_multiplier": balance_multiplier,
                "risk_profile": base_user.risk_profile,
            },
            user_id,
        )
        metrics = _metrics_for_store(
            variant_store,
            clock,
            user_id,
            simulations=simulations,
            seed=5200 + index,
        )
        rows.append({"variant": label, **metrics})
    base = rows[0]
    providers = build_data_providers(store)
    seed_p05 = []
    for seed in (6101, 6102):
        forecast = build_probability_forecast(
            store,
            providers,
            clock,
            user_id,
            months=3,
            simulations=simulations,
            seed=seed,
        )
        seed_p05.append(forecast["min_balance_quantiles"]["p05"])
    return {
        "variants": rows,
        "metrics": {
            "max_health_score_delta": round(
                max(abs(row["health_score"] - base["health_score"]) for row in rows),
                4,
            ),
            "max_risk_level_delta": max(
                abs(row["risk_level"] - base["risk_level"]) for row in rows
            ),
            "max_p05_min_balance_delta": round(
                max(abs(row["p05_min_balance"] - base["p05_min_balance"]) for row in rows),
                2,
            ),
            "max_equity_share_delta": round(
                max(abs(row["equity_share"] - base["equity_share"]) for row in rows),
                4,
            ),
            "max_risk_asset_share_delta": round(
                max(
                    abs(row["risk_asset_share"] - base["risk_asset_share"])
                    for row in rows
                ),
                4,
            ),
            "seed_p05_range": round(max(seed_p05) - min(seed_p05), 2),
        },
    }


def _recommendation_ablation(
    store: DataStore,
    providers: Any,
    clock: Clock,
    user_id: str,
    *,
    simulations: int,
) -> List[Dict[str, Any]]:
    user = store.get_user(user_id)
    if user is None:
        raise ValueError(f"unknown user: {user_id}")
    probability = build_probability_forecast(
        store,
        providers,
        clock,
        user_id,
        months=3,
        simulations=simulations,
        seed=6301,
    )
    risk_budget = probability_risk_budget(probability)
    risk = assess_risk_profile(user, store, clock.today())
    liquid = _liquid_assets(store, providers, user_id)
    essential = float(probability["baseline"]["essential_monthly"])
    p05_buffer = essential + max(0.0, -float(probability["min_balance_quantiles"]["p05"]))
    point_buffer = essential
    products = [
        product
        for product in providers.wealth.products()
        if product.risk_level <= risk.level
    ]

    def run(cash_buffer: float, cvar_penalty: float, risk_aversion: float):
        return optimize_portfolio(
            products,
            liquid_assets=liquid,
            requested_investment=max(0.0, liquid - cash_buffer),
            client_risk=risk.level,
            horizon_days=365,
            cash_buffer=cash_buffer,
            drawdown_limit=risk.drawdown_tolerance,
            probability_forecast=probability,
            seed=6301,
            cvar_penalty=cvar_penalty,
            risk_aversion=risk_aversion,
        )

    full = run(p05_buffer, 0.05, risk_budget["risk_aversion"])
    no_cvar = run(p05_buffer, 0.0, risk_budget["risk_aversion"])
    point = run(point_buffer, 0.05, risk_budget["base_risk_aversion"])

    def equity(result: Dict[str, Any]) -> float:
        product_map = {item.product_id: item for item in products}
        return round(
            sum(
                item["weight"]
                for item in result["allocations"]
                if item["product_id"] in product_map
                and _is_equity(product_map[item["product_id"]])
            ),
            4,
        )

    def risk_asset(result: Dict[str, Any]) -> float:
        product_map = {item.product_id: item for item in products}
        return round(
            sum(
                item["weight"]
                for item in result["allocations"]
                if item["product_id"] in product_map
                and _is_risk_asset(product_map[item["product_id"]])
            ),
            4,
        )

    return [
        {
            "ablation": "remove_cvar_penalty",
            "equity_share_delta": round(equity(no_cvar) - equity(full), 4),
            "risk_asset_share_delta": round(
                risk_asset(no_cvar) - risk_asset(full),
                4,
            ),
            "estimated_drawdown_delta": round(
                no_cvar["estimated_drawdown"] - full["estimated_drawdown"],
                4,
            ),
        },
        {
            "ablation": "replace_p05_buffer_with_point_buffer",
            "equity_share_delta": round(equity(point) - equity(full), 4),
            "risk_asset_share_delta": round(
                risk_asset(point) - risk_asset(full),
                4,
            ),
            "cash_buffer_delta": round(point_buffer - p05_buffer, 2),
            "risk_aversion_delta": round(
                risk_budget["base_risk_aversion"] - risk_budget["risk_aversion"],
                4,
            ),
            "tail_loss_pct": risk_budget["tail_loss_pct"],
        },
    ]


def _ablation_section(
    store: DataStore,
    providers: Any,
    clock: Clock,
    user_id: str,
    *,
    simulations: int,
) -> Dict[str, Any]:
    user = store.get_user(user_id)
    if user is None:
        raise ValueError(f"unknown user: {user_id}")
    health = build_financial_health(store, providers, clock, user_id)
    health_rows = []
    total_weight = sum(item["weight"] for item in health["dimensions"])
    for item in health["dimensions"]:
        remaining_weight = total_weight - item["weight"]
        remaining_score = (
            sum(
                other["score"] * other["weight"]
                for other in health["dimensions"]
                if other["key"] != item["key"]
            )
            / remaining_weight
            if remaining_weight > 0
            else 0.0
        )
        health_rows.append({
            "removed": item["key"],
            "score_without_dimension": round(remaining_score, 2),
            "score_delta": round(health["score"] - remaining_score, 2),
        })

    risk = assess_risk_profile(user, store, clock.today())
    risk_rows = []
    for dimension, (score, max_score) in risk.dimension_totals.items():
        remaining_max = risk.max_total - max_score
        remaining_total = (
            (risk.total - score) / remaining_max * 100
            if remaining_max > 0
            else 0.0
        )
        risk_rows.append({
            "removed": dimension,
            "normalized_total_without_dimension": round(remaining_total, 2),
            "total_delta": round(risk.total - remaining_total, 2),
        })
    return {
        "health_dimension": health_rows,
        "risk_dimension": risk_rows,
        "recommendation": _recommendation_ablation(
            store,
            providers,
            clock,
            user_id,
            simulations=simulations,
        ),
    }


def run_model_audit(
    store: DataStore,
    providers: Any,
    clock: Clock,
    user_id: str,
    *,
    simulations: int = 120,
) -> Dict[str, Any]:
    """Run deterministic P2 checks for fairness, stability, and ablation."""
    return {
        "metadata": {
            "as_of": clock.today().isoformat(),
            "audit_version": MODEL_AUDIT_VERSION,
            "simulations": int(simulations),
            "persona_count": len(PERSONAS),
            "labels": "simulation and stress-test evidence only",
        },
        "fairness": _fairness_section(store, clock, user_id, simulations),
        "stability": _stability_section(store, clock, user_id, simulations),
        "ablation": _ablation_section(
            store,
            providers,
            clock,
            user_id,
            simulations=simulations,
        ),
    }


def write_model_audit_report(report: Dict[str, Any], path: str | Path) -> Path:
    """Write a compact Markdown version of the machine-readable audit."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fairness = report["fairness"]
    stability = report["stability"]
    ablation = report["ablation"]
    lines = [
        "# Financial Decision Model Audit",
        "",
        f"- As of: {report['metadata']['as_of']}",
        f"- Audit version: {report['metadata']['audit_version']}",
        f"- Simulations per persona: {report['metadata']['simulations']}",
        "",
        "## Fairness Stress Test",
        "",
        fairness["disclaimer"],
        "",
        "| Metric | Max group delta |",
        "|---|---:|",
    ]
    for metric, values in fairness["metrics"].items():
        lines.append(f"| {metric} | {values['max_delta']} |")
    lines.extend([
        "",
        "## Stability",
        "",
        "| Check | Value |",
        "|---|---:|",
    ])
    for metric, value in stability["metrics"].items():
        lines.append(f"| {metric} | {value} |")
    lines.extend([
        "",
        "## Ablation",
        "",
        "### Health Score",
        "",
        "| Removed dimension | Score delta |",
        "|---|---:|",
    ])
    for row in ablation["health_dimension"]:
        lines.append(f"| {row['removed']} | {row['score_delta']} |")
    lines.extend([
        "",
        "### Risk Rating",
        "",
        "| Removed dimension | Total delta |",
        "|---|---:|",
    ])
    for row in ablation["risk_dimension"]:
        lines.append(f"| {row['removed']} | {row['total_delta']} |")
    lines.extend([
        "",
        "### Recommendation",
        "",
        "| Ablation | Equity delta | Risk-asset delta | Other delta |",
        "|---|---:|---:|---:|",
    ])
    for row in ablation["recommendation"]:
        other_delta = row.get(
            "estimated_drawdown_delta",
            row.get("risk_aversion_delta", 0.0),
        )
        lines.append(
            f"| {row['ablation']} | {row['equity_share_delta']} | "
            f"{row['risk_asset_share_delta']} | {other_delta} |"
        )
    lines.append("")
    target.write_text("\n".join(lines), encoding="utf-8")
    return target
