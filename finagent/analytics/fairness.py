"""Deterministic six-case sensitivity audit for financial personalization."""
from __future__ import annotations

from typing import Any, Dict, List

from finagent.analytics.data_quality import build_data_quality
from finagent.analytics.financial_health import build_financial_health
from finagent.analytics.portfolio_optimizer import optimize_within_constraints
from finagent.analytics.product_risk import estimate_product_analytics
from finagent.data.card_ledger import sync_cards_for_account
from finagent.data.providers import build_data_providers
from finagent.data.store import Clock, DataStore
from finagent.risk.scorecard import assess_risk_profile


def _clone_store(store: DataStore) -> DataStore:
    clone = DataStore(clock=Clock(store.clock.now()))
    clone.load_snapshot(store.snapshot())
    return clone


def _scale_liquid_balance(store: DataStore, user_id: str, factor: float) -> None:
    for account in store.accounts_of(user_id):
        if account.kind not in {"current", "savings", "wallet"}:
            continue
        account.balance = round(max(account.balance * factor, 0.0), 2)
        store.touch(account)
        sync_cards_for_account(store, account)


def _scale_income(store: DataStore, user_id: str, factor: float) -> None:
    user = store.get_user(user_id)
    if user is not None:
        user.monthly_income = round(max(user.monthly_income * factor, 0.0), 2)
        store.touch(user)
    for txn in store.transactions_of(user_id):
        if txn.amount > 0 and txn.category == "收入":
            txn.amount = round(txn.amount * factor, 2)
            store.touch(txn)


def _optimizer_candidates(providers: Any) -> List[Dict[str, Any]]:
    candidates = []
    for product in providers.wealth.products():
        analytics = estimate_product_analytics(product)
        candidates.append({
            "product_id": product.product_id,
            "name": product.name,
            "risk_level": product.risk_level,
            "expected_return_pct": float(product.expected_return or 0.0) * 100,
            "volatility_pct": float(
                analytics.get("estimated_annual_volatility_pct") or 0.0
            ),
            "max_drawdown_pct": float(
                analytics.get("estimated_max_drawdown_pct") or 0.0
            ),
            "fee_rate_pct": float(analytics.get("fee_drag_1y_pct") or 0.0),
            "term_days": int(product.term_days or 0),
            "min_amount": float(product.min_amount or 0.0),
            "asset_class": product.asset_class,
        })
    return candidates


