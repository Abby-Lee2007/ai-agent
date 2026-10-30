"""理财工具集。

覆盖赛题要求：
- 产品推荐与对比；
- 风险评估（含投资者适当性管理）；
- 一键申购 / 赎回。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from finagent.analytics import (
    build_data_quality_report,
    build_probability_forecast,
    optimize_portfolio,
    probability_risk_budget,
)
from finagent.analytics.product_risk import estimate_product_analytics
from finagent.analytics.data_quality import build_data_quality
from finagent.analytics.portfolio_optimizer import optimize_within_constraints
from finagent.analytics.live_wealth import (
    calculate_live_metrics,
    diversified_selection,
    score_live_product,
)
from finagent.data.card_ledger import (
    card_asset_total,
    sync_cards_for_account,
    update_card_balance,
    verify_card_password,
)
from finagent.data.models import Holding, WealthProduct
from finagent.data.market import build_market_overview
from finagent.data.official_sources import (
    OfficialDataError,
    official_macro_answer_line,
    official_macro_context,
)
from finagent.data.provenance import source_badge
from finagent.data.transaction_lifecycle import enrich_transaction
from finagent.risk.scorecard import assess_risk_profile
from finagent.tools.base import Tool, ToolContext, ToolParam, ToolResult, render_table
from finagent.tools.transfer_tools import resolve_account, resolve_payment_card

RISK_LABEL = {
    1: "R1 谨慎型",
    2: "R2 稳健型",
    3: "R3 平衡型",
    4: "R4 成长型",
    5: "R5 进取型",
}


def _accounts(ctx: ToolContext):
    if getattr(ctx, "providers", None) is not None:
        return ctx.providers.accounts.of(ctx.user_id)
    return ctx.store.accounts_of(ctx.user_id)


def _products(ctx: ToolContext):
    if getattr(ctx, "providers", None) is not None:
        return ctx.providers.wealth.products()
    return list(ctx.store.products.values())


def _products_by_risk(ctx: ToolContext, max_risk: int):
    if getattr(ctx, "providers", None) is not None:
        return ctx.providers.wealth.products_by_risk(max_risk)
    return ctx.store.products_by_risk(max_risk)


def _find_product(ctx: ToolContext, keyword: str):
    if getattr(ctx, "providers", None) is not None:
        return ctx.providers.wealth.find(keyword)
    return ctx.store.find_product(keyword)


def _holdings(ctx: ToolContext, status: str = "holding"):
    if getattr(ctx, "providers", None) is not None:
        return ctx.providers.wealth.holdings(ctx.user_id, status)
    return ctx.store.holdings_of(ctx.user_id, status)


def assess_user_risk(ctx: ToolContext) -> Dict[str, Any]:
    """执行风险评分卡，返回给推荐/适当性判断用的精简结果。

    完整实现见 `finagent/risk/scorecard.py`：五个维度、十个打分项、
    每项都能解释"为什么给这个分"，并附带回撤压力测试。
    """
    profile = assess_risk_profile(
        ctx.user, ctx.store, ctx.clock.today(),
        level_override=ctx.session.get("risk_level"),
        answers=ctx.session.get("risk_answers") or None,
    )
    return {
        "score": profile.level,
        "label": profile.label,
        "total": profile.total,
        "reasons": profile.reasons(),
        "notes": profile.notes,
        "total_assets": profile.total_assets,
        "profile": profile,
    }


class RiskAssessTool(Tool):
    """风险评估。"""

    name = "wealth.risk_assess"
    category = "理财操作"
    description = "评估用户风险承受能力等级，并给出与之匹配的资产配置建议"
    risk_level = "low"
    params = []
    examples = ["帮我做个风险测评", "我能承受多大风险"]

    def run(self, ctx: ToolContext) -> ToolResult:  # type: ignore[override]
        result = assess_user_risk(ctx)
        profile = result["profile"]
        dimension_rows = [
            [dimension, f"{score:.0f} / {max_score:.0f}", "#" * max(int(score / max_score * 10), 0)]
            for dimension, (score, max_score) in profile.dimension_totals.items()
        ]
        gaps = sorted(
            profile.items,
            key=lambda item: (item.max_score - item.score) / max(item.max_score, 1),
            reverse=True,
        )[:3]
        lines = [
            "投资者风险承受能力评估（监管口径评分问卷）",
            f"  客户等级：{profile.label}    得分：{profile.total:.0f}/{profile.max_total:.0f}",
            f"  问卷构成：{len(profile.items)} 题 / 5 个维度；已作答 {profile.answered} 题，"
            f"{profile.inferred} 题由账户数据推断",
            "",
            "一、打分明细（五个维度）",
            render_table(["维度", "得分", "分布"], dimension_rows),
            "",
            "二、主要扣分项",
        ]
        lines.extend(f"  · {item.name} {item.score:.0f}/{item.max_score:.0f} —— {item.detail}"
                     for item in gaps)
        lines.append("")
        lines.append("三、逐项依据（逐题评分）")
        lines.extend(
            f"  · [{item.dimension}] {item.name} {item.score:.0f}/{item.max_score:.0f} —— {item.detail}"
            for item in profile.items
        )
        lines.append("")
        lines.append("四、等级匹配（客户 C 级 vs 产品 R 级）")
        lines.append(render_table(
            ["客户等级", "可买产品", "需警示确认", "禁止销售"],
            [[profile.label, f"R1-R{profile.max_product_risk}",
              f"R{profile.confirm_product_risk}", f"R{min(profile.level + 2, 5)} 及以上"]],
        ))
        lines.append("")
        lines.append(f"五、建议资产配置比例（{profile.label}）：")
        lines.append(render_table(["资产类别", "建议比例"], profile.allocation))
        lines.append("")
        lines.append("六、风险提示：")
        lines.extend(f"  · {note}" for note in profile.notes)
        lines.append("")
        lines.append("模型说明：按「维度加权 → 总分阈值映射 → C1-C5」计算，全部规则可解释、可复核。"
                     "题目与分值为原型自拟，评分框架对齐《证券期货投资者适当性管理办法》的适当性评估思路；"
                     "不替代监管要求的正式问卷与双录流程。")
        return ToolResult(
            True,
            "\n".join(lines),
            data={
                "level": profile.level,
                "label": profile.label,
                "total": profile.total,
                "c_level": f"C{profile.level}",
                "max_product_risk": profile.max_product_risk,
                "confirm_product_risk": profile.confirm_product_risk,
                "answered": profile.answered,
                "inferred": profile.inferred,
                "dimensions": {k: list(v) for k, v in profile.dimension_totals.items()},
                "drawdown_tolerance": profile.drawdown_tolerance,
                "stress_loss": profile.stress_loss,
            },
            summary=f"{profile.label}（{profile.total:.0f} 分）",
        )


EQUITY_CATEGORIES = {"股票", "股票基金", "指数基金"}
LIVING_WORDS = ("生活费", "急用", "房租", "学费", "救命", "借的钱")


def _is_fund_product(product: Any) -> bool:
    """Treat listed ETFs and off-exchange funds as funds, regardless of asset pool."""
    return (
        getattr(product, "asset_class", "") == "fund"
        or "基金" in str(getattr(product, "category", ""))
    )


def _is_bond_fund(product: Any) -> bool:
    category = str(getattr(product, "category", ""))
    tags = " ".join(str(item) for item in (getattr(product, "tags", []) or []))
    return "债券" in category or "纯债" in tags or "固收" in tags


def _listed_trade_spec(product: Any) -> Optional[Tuple[int, str]]:
    """Return the exchange lot size and correct unit for a listed product."""
    if getattr(product, "symbol_type", "") != "stock":
        return None
    category = str(getattr(product, "category", ""))
    if getattr(product, "asset_class", "") == "bond" or "债" in category:
        return 10, "张"
    if _is_fund_product(product):
        return 100, "份"
    return 100, "股"


def _listed_minimum_order(product: Any, price: float) -> Optional[Dict[str, Any]]:
    """Estimate one-lot cost without treating an ETF lot as 100 shares."""
    spec = _listed_trade_spec(product)
    if spec is None or price <= 0:
        return None
    lot_size, unit = spec
    gross = round(price * lot_size, 2)
    fee = round(max(5.0, gross * 0.00025), 2)
    return {
        "lot_size": lot_size,
        "unit": unit,
        "gross": gross,
        "fee": fee,
        "total": round(gross + fee, 2),
    }


def _market_context_for_answer(ctx: ToolContext) -> Dict[str, Any]:
    market = ctx.session.get("market")
    if market is None:
        return {}
    return build_market_overview(market)


def _market_context_line(overview: Dict[str, Any]) -> str:
    if not overview.get("available"):
        return ""
    indices = {
        str(item.get("name")): item
        for item in overview.get("indices") or []
        if item.get("available")
    }
    wanted = ["上证指数", "深证成指", "创业板指", "科创50"]
    bits = [
        f"{name} {float(indices[name]['change_pct']):+.2f}%"
        for name in wanted
        if name in indices
    ]
    turnover = overview.get("turnover_100m_cny")
    turnover_text = (
        f"；两市成交额约 {float(turnover):,.2f} 亿元"
        if isinstance(turnover, (int, float)) and turnover > 0 else ""
    )
    return (
        f"市场环境（{overview.get('market_state') or '状态未知'}，"
        f"{overview.get('observed_at') or '行情时点未知'}）："
        f"{'、'.join(bits)}{turnover_text}。"
    )


class ProductRecommendTool(Tool):
    """产品推荐与对比。

    三个容易答非所问的地方，这里专门处理：
    1. **资产类别**：股票 / 债券 / 基金 / 衍生品分池推荐，不能拿货基去凑"股票"；
       股票还要说明"没有低风险这一档"；
    2. **资金性质**：用户说这是"生活费"时，要先按 3 个月应急金标准测算
       可投资金，钱不够就别鼓励他买权益类。
    3. **数据来源**：有行情的标"实时/净值"，没有的明确标"演示"，绝不混用。
    """

    name = "wealth.recommend"
    category = "理财操作"
    description = "按资产类别（A股/债券/基金/衍生品）、风险等级、金额和期限推荐产品"
    risk_level = "low"
    params = [
        ToolParam("risk_level", "integer", False, "可接受的最高风险等级 1-5，默认按客户评估结果"),
        ToolParam("amount", "number", False, "计划投入金额（元）"),
        ToolParam("horizon", "string", False, "投资期限", enum=["short", "mid", "long"]),
        ToolParam("asset_class", "string", False, "资产类别",
                  enum=["stock", "bond", "bond_fund", "fund", "derivative", "deposit"]),
        ToolParam("purpose", "string", False, "资金性质", enum=["living", "idle"]),
    ]
    examples = ["推荐几个理财产品", "我有5万块，稳健一点怎么配置",
                "我有2万生活费，想买股票，给我低风险的股票推荐"]

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        risk_level: Any = None,
        amount: Any = None,
        horizon: str = "mid",
        asset_class: str = "",
        purpose: str = "",
    ) -> ToolResult:
        assessment = assess_user_risk(ctx)
        profile = assessment["profile"]
        user_level = assessment["score"]
        try:
            official_macro = official_macro_context()
        except (OfficialDataError, OSError, ValueError):
            official_macro = {
                "status": "unavailable",
                "delivery": "unavailable",
                "warnings": ["官方宏观快照不可用，未用模拟宏观值替代。"],
            }
        market_context = _market_context_for_answer(ctx)
        requested = str(asset_class).lower().strip()
        wants_stock = requested in {"stock", "equity"}
        wants_bond = requested == "bond"
        wants_bond_fund = requested == "bond_fund"
        wants_fund = requested == "fund"
        wants_derivative = requested == "derivative"
        wants_deposit = requested == "deposit"
        is_living = str(purpose).lower() == "living"
        try:
            invest = float(amount) if amount is not None else 0.0
        except (TypeError, ValueError):
            invest = 0.0

        accounts = _accounts(ctx)
        cards = (
            ctx.providers.cards.of(ctx.user_id)
            if getattr(ctx, "providers", None) is not None
            else ctx.store.cards_of(ctx.user_id)
        )
        spendable_cards = []
        for card in cards:
            if card.kind not in {"debit", "virtual"} or card.status != "active":
                continue
            account = ctx.store.accounts.get(card.account_id)
            if account is not None and account.kind not in {"current", "wallet"}:
                continue
            spendable_cards.append(card)
        card_liquid = round(sum(card.balance for card in spendable_cards), 2)
        account_liquid = round(
            sum(a.balance for a in accounts if a.kind in {"current", "wallet"}),
            2,
        )
        liquid = card_liquid if card_liquid > 0 else account_liquid
        emergency_need = round(profile.monthly_expense * 3, 2)
        probability = build_probability_forecast(
            ctx.store,
            ctx.providers,
            ctx.clock,
            ctx.user_id,
            months=3,
            simulations=240,
        )
        risk_budget = probability_risk_budget(probability)
        quality = build_data_quality_report(
            ctx.store,
            ctx.providers,
            ctx.clock,
            ctx.user_id,
        )
        p05_min_balance = float(probability["min_balance_quantiles"]["p05"])
        liquidity_stress = max(0.0, -p05_min_balance)
        probability_cash_buffer = (
            float(probability["baseline"]["essential_monthly"]) + liquidity_stress
        )
        required_cash_buffer = round(
            max(probability_cash_buffer, emergency_need if is_living else 0.0),
            2,
        )
        investable = max(0.0, round(liquid - required_cash_buffer, 2))
        affordable_budget = min(
            invest if invest > 0 else liquid,
            liquid,
        )
        data_quality = build_data_quality(
            ctx.store,
            ctx.providers,
            ctx.clock,
            ctx.user_id,
        )
        quality_overall = data_quality["overall"]
        if not quality_overall["personalization_allowed"]:
            cash_amount = max(
                min(liquid, emergency_need if emergency_need > 0 else liquid),
                0.0,
            )
            return ToolResult(
                False,
                "数据质量不足，未生成个性化理财配置。\n"
                f"  综合数据质量：{quality_overall['score']:.2f}/100；"
                "执行层已阻断个性化推荐。\n"
                f"  原因：{'；'.join(data_quality['block_reasons']) or '质量分低于门槛'}\n"
                f"  现金安全方案：保留约 {cash_amount:,.2f} 元活期现金，不自动买入任何产品。\n"
                "  请先补齐交易、卡片或产品数据后再试；系统不会放宽硬约束。",
                data={
                    "blocked_by_data_quality": True,
                    "data_quality": data_quality,
                    "cash_safe_plan": {
                        "product_id": "CASH",
                        "amount": round(cash_amount, 2),
                        "weight": 1.0,
                    },
                    "products": [],
                    "display_count": 0,
                },
                summary="数据质量不足，个性化配置已阻断并返回现金安全方案",
            )

        market = ctx.session.get("market")
        if market is None:
            from finagent.data.market import MarketDataProvider
            market = MarketDataProvider()

        lines: List[str] = []
        # ---------------- 选池 ----------------
        if wants_derivative:
            return self._derivative_answer(ctx, assessment, invest, is_living, liquid, emergency_need)

        if wants_stock:
            lines.append("先纠正一个前提：股票没有「低风险」这一档。")
            lines.append("  个股和股票基金都属于 R4-R5（权益类），短期回撤 20% 以上很正常；")
            lines.append("  真正低风险的是货币基金、国债、大额存单，年化通常只有 2%-3%。")
            lines.append("")
            if user_level <= 2:
                alternatives = [p.name for p in _products_by_risk(ctx, user_level)][:3]
                lines.append(f"按你的测评等级「{assessment['label']}」，R4 的股票比承受能力高两级，")
                lines.append("按投资者适当性管理不适合直接配置。可以先看这些同等级产品：")
                lines.append("  " + "、".join(alternatives))
                return ToolResult(
                    True, "\n".join(lines),
                    data={"asset_class": "stock", "blocked_by_suitability": True,
                          "user_level": user_level, "max_risk": user_level},
                    summary="股票风险等级超出承受能力，已改为提示低风险替代品",
                )
            max_risk = 4
            pool = [p for p in _products(ctx)
                    if p.asset_class == "stock" and p.risk_level <= max_risk]
            # 有实时数据时只保留能取到真实行情的品种，避免表里混入演示产品
            if market.enabled:
                live_pool = [p for p in pool if p.has_live_data]
                if live_pool:
                    pool = live_pool
            lines.append("下面给你的是「权益类里相对低波动」的选择，注意它们仍然是 R4、不是低风险资产：")
        elif wants_bond_fund:
            max_risk = min(user_level + 1, 5)
            pool = [
                p for p in _products(ctx)
                if _is_fund_product(p)
                and _is_bond_fund(p)
                and p.risk_level <= max_risk
            ]
            lines.append("债券基金（仍属于基金，主要风险来自利率和信用）：")
        elif wants_bond:
            max_risk = min(user_level + 1, 5)
            pool = [p for p in _products(ctx)
                    if p.asset_class == "bond" or p.category == "债券基金"]
            lines.append("债券类产品（国债 / 可转债 / 债券基金）：")
            lines.append("  国债以持有到期吃票息为主；可转债有股性，波动明显大于纯债。")
        elif wants_fund:
            max_risk = min(user_level + 1, 5)
            # "推荐基金" defaults to non-bond funds. Users who specifically want
            # bond funds are routed to the bond pool by the intent parser.
            pool = [
                p for p in _products(ctx)
                if _is_fund_product(p)
                and not _is_bond_fund(p)
                and p.risk_level <= max_risk
            ]
            if market.enabled:
                live_pool = [
                    p for p in pool
                    if p.has_live_data
                ]
                if live_pool:
                    pool = live_pool
            lines.append(
                f"按你的风险等级「{assessment['label']}」，"
                "可选的基金如下（默认排除纯债基金）："
            )
        elif wants_deposit:
            max_risk = 1
            pool = [p for p in _products(ctx) if p.asset_class == "deposit"]
            lines.append("存款类产品（受存款保险保护，不属于理财）：")
        else:
            try:
                max_risk = int(risk_level) if risk_level is not None else user_level
            except (TypeError, ValueError):
                max_risk = user_level
            max_risk = max(1, min(5, max_risk))
            if max_risk > user_level:
                max_risk = user_level
            pool = [p for p in _products_by_risk(ctx, max_risk)
                    if p.min_amount <= (invest or p.min_amount)]
            if not pool:
                pool = _products_by_risk(ctx, max_risk)
            lines.append(
                f"主动推荐仅展示与您「{assessment['label']}」同等级的产品"
                f"（R1-R{max_risk}）；更高风险产品不会作为推荐结果展示。"
            )
        affordable = [
            product for product in pool
            if product.min_amount <= max(affordable_budget, 0.0)
        ]
        if not affordable:
            return ToolResult(
                False,
                f"按当前可动用资金 {affordable_budget:,.2f} 元，没有找到达到起投门槛且"
                f"符合「{assessment['label']}」的产品。\n"
                "可以切换到「存款」「债券」「基金」查看其他类别，或先补充资金后重新推荐。",
                data={
                    "affordable_budget": round(affordable_budget, 2),
                    "max_risk": max_risk,
                    "asset_class": requested or "mixed",
                    "products": [],
                    "display_count": 0,
                    "filter_options": ["存款", "债券", "基金"],
                },
                summary="当前资金不足，未展示买不起的产品",
            )
        if market.enabled:
            live_affordable = [
                product for product in affordable
                if product.has_live_data
            ]
            if live_affordable:
                affordable = live_affordable
        pool = affordable
        filter_key = requested or "mixed"
        previous_filter = str(ctx.session.get("wealth_recommend_filter") or "")
        seen_ids = list(ctx.session.get("wealth_recommend_seen") or [])
        cycle = int(ctx.session.get("wealth_recommend_cycle") or 1)
        batch_number = int(ctx.session.get("wealth_recommend_batch") or 0) + 1
        if previous_filter != filter_key:
            seen_ids = []
            cycle = 1
            batch_number = 1
        seen_set = set(str(item) for item in seen_ids)
        unseen = [product for product in pool if product.product_id not in seen_set]
        cycle_reset = False
        if not unseen:
            cycle += 1
            seen_set = set()
            unseen = list(pool)
            cycle_reset = True
        analysis_pool = unseen[:10]
        if not analysis_pool:
            return ToolResult(False, "当前没有匹配您风险等级的产品。")

        # ---------------- 取真实行情 / 历史序列 / 净值 ----------------
        stock_symbols = [p.symbol for p in analysis_pool if p.symbol_type == "stock"]
        fund_codes = [p.symbol for p in analysis_pool if p.symbol_type == "fund"]
        quotes = market.quotes(stock_symbols) if stock_symbols else {}
        navs = {}
        for code in fund_codes:
            nav = market.fund_nav(code)
            if nav is not None:
                navs[code] = nav
        if market.enabled:
            affordable_at_live_price = []
            for product in analysis_pool:
                quote = quotes.get(product.symbol) if product.symbol_type == "stock" else None
                minimum_order = (
                    _listed_minimum_order(product, float(quote.price))
                    if quote is not None else None
                )
                if (
                    minimum_order is not None
                    and float(minimum_order["total"]) > affordable_budget
                ):
                    continue
                affordable_at_live_price.append(product)
            analysis_pool = affordable_at_live_price
            if not analysis_pool:
                return ToolResult(
                    False,
                    f"按当前可动用资金 {affordable_budget:,.2f} 元，"
                    "没有找到计入最小交易单位和模拟佣金后仍买得起的产品。",
                    data={
                        "affordable_budget": round(affordable_budget, 2),
                        "max_risk": max_risk,
                        "asset_class": requested or "mixed",
                        "products": [],
                        "display_count": 0,
                    },
                    summary="当前资金不足，未展示买不起的场内产品",
                )

        risk_free = official_macro.get("reverse_repo_7d_rate_pct") or 0.0
        held_ids = {
            holding.product_id for holding in _holdings(ctx, status="holding")
        }
        ranked_candidates: List[Dict[str, Any]] = []
        quant_by_product: Dict[str, Dict[str, Any]] = {}
        if market.enabled:
            for product in analysis_pool:
                history_fetcher = getattr(
                    market,
                    "fund_nav_history" if product.symbol_type == "fund" else "history",
                    None,
                )
                history = (
                    history_fetcher(product.symbol, 250)
                    if callable(history_fetcher) else None
                )
                metrics = (
                    calculate_live_metrics(
                        history,
                        risk_free_rate_pct=float(risk_free or 0.0),
                    )
                    if history is not None
                    else {
                        "available": False,
                        "reason": "公开历史行情暂不可用",
                        "observations": 0,
                    }
                )
                score = score_live_product(
                    product,
                    metrics,
                    user_risk_level=user_level,
                    drawdown_tolerance_pct=profile.drawdown_tolerance * 100,
                    horizon=horizon,
                    quote=quotes.get(product.symbol),
                    already_held=product.product_id in held_ids,
                )
                quant_by_product[product.product_id] = {
                    "metrics": metrics,
                    "score": score,
                }
                ranked_candidates.append({
                    "product": product,
                    "product_id": product.product_id,
                    "category": product.category,
                    "score": score["score"],
                })
            ranked_candidates.sort(
                key=lambda item: (
                    item["score"],
                    quant_by_product[item["product_id"]]["metrics"].get(
                        "sharpe_ratio", -999,
                    ),
                ),
                reverse=True,
            )
            ranked_with_history = [
                item for item in ranked_candidates
                if quant_by_product[item["product_id"]]["metrics"].get("available")
            ]
            if ranked_with_history:
                chosen_rows = diversified_selection(ranked_with_history, limit=5)
                pool = [item["product"] for item in chosen_rows]
            else:
                # Keep live current-price products visible, but do not invent
                # historical metrics when the public history source is down.
                pool = analysis_pool[:5]
        else:
            pool = analysis_pool[:5]

        if not pool:
            return ToolResult(False, "当前没有可形成有效推荐的公开行情标的。")
        ctx.session["wealth_recommend_filter"] = filter_key
        ctx.session["wealth_recommend_seen"] = list(
            seen_set | {product.product_id for product in pool}
        )
        ctx.session["wealth_recommend_cycle"] = cycle
        ctx.session["wealth_recommend_batch"] = batch_number
        lines.append(
            f"根据您的风险等级「{assessment['label']}」，本次共展示 {len(pool)} 只产品。"
        )
        lines.append(f"  当前为第 {batch_number} 批候选")
        if cycle > 1:
            lines.append(f"  当前为第 {cycle} 轮候选")
        if cycle_reset:
            lines.append(
                "  当前筛选条件下没有更多未展示过的新产品，"
                "因此重新从完整候选集开始，不冒充新品。"
            )
        if market.enabled:
            lines.append(
                "  量化筛选：适当性25 + 历史回撤20 + Sharpe20 + 趋势/估值20"
                " + 流动性10 + 数据质量5；按用户风险画像、投资期限和已持仓综合排序。"
            )
            lines.append(
                "  股票/ETF/债券历史来自腾讯财经公开日线，场外基金来自东方财富净值序列；"
                "只用于风险收益特征，不保证未来收益。"
            )
            lines.append("")

        combined_rows: List[List[str]] = []
        product_details: List[Dict[str, Any]] = []
        live_count = 0
        demo_count = 0
        for index, product in enumerate(pool, start=1):
            metric = "-"
            movement = "-"
            data_label = "演示参数"
            if product.symbol_type == "stock" and product.symbol in quotes:
                quote = quotes[product.symbol]
                metric = f"{quote.price:,.3f} 元"
                movement = f"{quote.change_pct:+.2f}%"
                data_label = source_badge(quote.source, quote.live, quote.is_demo, quote.stale)
                live_count += 1
            elif product.symbol_type == "fund" and product.symbol in navs:
                nav = navs[product.symbol]
                metric = f"净值 {nav.nav:,.4f}（{nav.date}）"
                movement = f"{nav.change_pct:+.2f}%" if nav.change_pct is not None else "-"
                data_label = (
                    f"{nav.date} · "
                    f"{source_badge(nav.source, nav.live, nav.is_demo, nav.stale)}"
                )
                live_count += 1
            else:
                if product.has_live_data:
                    metric = "公开行情未取到"
                    data_label = "公开数据未返回"
                elif product.expected_return > 0:
                    metric = f"演示年化 {product.expected_return * 100:.2f}%"
                if not product.has_live_data:
                    data_label = "演示参数"
                    demo_count += 1
            quote = quotes.get(product.symbol) if product.symbol_type == "stock" else None
            minimum_order = (
                _listed_minimum_order(product, float(quote.price))
                if quote is not None else None
            )
            category_display = product.category
            if minimum_order is not None and _is_fund_product(product):
                category_display = f"{product.category} / 场内 ETF"
            if minimum_order is not None:
                minimum_order_label = (
                    f"最低约 {float(minimum_order['total']):,.2f} 元"
                    f"（{minimum_order['lot_size']}{minimum_order['unit']}，含模拟佣金）"
                )
            elif product.symbol_type == "stock":
                minimum_order_label = "最低交易额待公开行情确认"
            else:
                minimum_order_label = f"起投 {product.min_amount:,.0f} 元"
            flow_text = (
                f"{minimum_order_label}；"
                f"{product.redemption_rule or product.liquidity}"
            )
            risk_text = f"R{product.risk_level}"
            if product.risk_level > user_level:
                risk_text += " 需确认"
            quant = quant_by_product.get(product.product_id) or {}
            quant_metrics = dict(quant.get("metrics") or {})
            quant_score = dict(quant.get("score") or {})
            score_text = (
                f"{float(quant_score['score']):.1f} · {quant_score['label']}"
                if quant_score.get("score") is not None else "-"
            )
            analytics = estimate_product_analytics(product)
            if quant_metrics.get("available"):
                analytics.update({
                    "estimated_annual_volatility_pct": quant_metrics[
                        "annualized_volatility_pct"
                    ],
                    "estimated_max_drawdown_pct": quant_metrics["max_drawdown_pct"],
                    "annualized_return_pct": quant_metrics["annualized_return_pct"],
                    "return_1m_pct": quant_metrics["return_1m_pct"],
                    "return_3m_pct": quant_metrics["return_3m_pct"],
                    "return_6m_pct": quant_metrics["return_6m_pct"],
                    "sharpe_ratio": quant_metrics["sharpe_ratio"],
                    "observations": quant_metrics["observations"],
                    "history_start": quant_metrics["start_date"],
                    "history_end": quant_metrics["end_date"],
                    "trend": (
                        "价格在20日均线上方"
                        if quant_metrics.get("above_ma20")
                        else "价格低于20日均线"
                    ),
                    "risk_method": quant_metrics["method"],
                    "is_estimate": False,
                })
            combined_rows.append([
                str(index),
                f"{product.name} {product.symbol.upper() if product.symbol else product.product_id}",
                f"{product.category} / {risk_text}",
                score_text,
                f"{metric} {movement}".strip(),
                flow_text,
                data_label,
            ])
            product_details.append({
                "index": index,
                "product_id": product.product_id,
                "name": product.name,
                "code": product.symbol.upper() if product.symbol else product.product_id,
                "category": product.category,
                "category_display": category_display,
                "risk_level": product.risk_level,
                "risk_label": risk_text,
                "advice_score": quant_score.get("score"),
                "advice_label": quant_score.get("label"),
                "score_components": quant_score.get("components") or {},
                "score_reasons": quant_score.get("reasons") or [],
                "requires_risk_confirmation": product.risk_level > user_level,
                "metric": metric,
                "change": movement,
                "liquidity": product.redemption_rule or product.liquidity,
                "min_amount": product.min_amount,
                "minimum_order_amount": (
                    float(minimum_order["total"])
                    if minimum_order is not None
                    else float(product.min_amount)
                ),
                "minimum_order_label": minimum_order_label,
                "minimum_order_lot_size": (
                    int(minimum_order["lot_size"])
                    if minimum_order is not None else 0
                ),
                "minimum_order_unit": (
                    str(minimum_order["unit"])
                    if minimum_order is not None else ""
                ),
                "fee_rate": product.fee_rate,
                "data_source": data_label,
                "history_source": quant_metrics.get("source", ""),
                "history_observations": quant_metrics.get("observations", 0),
                "description": self._plain_product_description(product),
                "analytics": analytics,
                "expected_return_pct": (
                    float(analytics["annualized_return_pct"])
                    if analytics.get("annualized_return_pct") is not None
                    else float(product.expected_return or 0.0) * 100
                ),
                "volatility_pct": float(
                    analytics.get("estimated_annual_volatility_pct") or 0.0
                ),
                "max_drawdown_pct": float(
                    analytics.get("estimated_max_drawdown_pct") or 0.0
                ),
                "fee_rate_pct": float(analytics.get("fee_drag_1y_pct") or 0.0),
                "term_days": int(product.term_days or 0),
                "asset_class": product.asset_class,
            })

        lines.append("")
        lines.append("统一产品清单：")
        lines.append(render_table(
            ["#", "产品/代码", "类型/风险", "适合度", "价格或收益参考", "门槛与流动性", "数据"],
            combined_rows,
        ))
        if live_count:
            stamps = [q.timestamp for q in quotes.values() if q.timestamp]
            quote_sources = sorted({q.source for q in quotes.values() if q.source})
            if stamps:
                lines.append(
                    f"  行情来源：{'、'.join(quote_sources) or '公开行情'}（{max(stamps)}，公开数据增强）"
                )
            if navs:
                nav_sources = sorted({nav.source for nav in navs.values() if nav.source})
                lines.append(
                    f"  净值来源：{'、'.join(nav_sources) or '公开基金净值'}（场外基金为 T-1 披露）"
                )
            stale_count = sum(1 for quote in quotes.values() if quote.stale)
            stale_count += sum(1 for nav in navs.values() if nav.stale)
            if stale_count:
                lines.append(f"  新鲜度提示：{stale_count} 条行情超过时效阈值，已标记为“已过期”。")
            pe_bits = [f"{q.name} PE {q.pe:.1f}" for q in quotes.values() if q.pe]
            if pe_bits:
                lines.append("  个股参考：" + "；".join(pe_bits))
            lines.append("  真实行情只代表当前价格与当日涨跌，历史表现不代表未来收益。")
        if demo_count:
            sources = sorted({product.source for product in pool if product.source})
            lines.append(
                f"  演示数据提示：{demo_count} 只产品的收益率、费率和期限为原型参数"
                + (f"（来源：{'、'.join(sources)}）" if sources else "")
                + "，不代表真实产品承诺。"
            )

        lines.append("")
        lines.append("产品说明（用大白话解释）：")
        for item in product_details:
            lines.append(f"  {item['index']}. {item['name']}：{item['description']}")
        if market.enabled:
            lines.append("")
            lines.append("量化依据与风险画像匹配：")
            for item in product_details:
                analytics = item.get("analytics") or {}
                if (
                    analytics.get("return_3m_pct") is None
                    and analytics.get("is_estimate", True)
                ):
                    continue
                return_3m = analytics.get("return_3m_pct")
                return_3m_text = (
                    f"{float(return_3m):+.2f}%"
                    if isinstance(return_3m, (int, float)) else "样本不足"
                )
                lines.append(
                    f"  · {item['name']}：适合度 {item.get('advice_score') or 0:.1f}/100"
                    f"（{item.get('advice_label') or '数据不足'}）；"
                    f"近3月 {return_3m_text} · "
                    f"年化波动 {float(analytics.get('estimated_annual_volatility_pct') or 0):.2f}% · "
                    f"最大回撤 {float(analytics.get('estimated_max_drawdown_pct') or 0):.2f}% · "
                    f"Sharpe {float(analytics.get('sharpe_ratio') or 0):.2f}。"
                )
                if item.get("score_reasons"):
                    lines.append("    理由：" + "；".join(item["score_reasons"]))
                if item.get("history_observations"):
                    lines.append(
                        f"    样本：{item['history_observations']} 个公开交易日"
                        f"（{analytics.get('history_start') or '-'} 至 "
                        f"{analytics.get('history_end') or '-'}）。"
                    )

        # ---------------- 生活费检查 ----------------
        if is_living:
            lines.append("")
            lines.append("关于这笔钱：你说这是生活费，这一点比收益率重要得多。")
            lines.append(f"  活期可用约 {liquid:,.2f} 元，月均支出约 {profile.monthly_expense:,.2f} 元，"
                         f"3 个月应急金需要 {emergency_need:,.2f} 元。")
            if investable <= 0:
                lines.append(f"  按这个标准你还差 {emergency_need - liquid:,.2f} 元，"
                             f"{invest:,.2f} 元生活费不建议投入任何权益类产品。")
                lines.append("  更合适的做法：先放在货币基金/活期+（R1，随时可取），补足应急金后再谈投资。")
            else:
                lines.append(f"  留足应急金后，可投资金约 {investable:,.2f} 元，"
                             f"权益类建议不超过其中的 30%。")
        elif invest > liquid:
            lines.append("")
            lines.append(f"  注意：你提到的 {invest:,.2f} 元超过活期可用 {liquid:,.2f} 元，"
                         f"实际下单会受余额限制。")

        # ---------------- 组合建议 ----------------
        allow_allocation = not (is_living and investable <= 0)
        quant_plan: List[Dict[str, Any]] = []
        portfolio_optimization: Optional[Dict[str, Any]] = None
        optimization: Optional[Dict[str, Any]] = None
        horizon_days = {
            "short": 90,
            "mid": 365,
            "long": 1095,
        }.get(horizon, 365)
        cash_buffer_weight = {
            "short": 0.20,
            "mid": 0.10,
            "long": 0.05,
        }.get(horizon, 0.10)
        effective_risk_aversion = float(
            risk_budget.get("risk_aversion") or 0.6
        )
        if invest > 0:
            portfolio_optimization = optimize_within_constraints(
                product_details,
                invest,
                max_risk_level=user_level,
                horizon_days=horizon_days,
                drawdown_tolerance_pct=profile.drawdown_tolerance * 100,
                cash_buffer_weight=cash_buffer_weight,
                risk_aversion=effective_risk_aversion,
                max_single_weight=0.35,
                max_portfolio_cvar_5_pct=max(
                    1.0,
                    profile.drawdown_tolerance * 100 * 0.25,
                ),
                simulations=0,
                seed=20261029,
            )

        if invest > 0 and allow_allocation:
            optimization_risk = (
                min(5, user_level + 1) if wants_stock else user_level
            )
            plan_pool = [
                product
                for product in pool
                if product.risk_level <= optimization_risk
            ]
            if quality.get("blocks_personalization"):
                plan_pool = []
            optimization = optimize_portfolio(
                plan_pool,
                liquid_assets=liquid,
                requested_investment=invest,
                client_risk=optimization_risk,
                horizon_days=horizon_days,
                cash_buffer=required_cash_buffer,
                drawdown_limit=profile.drawdown_tolerance,
                probability_forecast=probability,
                seed=20261028,
                risk_aversion=effective_risk_aversion,
            )
            plan_rows = [
                [
                    item["name"],
                    f"{item['weight'] * 100:.0f}%",
                    f"{item['amount']:,.2f}",
                    f"预期年化 {item['expected_return'] * 100:.2f}% / R{item['risk_level']}",
                ]
                for item in optimization["allocations"]
            ]
            lines.append("")
            lines.append(
                f"约束优化组合 / 组合配置建议（可投预算 "
                f"{optimization['spendable_budget']:,.2f} 元）："
            )
            if optimization["status"] == "optimized":
                quant_plan = list(optimization["allocations"])
                lines.append(render_table(
                    ["产品", "比例", "金额", "收益/风险"],
                    plan_rows,
                ))
                lines.append(
                    f"  预期年化 {optimization['expected_return'] * 100:.2f}%；"
                    f"年化波动 {optimization['volatility'] * 100:.2f}%；"
                    f"最坏 5% 损失估计 {optimization['cvar_5pct'] * 100:.2f}%；"
                    f"估计最大回撤 {optimization['estimated_drawdown'] * 100:.2f}%"
                    f"（上限 {profile.drawdown_tolerance * 100:.2f}%）。"
                )
                lines.append(
                    f"  现金缓冲 {required_cash_buffer:,.2f} 元；"
                    f"P50-P05 尾部损失宽度 {risk_budget['tail_loss_pct'] * 100:.2f}%，"
                    f"风险厌恶参数调整为 {effective_risk_aversion:.2f}。"
                )
                lines.append(
                    f"  {effective_risk_aversion:.2f} 是无量纲系数：每 1 个百分点"
                    " CVaR 损失按其数值扣除目标值，不是仓位比例或收益百分点。"
                )
                if (
                    portfolio_optimization
                    and portfolio_optimization.get("independent_verification")
                ):
                    lines.append(
                        "  独立约束复核："
                        f"{'通过' if portfolio_optimization['independent_verification']['passed'] else '未通过'}；"
                        "硬约束未被自动放宽。"
                    )
                if wants_stock and quotes:
                    allocation_weights = {
                        item["product_id"]: item["weight"]
                        for item in optimization["allocations"]
                    }
                    lines.extend(self._live_plan(
                        pool,
                        quotes,
                        optimization["spendable_budget"],
                        allocation_weights,
                    ))
            else:
                allow_allocation = False
                lines.append(
                    f"  约束优化组合：当前没有满足全部硬约束的组合。"
                    f"{optimization['reason']}。"
                    "系统不会为了给出推荐而放宽风险、期限或现金缓冲约束。"
                )
                lines.append(
                    f"  P05 最低余额 {probability['min_balance_quantiles']['p05']:,.2f} 元，"
                    f"负余额概率 {probability['negative_probability'] * 100:.1f}%。"
                )
        elif invest > 0:
            allow_allocation = False

        if wants_stock:
            lines.append("")
            lines.append("三条提醒：")
            lines.append(f"  1. R4 比你测评的 {assessment['label']} 高一级，申购时会额外确认，"
                         f"也可能被适当性规则拦截；")
            lines.append("  2. 权益类只适合 3 年以上不用的闲钱，短期要用的钱别放进来；")
            lines.append("  3. 个股波动大于指数，新手优先宽基/红利指数，别重仓单只。")
        elif len([p for p in pool if p.expected_return > 0]) >= 2:
            best = max(pool, key=lambda p: p.expected_return)
            lowest = min(pool, key=lambda p: p.risk_level)
            lines.append("")
            lines.append(f"一句话对比：收益最高的是「{best.name}」（年化 {best.expected_return * 100:.2f}%，"
                         f"R{best.risk_level}）；最稳的是「{lowest.name}」（R{lowest.risk_level}）。")

        lines.append("")
        official_line = official_macro_answer_line(official_macro)
        if official_line:
            lines.append(official_line)
            lines.append(
                "  这些是利率和物价环境指标，不是基金、债券或存款的预期收益。"
            )
            lines.append("")
        market_line = _market_context_line(market_context)
        if market_line:
            lines.append(market_line)
            lines.append("  指数涨跌只描述本次行情时点，不是买入建议，也不保证后续走势。")
            lines.append("")
        if live_count:
            lines.append("（真实行情来自公开接口，仅供演示；不构成投资建议，投资有风险）")
        if demo_count:
            lines.append("（标『演示』的收益率为原型参数，不代表实际收益；理财非存款，投资有风险）")
        if not live_count and not demo_count:
            lines.append("（以上为演示数据，不构成投资建议）")
        lines.append("")
        if allow_allocation:
            lines.append(
                "是否继续申购？请回复例如「购买第1只，金额5000元，用工资卡」；"
                "也可以回复「暂不购买」。在下单前还会再次核对产品、金额、付款卡并验证身份。"
            )
        else:
            if is_living:
                lines.append("这笔生活费不足应急金，建议暂不购买；如需演示交易，可先补充资金。")
            else:
                lines.append("当前没有满足现金缓冲和风险约束的可投组合，建议先保留现金。")

        class_labels = {"stock": "A股股票", "bond": "债券", "fund": "基金",
                        "derivative": "衍生品", "deposit": "存款"}
        class_label = class_labels.get(requested, "理财")
        combined_quality = dict(quality)
        combined_quality.update({
            "overall": data_quality["overall"],
            "metric_definitions": data_quality["metric_definitions"],
            "critical_issues": data_quality["critical_issues"],
            "block_reasons": data_quality["block_reasons"],
            "is_production_data_quality": False,
            "limitations": data_quality["limitations"],
        })
        ctx.session["last_wealth_products"] = list(product_details)
        return ToolResult(
            True,
            "\n".join(lines),
            data={
                "products": [p.product_id for p in pool],
                "max_risk": max_risk,
                "invest": invest,
                "asset_class": requested or "mixed",
                "purpose": "living" if is_living else "",
                "emergency_need": emergency_need,
                "required_cash_buffer": required_cash_buffer,
                "investable": investable,
                "affordable_budget": round(affordable_budget, 2),
                "display_count": len(pool),
                "total_candidates": len(affordable),
                "live_count": live_count,
                "demo_count": demo_count,
                "product_details": product_details,
                "purchase_prompt": allow_allocation,
                "has_more": len(affordable) > len(pool),
                "batch_number": batch_number,
                "batch_cycle": cycle,
                "batch_reset": cycle_reset,
                "seen_count": len(ctx.session["wealth_recommend_seen"]),
                "filter_options": ["存款", "债券", "基金", "换一批"],
                "official_macro": official_macro,
                "market_overview": market_context,
                "recommendation_method": (
                    "数据质量闸门 + 公开行情量化排序 + 约束优化 + "
                    "投资者适当性 + CVaR/回撤/期限/现金缓冲/集中度硬约束"
                    if market.enabled else "离线演示参数匹配"
                ),
                "live_quant_count": sum(
                    1 for item in product_details
                    if not (item.get("analytics") or {}).get("is_estimate", True)
                ),
                "risk_budget_plan": quant_plan,
                "portfolio_optimization": portfolio_optimization,
                "data_quality": combined_quality,
                "unified_data_quality": data_quality,
                "cashflow_distribution": {
                    "p05_min_balance": probability["min_balance_quantiles"]["p05"],
                    "p25_min_balance": probability["min_balance_quantiles"]["p25"],
                    "p50_min_balance": probability["min_balance_quantiles"]["p50"],
                    "p75_min_balance": probability["min_balance_quantiles"]["p75"],
                    "p95_min_balance": probability["min_balance_quantiles"]["p95"],
                    "terminal_interval_90": probability["intervals"]["terminal_90"],
                    "negative_probability": probability["negative_probability"],
                    "random_seed": probability["random_seed"],
                    "scenario_count": probability["scenario_count"],
                    "tail_loss_pct": risk_budget["tail_loss_pct"],
                    "risk_aversion": risk_budget["risk_aversion"],
                    "risk_budget_method": risk_budget["method"],
                },
                "optimization": optimization,
            },
            summary=(f"已按{class_label}推荐 {len(pool)} 只产品"
                     + ("；生活费不足应急金，不建议买入" if is_living and investable <= 0 else "")),
        )

    @staticmethod
    def _plain_product_description(product) -> str:
        """Explain how the product works and its main risk in everyday language."""
        category = product.category
        if "货币" in category:
            what = ("它把大家的闲钱集中起来，主要投向短期、流动性较好的现金类资产，"
                    "收益通常不高，但净值波动一般较小。")
            risk = "它不是银行存款，不承诺保本；极端市场下收益也可能下降。"
        elif "国债" in category:
            what = ("本质是把钱借给国家，持有到期主要赚票息；如果提前在交易所卖出，"
                    "成交价格会随市场利率变化。")
            risk = "持有到期和提前卖出是两种不同结果，提前卖出可能亏损。"
        elif "可转债" in category:
            what = ("它先是债，满足条件后又能换成股票，所以既有债券的利息，也会跟着股价上下波动。")
            risk = "波动通常明显高于纯债，不适合当作短期保本工具。"
        elif "债券" in category or "固收" in product.tags:
            what = ("主要把钱借给政府、银行或企业来赚利息，也会通过买卖债券获取价差，"
                    "所以收益通常比活期高一些。")
            risk = "利率上升时债券价格可能下跌；信用债还要承担发行方违约风险。"
        elif "大额存单" in category:
            what = ("它是银行发行的存款产品，按约定利率计息，在存款保险法定限额内受到保护。")
            risk = "提前支取通常按活期利率计息，会损失部分利息；门槛也较高。"
        elif "指数" in category:
            what = ("它一次买入一篮子股票或ETF，不用押注某一家公司，分散度比单只股票更好。")
            risk = "仍然属于权益资产，市场下跌时会出现明显浮亏，短期资金不适合。"
        elif "股票" in category:
            what = ("买它相当于成为公司的小股东，收益来自股价上涨和可能的分红。")
            risk = "公司经营、行业和市场情绪都会影响价格，本金可能明显亏损。"
        elif "混合" in category:
            what = ("基金会同时配置股票和债券，用债券打底、股票争取更高收益。")
            risk = "股票仓位越高，波动越大，不保证本金和收益。"
        elif category in {"期权", "股指期货", "结构性产品"}:
            what = "它依赖未来价格或特定条件触发，结构复杂，并可能使用杠杆。"
            risk = "可能损失全部权利金甚至出现超过本金的损失，只适合专业合格投资者。"
        else:
            what = product.description or "它通过配置金融资产获取利息、价差或分红收益。"
            risk = "收益不是承诺，净值会波动，赎回前应确认期限和费用。"
        suitability = {
            1: "适合几乎不用的短期资金和保守型客户。",
            2: "适合能接受小幅波动、持有数月到一年的稳健型客户。",
            3: "适合能接受一定回撤、投资期限至少一年的平衡型客户。",
            4: "适合有较长投资期限、能承受较大波动的成长型客户。",
            5: "只适合专业或进取型客户，普通稳健资金不应配置。",
        }.get(product.risk_level, "")
        return f"{what}{risk}{suitability}"

    @staticmethod
    def _derivative_answer(ctx: ToolContext, assessment: Dict[str, Any],
                           invest: float, is_living: bool,
                           liquid: float, emergency_need: float) -> ToolResult:
        """衍生品：只做科普与风险揭示，不给申购建议。"""
        products = sorted(
            (p for p in _products(ctx) if p.asset_class == "derivative"),
            key=lambda p: p.product_id,
        )
        rows = [[p.name, p.category, "R5", p.description] for p in products]
        lines = [
            "先说结论：衍生品不适合作为「稳健理财」来买，我也不会给你推荐具体仓位。",
            "",
            "原因：",
            "  · 期权、股指期货、雪球结构都属于 R5（最高风险档），带杠杆；",
            "  · 期权买方可能损失全部权利金，期货可能穿仓，雪球在极端行情下会损失本金；",
            "  · 按投资者适当性管理，这类产品只面向**合格投资者**，"
            "需要单独认证和风险揭示，不是普通账户能直接买的。",
        ]
        if products:
            lines.append("")
            lines.append("原型里收录的衍生品（仅作了解，不可申购）：")
            lines.append(render_table(["产品", "类别", "风险", "说明"], rows))
        lines.append("")
        lines.append(f"你当前的测评等级是「{assessment['label']}」，本演示账户也未做合格投资者认证，"
                     "所以这类产品在这里只能看、不能买。")
        if is_living:
            lines.append(f"另外，你自己也说了这 {invest:,.2f} 元是生活费——更不该碰杠杆产品。")
        lines.append("")
        lines.append("如果你的目标是稳健增值，回到「推荐几个稳健理财」或「推荐债券」更合适。")
        return ToolResult(
            True,
            "\n".join(lines),
            data={
                "asset_class": "derivative",
                "products": [p.product_id for p in products],
                "blocked_by_suitability": True,
                "max_risk": 5,
                "user_level": assessment["score"],
                "emergency_need": emergency_need,
                "investable": max(0.0, round(liquid - emergency_need, 2)),
                "live_count": 0,
                "demo_count": len(products),
            },
            summary="衍生品需合格投资者，已做风险揭示，不建议申购",
        )

    @staticmethod
    def _live_plan(
        pool,
        quotes: Dict[str, Any],
        invest: float,
        allocation_weights: Dict[str, float] | None = None,
    ) -> List[str]:
        """按真实价格估算能买多少股（A 股按 100 股整数倍）。"""
        available = [p for p in pool if p.symbol in quotes]
        if not available:
            return []
        if allocation_weights:
            available = [
                product
                for product in available
                if allocation_weights.get(product.product_id, 0.0) > 0
            ]
            chosen = available
            weights = [
                allocation_weights.get(product.product_id, 0.0)
                for product in chosen
            ]
            total = sum(weights)
            if total > 0:
                weights = [weight / total for weight in weights]
        else:
            chosen = available[:2]
            weights = [0.6, 0.4] if len(chosen) == 2 else [1.0] * len(chosen)
        if not chosen:
            return []
        rows = []
        for product, weight in zip(chosen, weights):
            quote = quotes[product.symbol]
            money = round(invest * weight, 2)
            lots = int(money // (quote.price * 100)) if quote.price > 0 else 0
            shares = lots * 100
            rows.append([
                product.name,
                quote.symbol.upper(),
                f"{weight * 100:.0f}%",
                f"{money:,.2f}",
                f"{shares:,} 股" if shares else "不足 100 股",
            ])
        return [
            "",
            f"按实时价格估算的配置（投入 {invest:,.2f} 元，A 股按 100 股整数倍）：",
            render_table(["产品", "代码", "比例", "金额", "约可买"], rows),
            "  实际成交价与股数以券商下单为准；这里只做金额与仓位的粗略测算。",
        ]

    @staticmethod
    def _build_plan(pool, invest: float, horizon: str, wants_stock: bool):
        """按期限偏好给出配置比例；股票池集中在同一风险档时退化为等权。"""
        weights = {
            "short": [(1, 0.7), (2, 0.3)],
            "mid": [(1, 0.4), (2, 0.3), (3, 0.3)],
            "long": [(2, 0.3), (3, 0.4), (4, 0.3)],
        }[horizon if horizon in {"short", "mid", "long"} else "mid"]
        levels = [(level, weight) for level, weight in weights if any(p.risk_level == level for p in pool)]
        plan_rows = []
        expected_return = 0.0
        if not levels:
            chosen = pool[0]
            if invest >= chosen.min_amount:
                plan_rows.append([chosen.name, "100%", f"{invest:,.2f}",
                                  f"{chosen.expected_return * 100:.2f}%"])
                expected_return = chosen.expected_return
            return plan_rows, expected_return
        total_weight = sum(weight for _, weight in levels) or 1.0
        for level, weight in levels:
            candidates = [p for p in pool if p.risk_level == level]
            if not candidates:
                continue
            chosen = candidates[0]
            normalized = weight / total_weight
            money = round(invest * normalized, 2)
            if money < chosen.min_amount:
                continue
            plan_rows.append([chosen.name, f"{normalized * 100:.0f}%", f"{money:,.2f}",
                              f"{chosen.expected_return * 100:.2f}%"])
            expected_return += chosen.expected_return * normalized
        return plan_rows, expected_return

    @staticmethod
    def _quantitative_plan(
        details: List[Dict[str, Any]],
        invest: float,
        user_level: int,
    ) -> tuple[List[List[str]], List[Dict[str, Any]]]:
        eligible = [
            item for item in details
            if item.get("advice_score") is not None
            and float(item["advice_score"]) >= 45
            and int(item["risk_level"]) <= user_level + 1
            and float(item["min_amount"]) <= invest
        ][:3]
        if not eligible:
            return [], []
        score_sum = sum(max(float(item["advice_score"]), 0.1) for item in eligible)
        rows = []
        for item in eligible:
            weight = max(float(item["advice_score"]), 0.1) / score_sum
            amount = round(invest * weight, 2)
            if amount < float(item["min_amount"]):
                continue
            rows.append([
                item["name"],
                f"{float(item['advice_score']):.1f}",
                f"{weight * 100:.1f}%",
                f"{amount:,.2f} 元",
            ])
        return rows, eligible


class ProductCompareTool(Tool):
    """Compare 2-3 products without issuing a purchase recommendation."""

    name = "wealth.compare"
    category = "理财操作"
    description = "横向比较风险、收益口径、流动性、费率、数据来源与适用人群"
    risk_level = "low"
    params = [
        ToolParam("products", "string", False, "产品名称，可用顿号、逗号或“和”连接"),
        ToolParam(
            "asset_class",
            "string",
            False,
            "未指定产品时的比较范围",
            enum=["stock", "bond", "fund", "deposit", "mixed"],
        ),
    ]
    examples = ["比较一下稳健型理财产品", "对比稳利债券A和微众活期+"]

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        products: str = "",
        asset_class: str = "",
    ) -> ToolResult:
        assessment = assess_user_risk(ctx)
        user_level = int(assessment["score"])
        try:
            official_macro = official_macro_context()
        except (OfficialDataError, OSError, ValueError):
            official_macro = {
                "status": "unavailable",
                "delivery": "unavailable",
            }
        market_context = _market_context_for_answer(ctx)
        requested = str(asset_class or "").lower().strip()
        names = [
            item.strip()
            for item in re.split(r"[、,，;；]|和", str(products or ""))
            if item.strip()
        ]
        selected = []
        for name in names:
            product = _find_product(ctx, name)
            if product is not None and product not in selected:
                selected.append(product)

        if not selected:
            max_risk = min(user_level + 1, 5)
            pool = [
                product for product in _products(ctx)
                if product.asset_class != "derivative"
                and product.risk_level <= max_risk
            ]
            if requested == "fund":
                pool = [
                    product for product in pool
                    if _is_fund_product(product) and not _is_bond_fund(product)
                ]
            elif requested == "bond":
                pool = [
                    product for product in pool
                    if product.asset_class == "bond" or _is_bond_fund(product)
                ]
            elif requested == "stock":
                pool = [product for product in pool if product.asset_class == "stock"]
            elif requested == "deposit":
                pool = [product for product in pool if product.asset_class == "deposit"]
            if requested in {"fund", "stock"}:
                live_candidates = [product for product in pool if product.has_live_data]
                if live_candidates:
                    pool = live_candidates

            role_order = {"存款": 0, "货币基金": 1, "债券基金": 2, "混合基金": 3}
            used_categories = set()
            for product in sorted(
                pool,
                key=lambda item: (
                    item.risk_level,
                    role_order.get(item.category, 9),
                    item.name,
                ),
            ):
                if product.category in used_categories:
                    continue
                selected.append(product)
                used_categories.add(product.category)
                if len(selected) >= 3:
                    break
            if len(selected) < 2:
                for product in pool:
                    if product in selected:
                        continue
                    selected.append(product)
                    if len(selected) >= 3:
                        break
        selected = selected[:3]
        if len(selected) < 2:
            return ToolResult(
                False,
                "至少需要 2 个可比较的产品。请指定产品名称，"
                "或说“比较稳健型理财产品”自动选择候选。",
            )

        market = ctx.session.get("market")
        if market is None:
            from finagent.data.market import MarketDataProvider
            market = MarketDataProvider()
        stock_symbols = [
            product.symbol for product in selected
            if product.symbol_type == "stock"
        ]
        quotes = market.quotes(stock_symbols) if stock_symbols else {}
        navs = {}
        for product in selected:
            if product.symbol_type != "fund":
                continue
            nav = market.fund_nav(product.symbol)
            if nav is not None:
                navs[product.symbol] = nav

        compared: List[Dict[str, Any]] = []
        for product in selected:
            current_value = (
                f"演示参考 {product.expected_return * 100:.2f}%"
                if product.expected_return > 0 else "-"
            )
            data_source = "演示参数"
            is_live = False
            if product.symbol_type == "stock" and product.symbol in quotes:
                quote = quotes[product.symbol]
                current_value = f"{quote.price:,.3f} 元 / {quote.change_pct:+.2f}%"
                data_source = source_badge(
                    quote.source, quote.live, quote.is_demo, quote.stale
                )
                is_live = True
            elif product.symbol_type == "fund" and product.symbol in navs:
                nav = navs[product.symbol]
                current_value = f"净值 {nav.nav:,.4f}（{nav.date}）"
                data_source = source_badge(
                    nav.source, nav.live, nav.is_demo, nav.stale
                )
                is_live = True
            analytics = estimate_product_analytics(product)
            compared.append({
                "product_id": product.product_id,
                "name": product.name,
                "code": product.symbol.upper() if product.symbol else product.product_id,
                "category": product.category,
                "risk_level": product.risk_level,
                "min_amount": product.min_amount,
                "liquidity": product.redemption_rule or product.liquidity,
                "fee_rate": product.fee_rate,
                "current_value": current_value,
                "data_source": data_source,
                "is_live": is_live,
                "volatility_pct": analytics.get("estimated_annual_volatility_pct", 0),
                "drawdown_pct": analytics.get("estimated_max_drawdown_pct", 0),
                "description": ProductRecommendTool._plain_product_description(product),
            })

        criteria = [
            ["风险等级"] + [f"R{item['risk_level']}" for item in compared],
            ["产品类型"] + [item["category"] for item in compared],
            ["当前价格/净值"] + [item["current_value"] for item in compared],
            ["数据来源"] + [item["data_source"] for item in compared],
            ["起投金额"] + [f"{item['min_amount']:,.0f} 元" for item in compared],
            ["流动性"] + [item["liquidity"] for item in compared],
            ["费率"] + [f"{item['fee_rate'] * 100:.2f}%" for item in compared],
            ["情景波动"] + [f"{item['volatility_pct']:.1f}%" for item in compared],
            ["情景最大回撤"] + [f"{item['drawdown_pct']:.1f}%" for item in compared],
        ]
        lowest_risk = min(compared, key=lambda item: item["risk_level"])
        lowest_cost = min(compared, key=lambda item: item["fee_rate"])
        most_liquid = min(
            compared,
            key=lambda item: (
                0 if "T+0" in item["liquidity"] or "活期" in item["liquidity"] else 1,
                item["liquidity"],
            ),
        )
        lines = [
            "理财产品横向对比",
            f"  比较范围：{'、'.join(item['name'] for item in compared)}",
            render_table(
                ["比较项"] + [item["name"] for item in compared],
                criteria,
            ),
            "",
            "差异结论：",
            f"  · 风险最低：{lowest_risk['name']}（R{lowest_risk['risk_level']}）",
            f"  · 费率最低：{lowest_cost['name']}（{lowest_cost['fee_rate'] * 100:.2f}%）",
            f"  · 流动性最好：{most_liquid['name']}（{most_liquid['liquidity']}）",
            "  · 对比模式不生成组合比例，也不要求立即购买。",
            "",
            "适用人群说明：",
        ]
        lines.extend(f"  · {item['name']}：{item['description']}" for item in compared)
        lines.append("")
        official_line = official_macro_answer_line(official_macro)
        if official_line:
            lines.append(official_line)
            lines.append(
                "  宏观利率只用于理解当前利率环境，不能替代产品净值、"
                "信用风险和流动性分析。"
            )
            lines.append("")
        market_line = _market_context_line(market_context)
        if market_line:
            lines.append(market_line)
            lines.append("  市场指数用于解释当时的风险偏好，不改变产品条款。")
            lines.append("")
        if any(item["is_live"] for item in compared):
            lines.append(
                "真实价格或净值只代表当前公开数据，不代表未来收益；"
                "场外基金净值通常为 T-1 披露。"
            )
        else:
            lines.append("本次比较包含演示参数，收益与费率不是实际产品承诺。")
        return ToolResult(
            True,
            "\n".join(lines),
            data={
                "comparison": {
                    "products": compared,
                    "criteria": criteria,
                    "lowest_risk": lowest_risk["name"],
                    "lowest_cost": lowest_cost["name"],
                    "most_liquid": most_liquid["name"],
                    "purchase_prompt": False,
                },
                "user_level": user_level,
                "official_macro": official_macro,
                "market_overview": market_context,
            },
            summary=f"已横向对比 {len(compared)} 个产品",
        )


def _market_symbol_from_query(query: str) -> str:
    text = re.sub(r"\s+", "", str(query or "")).lower()
    match = re.fullmatch(r"(?:(sh|sz|bj))?(\d{6})", text)
    if not match:
        return ""
    prefix, code = match.groups()
    if prefix:
        return prefix + code
    if code.startswith(("5", "6", "9")):
        return "sh" + code
    if code.startswith(("0", "1", "2", "3")):
        return "sz" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return ""


def resolve_live_product(
    ctx: ToolContext,
    query: str,
) -> Optional[WealthProduct]:
    """Resolve a real listed instrument by six-digit code through live quotes."""
    symbol = _market_symbol_from_query(query)
    if not symbol:
        return None
    market = ctx.session.get("market")
    if market is None or not market.enabled:
        return None
    quote = market.quote(symbol)
    if quote is None or quote.price <= 0:
        return None
    code = symbol[-6:]
    if code.startswith("11") or code.startswith("12"):
        category, asset_class, risk_level = "可转债", "bond", 4
    elif code.startswith("01"):
        category, asset_class, risk_level = "国债", "bond", 2
    elif code.startswith("511"):
        category, asset_class, risk_level = "货币基金", "fund", 1
    elif code.startswith(("5", "15", "16", "18")):
        category, asset_class, risk_level = "场内ETF", "fund", 4
    else:
        category, asset_class, risk_level = "股票", "stock", 4
    product_id = f"LIVE-{symbol.upper()}"
    existing = ctx.store.products.get(product_id)
    if existing is not None:
        return existing
    product = WealthProduct(
        product_id=product_id,
        name=quote.name or symbol.upper(),
        category=category,
        risk_level=risk_level,
        expected_return=0.0,
        min_amount=100.0,
        term_days=0,
        liquidity="T+1",
        issuer="证券交易所",
        tags=["公开行情", "真实标的", "模拟交易"],
        description=(
            f"{quote.name or symbol.upper()}为公开市场真实上市标的。"
            "本项目只模拟下单和持仓，不接入真实证券账户。"
        ),
        symbol=symbol,
        symbol_type="stock",
        asset_class=asset_class,
        fee_rate=0.00025,
        redemption_rule="T+1 可用；实际以上市规则与券商为准",
        risk_disclosure="证券价格会波动，模拟成交不代表真实成交。",
        source=f"public-market:{quote.source}",
        is_live=True,
        is_demo=False,
    )
    ctx.store.products[product_id] = product
    ctx.store.touch(product)
    return product


def _live_price_snapshot(
    ctx: ToolContext,
    product: WealthProduct,
) -> Optional[Dict[str, Any]]:
    market = ctx.session.get("market")
    if market is None or not market.enabled or not product.has_live_data:
        return None
    if product.symbol_type == "fund":
        nav = market.fund_nav(product.symbol)
        if nav is None:
            return None
        return {
            "kind": "fund_nav",
            "price": float(nav.nav),
            "source": nav.source,
            "as_of": nav.date,
            "symbol": product.symbol.upper(),
            "change_pct": nav.change_pct,
        }
    quote = market.quote(product.symbol)
    if quote is None or quote.price <= 0:
        return None
    return {
        "kind": "quote",
        "price": float(quote.price),
        "source": quote.source,
        "as_of": quote.timestamp,
        "symbol": quote.symbol.upper(),
        "change_pct": quote.change_pct,
    }


def build_subscription_order(
    ctx: ToolContext,
    product: WealthProduct,
    requested_amount: float,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Price a simulated order from a fresh public quote or latest fund NAV."""
    snapshot = _live_price_snapshot(ctx, product)
    if snapshot is None:
        return None, "公开行情当前不可用，模拟申购未执行，未用演示价格替代。"
    price = float(snapshot["price"])
    if price <= 0:
        return None, "公开行情价格无效，模拟申购未执行。"
    if snapshot["kind"] == "fund_nav":
        fee = round(requested_amount * max(product.fee_rate, 0.0), 2)
        net = round(requested_amount - fee, 2)
        quantity = round(net / price, 4)
        return {
            **snapshot,
            "quantity": quantity,
            "unit": "份",
            "gross_amount": round(requested_amount, 2),
            "fee": fee,
            "total_debit": round(requested_amount, 2),
            "market_value": net,
            "lot_size": 0,
        }, ""

    minimum_order = _listed_minimum_order(product, price)
    if minimum_order is None:
        return None, "无法确定该场内产品的最小交易单位，模拟申购未执行。"
    lot_size = int(minimum_order["lot_size"])
    unit = str(minimum_order["unit"])
    lot_cost = float(minimum_order["gross"])
    minimum_total = float(minimum_order["total"])
    if requested_amount < minimum_total:
        instrument = (
            "场内基金"
            if _is_fund_product(product)
            else ("交易所债券" if unit == "张" else "股票")
        )
        return None, (
            f"「{product.name}」是{instrument}，按最新价 {price:,.3f} 元和"
            f"最小交易单位 {lot_size}{unit} 计算，一手成交额约 {lot_cost:,.2f} 元；"
            f"计入模拟佣金后至少需要约 {minimum_total:,.2f} 元。"
        )
    lots = int(requested_amount // lot_cost)
    quantity = lots * lot_size
    while quantity > 0:
        gross = round(price * quantity, 2)
        # A-share/ETF/交易所债券买方的模拟佣金；卖出印花税不在本函数中估算。
        fee = round(max(5.0, gross * 0.00025), 2)
        total = round(gross + fee, 2)
        if total <= requested_amount:
            return {
                **snapshot,
                "quantity": quantity,
                "unit": unit,
                "gross_amount": gross,
                "fee": fee,
                "total_debit": total,
                "market_value": gross,
                "lot_size": lot_size,
            }, ""
        quantity -= lot_size
    return None, "申购金额不足以覆盖一手证券及模拟佣金。"


class SubscribeTool(Tool):
    """一键申购。"""

    name = "wealth.subscribe"
    category = "理财操作"
    description = "申购指定理财产品，从本人借记卡扣款并生成持仓"
    risk_level = "high"
    requires_confirmation = True
    params = [
        ToolParam("product", "string", True, "产品名称或代码，如 稳利债券A / P002"),
        ToolParam("amount", "number", True, "申购金额（元）"),
        ToolParam("account", "string", True, "付款银行卡，如 工资卡/尾号6222"),
        ToolParam("payment_password", "string", True, "付款卡 6 位交易密码"),
    ]
    examples = ["帮我买1万块的稳利债券A", "申购5000元微众活期+"]

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        product: str,
        amount: Any,
        account: str,
        payment_password: str = "",
    ) -> ToolResult:
        user = ctx.user
        store = ctx.store
        try:
            value = round(float(amount), 2)
        except (TypeError, ValueError):
            return ToolResult(False, f"金额「{amount}」无法识别。")
        requested_value = value

        target = _find_product(ctx, product)
        if target is None:
            target = resolve_live_product(ctx, str(product))
        if target is None:
            return ToolResult(
                False,
                f"没有找到产品「{product}」。如果申购真实上市标的，"
                "请输入 6 位证券代码，例如「600519」「510300」或以 sh/sz/bj 开头；"
                "也可以说「推荐理财产品」查看已接入的公开行情标的。",
            )
        if value < target.min_amount:
            return ToolResult(False, f"「{target.name}」起投金额为 {target.min_amount:,.2f} 元。")
        live_order: Optional[Dict[str, Any]] = None
        if target.has_live_data:
            live_order, order_error = build_subscription_order(ctx, target, value)
            if live_order is None:
                return ToolResult(
                    False,
                    order_error,
                    data={"blocked": True, "reason": "live_quote_unavailable"},
                )
            value = float(live_order["total_debit"])

        # 投资者适当性管理：产品风险高于客户承受能力时给出明确提示
        assessment = assess_user_risk(ctx)
        user_level = assessment["score"]
        notice = ""
        if target.risk_level > user_level + 1:
            alternatives = [p.name for p in _products_by_risk(ctx, user_level)][:3]
            return ToolResult(
                False,
                f"「{target.name}」风险等级 R{target.risk_level}，高于您的风险承受能力 {assessment['label']}，"
                f"按投资者适当性管理要求无法直接申购。\n"
                f"  可选替代：{'、'.join(alternatives) if alternatives else '暂无'}",
                data={"blocked": True, "reason": "suitability"},
            )
        if target.risk_level == user_level + 1:
            notice = (f"  风险提示：该产品风险等级 R{target.risk_level} 高于您的评估等级 "
                      f"{assessment['label']}，请确认可以承受相应波动。")

        payer = resolve_payment_card(ctx, account)
        if payer is None:
            return ToolResult(False, "没有找到指定的付款银行卡，请选择工资卡、尾号或其他借记卡。")
        if not verify_card_password(payer, payment_password):
            return ToolResult(False, "付款卡交易密码验证失败，申购未执行。")
        if payer.available < value:
            return ToolResult(
                False,
                f"{payer.brand}（尾号 {payer.tail}）余额不足"
                f"（可用 {payer.available:,.2f} 元，需要 {value:,.2f} 元）。",
            )

        update_card_balance(ctx.store, payer, payer.balance - value)
        txn = store.add_transaction(
            user_id=user.user_id,
            account_id=payer.account_id,
            amount=-value,
            merchant=target.name,
            category="理财",
            channel="APP",
            note=f"{target.category}申购",
        )
        enrich_transaction(
            txn,
            ctx.clock,
            metadata={
                "product_id": target.product_id,
                "product_name": target.name,
                "amount": value,
                **(
                    {
                        "execution_price": live_order["price"],
                        "price_source": live_order["source"],
                        "price_as_of": live_order["as_of"],
                        "quantity": live_order["quantity"],
                        "unit": live_order["unit"],
                        "estimated_fee": live_order["fee"],
                        "gross_amount": live_order["gross_amount"],
                    }
                    if live_order is not None else {}
                ),
            },
        )
        asset_total_after = card_asset_total(ctx.store, user.user_id)
        # 同一产品已有持仓时直接累加，避免持仓列表里出现一堆零散的同名份额
        holding = next(
            (h for h in _holdings(ctx, status="holding")
             if h.product_id == target.product_id),
            None,
        )
        if holding is None:
            price = float(live_order["price"]) if live_order is not None else 0.0
            quantity = float(live_order["quantity"]) if live_order is not None else 0.0
            holding = Holding(
                holding_id=store.next_id("holdings"),
                user_id=user.user_id,
                product_id=target.product_id,
                principal=value,
                market_value=(
                    float(live_order["market_value"])
                    if live_order is not None else value
                ),
                purchase_date=ctx.clock.iso(),
                source=(
                    f"public-market:{live_order['source']}"
                    if live_order is not None
                    else "agent:simulation"
                ),
                is_live=live_order is not None,
                is_demo=live_order is None,
                quantity=quantity,
                average_cost=(
                    round(value / quantity, 6)
                    if quantity > 0 else 0.0
                ),
                last_price=price,
                price_source=(
                    str(live_order["source"]) if live_order is not None else ""
                ),
                price_as_of=(
                    str(live_order["as_of"]) if live_order is not None else ""
                ),
            )
            ctx.store.touch(holding)
            store.holdings[holding.holding_id] = holding
        else:
            holding.principal = round(holding.principal + value, 2)
            holding.market_value = round(
                holding.market_value
                + (
                    float(live_order["market_value"])
                    if live_order is not None else value
                ),
                2,
            )
            if live_order is not None:
                added_quantity = float(live_order["quantity"])
                holding.quantity = round(holding.quantity + added_quantity, 4)
                holding.average_cost = (
                    round(holding.principal / holding.quantity, 6)
                    if holding.quantity > 0 else 0.0
                )
                holding.last_price = float(live_order["price"])
                holding.price_source = str(live_order["source"])
                holding.price_as_of = str(live_order["as_of"])
                holding.source = f"public-market:{live_order['source']}"
                holding.is_live = True
                holding.is_demo = False
            ctx.store.touch(holding)

        sandbox = ctx.session.get("sandbox")
        sandbox_execution_id = ""
        if sandbox is not None:
            execution = sandbox.broker.submit_order(
                txn.txn_id,
                side="subscribe",
                product_id=target.product_id,
                amount=value,
                symbol=target.symbol,
                limit_price=(
                    float(live_order["price"])
                    if live_order is not None else None
                ),
                quantity=(
                    float(live_order["quantity"])
                    if live_order is not None else 0.0
                ),
                fees=(
                    float(live_order["fee"])
                    if live_order is not None else 0.0
                ),
            )
            sandbox.gateway.complete_immediately(
                execution,
                provider_reference=f"BROKER-{txn.txn_id[-8:].upper()}",
            )
            sandbox_execution_id = execution.execution_id

        annual = value * target.expected_return
        annual_label = "演示年化" if target.is_demo else "参数年化(非实时)"
        lines = [
            f"申购成功：「{target.name}」{value:,.2f} 元",
            f"  产品风险等级：R{target.risk_level} | {annual_label} {target.expected_return * 100:.2f}%",
            f"  费率：{target.fee_rate * 100:.2f}% | 赎回规则：{target.redemption_rule}",
            f"  预期年收益约 {annual:,.2f} 元（测算值，非承诺收益）",
            f"  付款银行卡：{payer.brand}（尾号 {payer.tail}）  剩余可用 {payer.available:,.2f} 元",
            f"  银行卡资产总额：{asset_total_after:,.2f} 元",
            f"  持仓编号：{holding.holding_id}   流水号：{txn.txn_id}",
            f"  风险揭示：{target.risk_disclosure}",
        ]
        if live_order is not None and live_order["kind"] == "quote":
            lines = [
                f"模拟申购成交：「{target.name}」（{live_order['symbol']}）",
                f"  行情价格：{float(live_order['price']):,.3f} 元"
                f"（{live_order['source']}，{live_order['as_of']}）",
                f"  模拟买入数量：{float(live_order['quantity']):,.0f} {live_order['unit']}"
                f"（按最小交易单位 {live_order['lot_size']}{live_order['unit']} 取整）",
                f"  证券成交额：{float(live_order['gross_amount']):,.2f} 元；"
                f"模拟佣金：{float(live_order['fee']):,.2f} 元",
                f"  本次实际扣款：{value:,.2f} 元；"
                f"申请预算 {requested_value:,.2f} 元，余款未扣。",
                f"  产品风险等级：R{target.risk_level} | 持仓成本已按模拟佣金调整",
                f"  付款银行卡：{payer.brand}（尾号 {payer.tail}）  剩余可用 {payer.available:,.2f} 元",
                f"  银行卡资产总额：{asset_total_after:,.2f} 元",
                f"  持仓编号：{holding.holding_id}   流水号：{txn.txn_id}",
                "  本系统未连接券商，不会向真实交易所提交委托；价格来自公开行情，成交为沙箱模拟。",
            ]
        elif live_order is not None and live_order["kind"] == "fund_nav":
            lines = [
                f"模拟基金申购成交：「{target.name}」（{live_order['symbol']}）",
                f"  最新公开净值：{float(live_order['price']):,.4f} 元"
                f"（{live_order['source']}，净值日期 {live_order['as_of']}）",
                f"  申购金额：{float(live_order['gross_amount']):,.2f} 元；"
                f"模拟申购费：{float(live_order['fee']):,.2f} 元",
                f"  估算确认份额：{float(live_order['quantity']):,.4f} 份",
                f"  产品风险等级：R{target.risk_level} | 场外基金净值通常为 T-1 披露",
                f"  付款银行卡：{payer.brand}（尾号 {payer.tail}）  剩余可用 {payer.available:,.2f} 元",
                f"  银行卡资产总额：{asset_total_after:,.2f} 元",
                f"  持仓编号：{holding.holding_id}   流水号：{txn.txn_id}",
                "  本系统未连接基金销售机构，份额和费用为沙箱模拟。",
            ]
        elif target.expected_return <= 0:
            lines = [
                f"申购成功：「{target.name}」{value:,.2f} 元",
                f"  产品风险等级：R{target.risk_level}",
                f"  付款银行卡：{payer.brand}（尾号 {payer.tail}）  剩余可用 {payer.available:,.2f} 元",
                f"  银行卡资产总额：{asset_total_after:,.2f} 元",
                f"  持仓编号：{holding.holding_id}   流水号：{txn.txn_id}",
            ]
        if notice:
            lines.append(notice)
        return ToolResult(
            True,
            "\n".join(lines),
            data={"holding_id": holding.holding_id, "product": target.name, "amount": value,
                  "requested_amount": requested_value,
                  "risk_level": target.risk_level, "payment_card_id": payer.card_id,
                  "payment_card_balance_after": payer.balance,
                  "asset_total_after": asset_total_after, "txn_id": txn.txn_id,
                  "sandbox_execution_id": sandbox_execution_id,
                  "execution_price": (
                      live_order["price"] if live_order is not None else None
                  ),
                  "quantity": (
                      live_order["quantity"] if live_order is not None else None
                  ),
                  "unit": live_order["unit"] if live_order is not None else "",
                  "fees": live_order["fee"] if live_order is not None else 0.0,
                  "price_source": (
                      live_order["source"] if live_order is not None else ""
                  ),
                  "price_as_of": (
                      live_order["as_of"] if live_order is not None else ""
                  )},
            summary=f"已申购「{target.name}」{value:,.2f} 元",
        )


