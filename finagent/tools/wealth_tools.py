"""理财工具集。

覆盖赛题要求：
- 产品推荐与对比；
- 风险评估（含投资者适当性管理）；
- 一键申购 / 赎回。
"""
from __future__ import annotations

from typing import Any, Dict, List

from finagent.data.card_ledger import (
    card_asset_total,
    sync_cards_for_account,
    update_card_balance,
    verify_card_password,
)
from finagent.data.models import Holding
from finagent.data.provenance import source_badge
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
                  enum=["stock", "bond", "fund", "derivative", "deposit"]),
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
        requested = str(asset_class).lower().strip()
        wants_stock = requested in {"stock", "equity"}
        wants_bond = requested == "bond"
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
        investable = max(0.0, round(liquid - emergency_need, 2))
        affordable_budget = min(
            invest if invest > 0 else liquid,
            liquid,
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
        elif wants_bond:
            max_risk = min(user_level + 1, 5)
            pool = [p for p in _products(ctx)
                    if p.asset_class == "bond" or p.category == "债券基金"]
            lines.append("债券类产品（国债 / 可转债 / 债券基金）：")
            lines.append("  国债以持有到期吃票息为主；可转债有股性，波动明显大于纯债。")
        elif wants_fund:
            max_risk = min(user_level + 1, 5)
            pool = [p for p in _products(ctx)
                    if p.asset_class == "fund" and p.risk_level <= max_risk]
            lines.append(f"按你的风险等级「{assessment['label']}」，可选的基金如下：")
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
            max_risk = min(5, max_risk + 1)
            pool = [p for p in _products_by_risk(ctx, max_risk)
                    if p.min_amount <= (invest or p.min_amount)]
            if not pool:
                pool = _products_by_risk(ctx, max_risk)
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
                    "products": [],
                    "display_count": 0,
                    "filter_options": ["存款", "债券", "基金"],
                },
                summary="当前资金不足，未展示买不起的产品",
            )
        pool = affordable
        filter_key = requested or "mixed"
        previous_filter = str(ctx.session.get("wealth_recommend_filter") or "")
        offset = int(ctx.session.get("wealth_recommend_offset") or 0)
        if previous_filter != filter_key:
            offset = 0
        if offset >= len(pool):
            offset = 0
        rotated = pool[offset:] + pool[:offset]
        pool = rotated[:5]
        if not pool:
            return ToolResult(False, "当前没有匹配您风险等级的产品。")
        ctx.session["wealth_recommend_filter"] = filter_key
        ctx.session["wealth_recommend_offset"] = (offset + len(pool)) % max(len(affordable), 1)
        lines.append(
            f"根据您的风险等级「{assessment['label']}」，本次共展示 {len(pool)} 只产品。"
        )

        # ---------------- 取真实行情 / 净值 ----------------
        stock_symbols = [p.symbol for p in pool if p.symbol_type == "stock"]
        fund_codes = [p.symbol for p in pool if p.symbol_type == "fund"]
        quotes = market.quotes(stock_symbols) if stock_symbols else {}
        navs = {}
        for code in fund_codes:
            nav = market.fund_nav(code)
            if nav is not None:
                navs[code] = nav

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
            flow_text = (
                f"起投 {product.min_amount:,.0f} 元；"
                f"{product.redemption_rule or product.liquidity}"
            )
            risk_text = f"R{product.risk_level}"
            if product.risk_level > user_level:
                risk_text += " 需确认"
            combined_rows.append([
                str(index),
                f"{product.name} {product.symbol.upper() if product.symbol else product.product_id}",
                f"{product.category} / {risk_text}",
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
                "risk_level": product.risk_level,
                "risk_label": risk_text,
                "requires_risk_confirmation": product.risk_level > user_level,
                "metric": metric,
                "change": movement,
                "liquidity": product.redemption_rule or product.liquidity,
                "min_amount": product.min_amount,
                "fee_rate": product.fee_rate,
                "data_source": data_label,
                "description": self._plain_product_description(product),
            })

        lines.append("")
        lines.append("统一产品清单：")
        lines.append(render_table(
            ["#", "产品/代码", "类型/风险", "价格或收益参考", "起投与流动性", "数据"],
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
        if invest > 0 and allow_allocation:
            if wants_stock and quotes:
                lines.extend(self._live_plan(pool, quotes, invest))
            elif any(p.expected_return > 0 for p in pool):
                plan_pool = [p for p in pool if p.risk_level <= user_level] or pool
                plan_rows, expected_return = self._build_plan(
                    plan_pool, invest, horizon, wants_stock
                )
                if plan_rows:
                    lines.append("")
                    lines.append(f"组合配置建议（投入 {invest:,.2f} 元）：")
                    lines.append(render_table(["产品", "比例", "金额", "收益测算"], plan_rows))
                    lines.append(f"  组合测算年化约 {expected_return * 100:.2f}%，"
                                 f"收益约 {invest * expected_return:,.2f} 元（演示参数）")

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
            lines.append("这笔生活费不足应急金，建议暂不购买；如需演示交易，可先补充资金。")

        class_labels = {"stock": "A股股票", "bond": "债券", "fund": "基金",
                        "derivative": "衍生品", "deposit": "存款"}
        class_label = class_labels.get(requested, "理财")
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
                "investable": investable,
                "affordable_budget": round(affordable_budget, 2),
                "display_count": len(pool),
                "total_candidates": len(affordable),
                "live_count": live_count,
                "demo_count": demo_count,
                "product_details": product_details,
                "purchase_prompt": allow_allocation,
                "has_more": len(affordable) > len(pool),
                "filter_options": ["存款", "债券", "基金", "换一批"],
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
    def _live_plan(pool, quotes: Dict[str, Any], invest: float) -> List[str]:
        """按真实价格估算能买多少股（A 股按 100 股整数倍）。"""
        available = [p for p in pool if p.symbol in quotes]
        if not available:
            return []
        chosen = available[:2]
        weights = [0.6, 0.4] if len(chosen) == 2 else [1.0]
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

        target = _find_product(ctx, product)
        if target is None:
            return ToolResult(False, f"没有找到产品「{product}」，可以说「推荐理财产品」看看可选清单。")
        if value < target.min_amount:
            return ToolResult(False, f"「{target.name}」起投金额为 {target.min_amount:,.2f} 元。")

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
        asset_total_after = card_asset_total(ctx.store, user.user_id)
        # 同一产品已有持仓时直接累加，避免持仓列表里出现一堆零散的同名份额
        holding = next(
            (h for h in _holdings(ctx, status="holding")
             if h.product_id == target.product_id),
            None,
        )
        if holding is None:
            holding = Holding(
                holding_id=store.next_id("holdings"),
                user_id=user.user_id,
                product_id=target.product_id,
                principal=value,
                market_value=value,
                purchase_date=ctx.clock.iso(),
            )
            ctx.store.touch(holding)
            store.holdings[holding.holding_id] = holding
        else:
            holding.principal = round(holding.principal + value, 2)
            holding.market_value = round(holding.market_value + value, 2)
            ctx.store.touch(holding)

        market = ctx.session.get("market")
        quote = None
        if target.has_live_data and market is not None:
            quote = market.quote(target.symbol)
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
        if quote is not None:
            shares = int(value // (quote.price * 100)) * 100 if quote.price > 0 else 0
            lines = [
                f"申购成功：「{target.name}」（{quote.symbol.upper()}）{value:,.2f} 元",
                f"  成交价参考：{quote.price:,.3f} 元（腾讯财经实时行情，{quote.timestamp}）",
                f"  约可买 {shares:,} 股（按 100 股整数倍估算，实际以券商成交为准）",
                f"  产品风险等级：R{target.risk_level}",
                f"  付款银行卡：{payer.brand}（尾号 {payer.tail}）  剩余可用 {payer.available:,.2f} 元",
                f"  银行卡资产总额：{asset_total_after:,.2f} 元",
                f"  持仓编号：{holding.holding_id}   流水号：{txn.txn_id}",
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
                  "risk_level": target.risk_level, "payment_card_id": payer.card_id,
                  "payment_card_balance_after": payer.balance,
                  "asset_total_after": asset_total_after, "txn_id": txn.txn_id},
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
                store.add_transaction(
                    user_id=user.user_id,
                    account_id=account.account_id,
                    amount=value,
                    merchant=name,
                    category="理财",
                    channel="APP",
                    note="理财赎回",
                )
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
            data={"total": round(total_redeemed, 2)},
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
        for h in holdings:
            prod = ctx.providers.wealth.get(h.product_id) if getattr(ctx, "providers", None) else ctx.store.products.get(h.product_id)
            name = prod.name if prod else h.product_id
            risk = f"R{prod.risk_level}" if prod else "-"
            total_value += h.market_value
            total_profit += h.market_value - h.principal
            rows.append([
                name, risk,
                f"{h.principal:,.2f}", f"{h.market_value:,.2f}",
                f"{h.market_value - h.principal:+,.2f}",
                f"{(h.market_value - h.principal) / h.principal * 100:+.2f}%" if h.principal else "-",
                source_badge(h.source, h.is_live, h.is_demo),
            ])
        lines = [
            f"持仓总市值 {total_value:,.2f} 元，累计浮动盈亏 {total_profit:+,.2f} 元",
            render_table(["产品", "风险", "本金", "市值", "盈亏", "收益率", "来源"], rows),
        ]
        return ToolResult(
            True,
            "\n".join(lines),
            data={"total_value": round(total_value, 2), "total_profit": round(total_profit, 2)},
            summary=f"持仓市值 {total_value:,.2f} 元，浮动盈亏 {total_profit:+,.2f} 元",
        )