def run_fairness_audit(
    seed_store: DataStore,
    user_id: str = "u_001",
) -> Dict[str, Any]:
    """Run six controlled customer variants twice to expose unstable jumps."""
    scenarios: List[Dict[str, Any]] = [
        {
            "case_id": "baseline",
            "label": "基准客户",
            "mutator": lambda store: None,
            "variable": "none",
            "change": "0%",
        },
        {
            "case_id": "age-plus-10",
            "label": "年龄增加 10 岁",
            "variable": "age",
            "change": "+10 years",
            "mutator": lambda store: _change_age(store, user_id, 10),
        },
        {
            "case_id": "income-minus-5",
            "label": "收入下降 5%",
            "variable": "income",
            "change": "-5%",
            "mutator": lambda store: _scale_income(store, user_id, 0.95),
        },
        {
            "case_id": "income-plus-5",
            "label": "收入增加 5%",
            "variable": "income",
            "change": "+5%",
            "mutator": lambda store: _scale_income(store, user_id, 1.05),
        },
        {
            "case_id": "balance-minus-5",
            "label": "流动资金下降 5%",
            "variable": "liquid_balance",
            "change": "-5%",
            "mutator": lambda store: _scale_liquid_balance(store, user_id, 0.95),
        },
        {
            "case_id": "balance-plus-5",
            "label": "流动资金增加 5%",
            "variable": "liquid_balance",
            "change": "+5%",
            "mutator": lambda store: _scale_liquid_balance(store, user_id, 1.05),
        },
    ]

    def execute() -> List[Dict[str, Any]]:
        rows = []
        for scenario in scenarios:
            store = _clone_store(seed_store)
            scenario["mutator"](store)
            providers = build_data_providers(store)
            clock = store.clock
            user = store.get_user(user_id)
            risk = assess_risk_profile(user, store, clock.today())
            health = build_financial_health(
                store,
                providers,
                clock,
                user_id,
            )
            invest = 50_000.0
            optimizer = optimize_within_constraints(
                _optimizer_candidates(providers),
                invest,
                max_risk_level=risk.level,
                horizon_days=365,
                drawdown_tolerance_pct=risk.drawdown_tolerance * 100,
                cash_buffer_weight=0.10,
                risk_aversion=0.6,
                max_single_weight=0.35,
                max_portfolio_cvar_5_pct=max(
                    1.0,
                    risk.drawdown_tolerance * 100 * 0.25,
                ),
            )
            risk_asset_weight = sum(
                allocation["weight"]
                for allocation in optimizer["allocations"]
                if allocation["product_id"] != "CASH"
                and allocation.get("risk_level", 1) >= 2
            )
            rows.append({
                "case_id": scenario["case_id"],
                "label": scenario["label"],
                "variable": scenario["variable"],
                "change": scenario["change"],
                "health_score": health["score"],
                "risk_level": risk.level,
                "risk_label": risk.label,
                "risk_asset_weight": round(risk_asset_weight, 6),
                "portfolio_status": optimizer["status"],
                "portfolio_cvar_5_pct": optimizer["metrics"]["cvar_5_loss_pct"],
                "selected_products": [
                    allocation["product_id"]
                    for allocation in optimizer["allocations"]
                    if allocation["product_id"] != "CASH"
                ],
                "data_quality_score": build_data_quality(
                    store,
                    providers,
                    clock,
                    user_id,
                )["overall"]["score"],
            })
        return rows

    first = execute()
    second = execute()
    repeatable = first == second
    baseline = first[0]
    for row in first:
        row["health_delta_vs_baseline"] = round(
            row["health_score"] - baseline["health_score"],
            2,
        )
        row["risk_asset_weight_delta_vs_baseline"] = round(
            row["risk_asset_weight"] - baseline["risk_asset_weight"],
            6,
        )
    controlled_store = _clone_store(seed_store)
    controlled_providers = build_data_providers(controlled_store)
    controlled_candidates = _optimizer_candidates(controlled_providers)
    baseline_user = controlled_store.get_user(user_id)
    conservative_answers = {
        "b1": "a", "b2": "a", "b3": "a",
        "b4": "a", "b5": "d", "b6": "a",
    }
    aggressive_answers = {
        "b1": "d", "b2": "d", "b3": "e",
        "b4": "e", "b5": "a", "b6": "d",
    }
    conservative_risk = assess_risk_profile(
        baseline_user,
        controlled_store,
        controlled_store.clock.today(),
        answers=conservative_answers,
    )
    aggressive_risk = assess_risk_profile(
        baseline_user,
        controlled_store,
        controlled_store.clock.today(),
        answers=aggressive_answers,
    )
    baseline_risk = assess_risk_profile(
        baseline_user,
        controlled_store,
        controlled_store.clock.today(),
    )

    def portfolio_metrics(risk: Any, horizon: int, cash_weight: float) -> Dict[str, Any]:
        optimized = optimize_within_constraints(
            controlled_candidates,
            50_000,
            max_risk_level=risk.level,
            horizon_days=horizon,
            drawdown_tolerance_pct=risk.drawdown_tolerance * 100,
            cash_buffer_weight=cash_weight,
            risk_aversion=0.6,
            max_single_weight=0.35,
            max_portfolio_cvar_5_pct=max(
                1.0,
                risk.drawdown_tolerance * 100 * 0.25,
            ),
        )
        return {
            "risk_level": risk.level,
            "horizon_days": horizon,
            "cash_buffer_weight": cash_weight,
            "status": optimized["status"],
            "risk_asset_weight": round(sum(
                item["weight"] for item in optimized["allocations"]
                if item["product_id"] != "CASH"
            ), 6),
            "cvar_5_loss_pct": optimized["metrics"]["cvar_5_loss_pct"],
            "max_drawdown_pct": optimized["metrics"]["estimated_max_drawdown_pct"],
        }

    controlled_experiments = {
        "risk_questionnaire_conservative": portfolio_metrics(
            conservative_risk,
            365,
            0.10,
        ),
        "risk_questionnaire_aggressive": portfolio_metrics(
            aggressive_risk,
            365,
            0.10,
        ),
        "horizon_short_90d": portfolio_metrics(baseline_risk, 90, 0.10),
        "horizon_long_1095d": portfolio_metrics(baseline_risk, 1095, 0.10),
        "cash_buffer_5pct": portfolio_metrics(baseline_risk, 365, 0.05),
        "cash_buffer_35pct": portfolio_metrics(baseline_risk, 365, 0.35),
    }
    return {
        "model_version": "fairness-sensitivity-v1",
        "case_count": len(first),
        "repeatable_with_same_environment": repeatable,
        "cases": first,
        "controlled_experiments": controlled_experiments,
        "single_variable_pairs": [
            ["income-minus-5", "baseline", "income-plus-5"],
            ["balance-minus-5", "baseline", "balance-plus-5"],
        ],
        "limitations": [
            "六个合成客户只提供有限敏感性证据，不能单独证明公平性。",
            "没有真实受保护群体的标注数据，不能据此做法律意义上的歧视检验。",
            "结果受问卷映射、演示产品和风险模型假设影响。",
        ],
    }


def _change_age(store: DataStore, user_id: str, delta: int) -> None:
    user = store.get_user(user_id)
    if user is None:
        return
    user.age = max(18, min(int(user.age or 35) + delta, 100))
    store.touch(user)