class WealthRedeemTool(Tool):
    """一键赎回。"""

    name = "wealth.redeem"
    category = "理财操作"
    description = "赎回理财产品，资金回到活期账户"
    risk_level = "medium"
    requires_confirmation = True
    params = [
        ToolParam("product", "string", False, "产品名称或代码，留空表示赎回全部持仓"),
        ToolParam("amount", "number", False, "赎回金额，留空表示全部赎回"),
    ]
    examples = ["赎回稳利债券A", "全部赎回"]

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        product: str = "",
        amount: Any = None,
    ) -> ToolResult:
        user = ctx.user
        store = ctx.store
        holdings = _holdings(ctx, status="holding")
        if not holdings:
            return ToolResult(True, "您当前没有持仓可以赎回。", summary="无持仓")

        targets: List[Holding] = []
        if product:
            target_product = _find_product(ctx, product)
            if target_product:
                targets = [h for h in holdings if h.product_id == target_product.product_id]
            if not targets:
                return ToolResult(False, f"没有找到「{product}」的持仓。")
        else:
            targets = holdings

        try:
            redeem_amount = float(amount) if amount is not None else None
        except (TypeError, ValueError):
            redeem_amount = None

        account = resolve_account(ctx)
        rows = []
        total_redeemed = 0.0
        sandbox_execution_ids: List[str] = []
        for holding in targets:
            prod = ctx.providers.wealth.get(holding.product_id) if getattr(ctx, "providers", None) else store.products.get(holding.product_id)
            name = prod.name if prod else holding.product_id
            value = holding.market_value if redeem_amount is None else min(redeem_amount, holding.market_value)
            if value <= 0:
                continue
            holding.market_value = round(holding.market_value - value, 2)
            holding.profit = round(holding.market_value - holding.principal, 2)
            if holding.market_value <= 0.01:
                holding.market_value = 0.0
                holding.status = "redeemed"
                holding.redeem_date = ctx.clock.iso()
            ctx.store.touch(holding)
            if account:
                account.balance = round(account.balance + value, 2)
                ctx.store.touch(account)
                sync_cards_for_account(ctx.store, account)
                redemption_txn = store.add_transaction(
                    user_id=user.user_id,
                    account_id=account.account_id,
                    amount=value,
                    merchant=name,
                    category="理财",
                    channel="APP",
                    note="理财赎回",
                )
                enrich_transaction(
                    redemption_txn,
                    ctx.clock,
                    metadata={
                        "holding_id": holding.holding_id,
                        "product_id": holding.product_id,
                        "amount": value,
                    },
                )
                sandbox = ctx.session.get("sandbox")
                if sandbox is not None:
                    execution = sandbox.broker.submit_order(
                        redemption_txn.txn_id,
                        side="redeem",
                        product_id=holding.product_id,
                        amount=value,
                        symbol=getattr(prod, "symbol", ""),
                    )
                    sandbox.gateway.complete_immediately(
                        execution,
                        provider_reference=(
                            f"BROKER-REDEEM-{redemption_txn.txn_id[-8:].upper()}"
                        ),
                    )
                    sandbox_execution_ids.append(execution.execution_id)
            total_redeemed += value
            rows.append([name, f"{value:,.2f}", "已清仓" if holding.status == "redeemed" else "部分赎回",
                         prod.liquidity if prod else "-"])
            if redeem_amount is not None:
                break

        if not rows:
            return ToolResult(True, "没有可赎回的份额。", summary="无可赎回份额")

        lines = [
            f"赎回成功，共 {total_redeemed:,.2f} 元已回到活期账户：",
            render_table(["产品", "赎回金额", "状态", "到账方式"], rows),
        ]
        if account:
            lines.append(f"  活期账户当前余额 {account.balance:,.2f} 元")
        lines.append("  提示：货币基金一般 T+0/T+1 到账，权益类产品到账时间以产品说明为准。")
        return ToolResult(
            True,
            "\n".join(lines),
            data={
                "total": round(total_redeemed, 2),
                "sandbox_execution_ids": sandbox_execution_ids,
            },
            summary=f"已赎回 {total_redeemed:,.2f} 元",
        )


class PortfolioTool(Tool):
    """持仓总览。"""

    name = "wealth.portfolio"
    category = "理财操作"
    description = "查看当前理财持仓、收益与资产结构"
    risk_level = "low"
    params = []
    examples = ["我的理财持仓", "看看我赚了多少"]

    def run(self, ctx: ToolContext) -> ToolResult:  # type: ignore[override]
        holdings = _holdings(ctx)
        if not holdings:
            return ToolResult(True, "您当前没有理财持仓。", summary="无持仓")
        rows = []
        total_value = total_profit = 0.0
        live_positions = 0
        for h in holdings:
            prod = ctx.providers.wealth.get(h.product_id) if getattr(ctx, "providers", None) else ctx.store.products.get(h.product_id)
            name = prod.name if prod else h.product_id
            risk = f"R{prod.risk_level}" if prod else "-"
            current_value = h.market_value
            current_price = h.last_price
            price_time = h.price_as_of
            price_source = h.price_source
            if prod is not None and prod.has_live_data and h.quantity > 0:
                snapshot = _live_price_snapshot(ctx, prod)
                if snapshot is not None:
                    current_price = float(snapshot["price"])
                    current_value = round(h.quantity * current_price, 2)
                    price_time = str(snapshot["as_of"])
                    price_source = str(snapshot["source"])
                    h.market_value = current_value
                    h.last_price = current_price
                    h.price_as_of = price_time
                    h.price_source = price_source
                    h.profit = round(current_value - h.principal, 2)
                    ctx.store.touch(h)
                    live_positions += 1
            total_value += current_value
            total_profit += current_value - h.principal
            quantity_text = (
                f"{h.quantity:,.4f}" if h.quantity and prod and prod.symbol_type == "fund"
                else f"{h.quantity:,.0f}" if h.quantity
                else "-"
            )
            rows.append([
                f"{name} {risk}", quantity_text,
                f"{h.average_cost:,.4f}" if h.average_cost else "-",
                f"{current_price:,.4f}" if current_price else "-",
                f"{current_value:,.2f}",
                f"{current_value - h.principal:+,.2f}",
                (
                    f"{price_source} {price_time}".strip()
                    if price_source or price_time
                    else source_badge(h.source, h.is_live, h.is_demo)
                ),
            ])
        lines = [
            f"持仓总市值 {total_value:,.2f} 元，累计浮动盈亏 {total_profit:+,.2f} 元",
            render_table(
                ["产品", "持仓", "成本价", "最新价", "市值", "浮盈亏", "行情来源/时点"],
                rows,
            ),
        ]
        if live_positions:
            lines.append(
                f"  已按公开行情刷新 {live_positions} 个持仓；"
                "场外基金使用最新已披露净值。"
            )
        return ToolResult(
            True,
            "\n".join(lines),
            data={
                "total_value": round(total_value, 2),
                "total_profit": round(total_profit, 2),
                "live_positions": live_positions,
            },
            summary=f"持仓市值 {total_value:,.2f} 元，浮动盈亏 {total_profit:+,.2f} 元",
        )

