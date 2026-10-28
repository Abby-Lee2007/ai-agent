"""Advanced advisory, risk-playbook and reconciliation tools."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List

from finagent.analytics import (
    build_event_center,
    build_financial_health,
    build_cashflow_forecast,
    estimate_product_analytics,
    portfolio_risk_summary,
)
from finagent.data.card_ledger import (
    card_asset_total,
    linked_account,
    update_card_balance,
    update_credit_usage,
)
from finagent.tools.base import Tool, ToolContext, ToolParam, ToolResult, render_table


class CashflowForecastTool(Tool):
    """Project future liquidity from current balances and recurring events."""

    name = "forecast.cashflow"
    category = "智能财务"
    description = "生成未来30/60/90天现金流数字孪生、资金缺口和情景测算"
    risk_level = "low"
    params = [
        ToolParam("months", "integer", False, "预测月数，1-12", default=3),
        ToolParam("purchase_amount", "number", False, "情景中的一次性计划消费"),
        ToolParam("purchase_date", "string", False, "计划消费日期 YYYY-MM-DD"),
        ToolParam("income_change_pct", "number", False, "收入变化百分比，例如 -20"),
        ToolParam("extra_monthly_expense", "number", False, "每月新增支出"),
        ToolParam("investment_amount", "number", False, "计划转入投资的金额"),
        ToolParam("investment_return_pct", "number", False, "投资情景预期年化收益率"),
    ]
    examples = [
        "预测我未来90天的现金流",
        "如果下个月买8000元电脑会怎样",
        "收入下降20%后我的资金够不够",
    ]

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        months: Any = 3,
        purchase_amount: Any = None,
        purchase_date: str = "",
        income_change_pct: Any = 0,
        extra_monthly_expense: Any = 0,
        investment_amount: Any = 0,
        investment_return_pct: Any = 0,
    ) -> ToolResult:
        scenario_date = None
        if purchase_date:
            try:
                scenario_date = date.fromisoformat(purchase_date)
            except ValueError:
                scenario_date = ctx.clock.today()
        forecast = build_cashflow_forecast(
            ctx.store,
            ctx.providers,
            ctx.clock,
            ctx.user_id,
            months=int(months or 3),
            purchase_amount=float(purchase_amount or 0),
            purchase_date=scenario_date,
            income_change_pct=float(income_change_pct or 0),
            extra_monthly_expense=float(extra_monthly_expense or 0),
            investment_amount=float(investment_amount or 0),
            investment_return_pct=float(investment_return_pct or 0),
        )
        baseline = build_cashflow_forecast(
            ctx.store,
            ctx.providers,
            ctx.clock,
            ctx.user_id,
            months=int(months or 3),
        )
        forecast["baseline_series"] = baseline["series"]
        forecast["baseline_end_balance"] = baseline["end_balance"]
        forecast["baseline_min_balance"] = baseline["min_balance"]
        forecast["scenario_delta_end"] = round(
            forecast["end_balance"] - baseline["end_balance"],
            2,
        )

        lines = [
            f"财务数字孪生（未来 {forecast['horizon_days']} 天）",
            f"  当前可动用资金：{forecast['start_balance']:,.2f} 元",
            f"  预计期末余额：{forecast['end_balance']:,.2f} 元",
            f"  预计最低余额：{forecast['min_balance']:,.2f} 元"
            f"（{forecast['min_balance_date']}）",
            f"  建议保留：{forecast['essential_monthly']:,.2f} 元/月必要支出",
            f"  可安排资金：{forecast['safe_to_spend']:,.2f} 元",
            f"  模型数据：{forecast['data_scope']}；置信度约 {forecast['confidence'] * 100:.0f}%",
        ]
        if forecast["warnings"]:
            lines.append("")
            lines.append("重点预警：")
            for warning in forecast["warnings"]:
                lines.append(f"  · {warning['title']}：{warning['detail']}")
        if forecast["events"]:
            rows = [
                [
                    event["date"],
                    event["label"],
                    f"{event['amount']:,.2f}",
                    event["source"],
                ]
                for event in forecast["events"][:12]
            ]
            lines.append("")
            lines.append("主要未来事件：")
            lines.append(render_table(["日期", "事件", "金额", "依据"], rows))
        lines.append("")
        lines.append(f"建议：{forecast['recommendation']}")
        lines.append(
            "以上是基于演示账户、历史流水和当前订阅/任务的确定性情景测算，"
            "不是实际银行余额承诺。"
        )
        return ToolResult(
            True,
            "\n".join(lines),
            data={"forecast": forecast},
            summary=(
                f"{forecast['horizon_days']}天预测最低余额 "
                f"{forecast['min_balance']:,.2f} 元"
            ),
        )


class ProactiveBriefTool(Tool):
    """Proactive personal-CFO briefing across liquidity and risk."""

    name = "advisor.proactive"
    category = "智能财务"
    description = "主动检查现金流、订阅、账务和风险事件，形成个人CFO待办清单"
    risk_level = "low"
    params = []
    examples = ["主动财务管家", "今天财务上有什么要注意", "帮我做一次财务体检"]

    def run(self, ctx: ToolContext) -> ToolResult:  # type: ignore[override]
        forecast = build_cashflow_forecast(
            ctx.store,
            ctx.providers,
            ctx.clock,
            ctx.user_id,
            months=1,
        )
        today = ctx.clock.today()
        next_14 = today + timedelta(days=14)
        alerts: List[Dict[str, Any]] = []
        for warning in forecast["warnings"]:
            alerts.append({
                "level": warning["level"],
                "title": warning["title"],
                "detail": warning["detail"],
                "suggested_prompt": "预测未来90天现金流",
            })
        for sub in ctx.providers.subscriptions.of(ctx.user_id, "active"):
            charge_date = sub.next_charge_date[:10]
            if charge_date and today.isoformat() <= charge_date <= next_14.isoformat():
                alerts.append({
                    "level": "info",
                    "title": f"{sub.merchant}即将续费",
                    "detail": f"{charge_date} 预计扣款 {sub.amount:,.2f} 元",
                    "suggested_prompt": f"取消{sub.merchant}订阅",
                })
        credit_used = sum(
            card.used for card in ctx.providers.cards.of(ctx.user_id)
            if card.kind == "credit" and card.used > 0
        )
        if credit_used > 0:
            alerts.append({
                "level": "info",
                "title": "信用卡待还金额",
                "detail": f"当前信用卡已用额度合计 {credit_used:,.2f} 元",
                "suggested_prompt": "帮我看看信用卡还款压力",
            })
        rows = ctx.store.transactions_of(
            ctx.user_id,
            (today - timedelta(days=30)).isoformat(),
            today.isoformat(),
        )
        anomalies = ctx.risk.detect_anomalies(ctx.user_id, rows)
        for anomaly in anomalies[:3]:
            alerts.append({
                "level": "danger" if anomaly.level == "danger" else "warning",
                "title": f"可疑交易：{anomaly.merchant}",
                "detail": f"{anomaly.date} {anomaly.amount:,.2f} 元；{anomaly.reason}",
                "suggested_prompt": f"给我{anomaly.merchant}的风险处置方案",
            })
        order = {"danger": 0, "warning": 1, "info": 2}
        alerts.sort(key=lambda item: order.get(item["level"], 3))
        lines = ["个人 CFO 主动简报"]
        if alerts:
            for item in alerts:
                lines.append(f"  · [{item['level']}] {item['title']}：{item['detail']}")
        else:
            lines.append("  今天没有发现需要立即处理的财务事项。")
        lines.append("")
        lines.append(
            f"未来30天预计最低余额 {forecast['min_balance']:,.2f} 元，"
            f"保留必要支出后可安排 {forecast['safe_to_spend']:,.2f} 元。"
        )
        return ToolResult(
            True,
            "\n".join(lines),
            data={"brief": alerts, "forecast": forecast},
            summary=f"发现 {len(alerts)} 项主动提醒",
        )


class FinancialHealthTool(Tool):
    """Explainable 100-point financial health score."""

    name = "advisor.health_score"
    category = "智能财务"
    description = "从流动性、负债、储蓄、投资分散、订阅、安全和目标进度计算财务健康分"
    risk_level = "low"
    params = []
    examples = ["我的财务健康分", "帮我做财务健康评分", "我哪里需要改善"]

    def run(self, ctx: ToolContext) -> ToolResult:  # type: ignore[override]
        health = build_financial_health(
            ctx.store,
            ctx.providers,
            ctx.clock,
            ctx.user_id,
        )
        rows = [
            [
                item["label"],
                f"{item['score']:.0f}",
                f"{item['weight']}%",
                item.get("current", "-"),
                item.get("benchmark", "-"),
                item["band"],
            ]
            for item in health["dimensions"]
        ]
        lines = [
            f"个人财务健康分：{health['score']:.1f}/100 · {health['grade']}",
            f"  模型：{health['methodology']['model']}；"
            f"{health['methodology']['limitations']}",
            render_table(
                ["维度", "得分", "权重", "当前值", "参考标准", "状态"],
                rows,
            ),
            "",
            "优先改善建议：",
        ]
        for index, item in enumerate(health["recommendations"], start=1):
            lines.append(
                f"  {index}. [{item['priority']}优先级] {item['title']}"
                f"（预计可提升约 {item['expected_score_gain']:.1f} 分）"
            )
            lines.append(f"     原因：{item['detail']}")
            lines.append(f"     目标：{item['target']}")
            lines.extend(f"     行动：{step}" for step in item["steps"])
        lines.append("")
        lines.append(
            "评分基于当前演示账户、近180天流水、近3个月现金流预测、持仓、订阅和目标，"
            "用于解释财务结构，不构成投资建议。"
        )
        return ToolResult(
            True,
            "\n".join(lines),
            data={"health": health},
            summary=f"财务健康分 {health['score']:.1f}/100",
        )


class EventCenterTool(Tool):
    """Proactive event inbox with local handle/snooze/ignore state."""

    name = "advisor.events"
    category = "智能财务"
    description = "主动事件中心：现金流、信用卡、订阅、任务和风险提醒"
    risk_level = "low"
    params = [
        ToolParam(
            "action",
            "string",
            False,
            "事件操作：list / handle / snooze / ignore",
            default="list",
            enum=["list", "handle", "snooze", "ignore"],
        ),
        ToolParam("event_id", "string", False, "事件编号"),
    ]
    examples = ["我的主动事件", "查看财务通知", "稍后提醒信用卡事件"]

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        action: str = "list",
        event_id: str = "",
    ) -> ToolResult:
        prefs = dict(ctx.user.preferences or {})
        state = dict(prefs.get("financial_event_state") or {})
        events = build_event_center(
            ctx.store,
            ctx.providers,
            ctx.clock,
            ctx.user_id,
            risk_engine=ctx.risk,
        )
        now = ctx.clock.now()
        visible = []
        for event in events:
            saved = state.get(event["event_id"], {})
            if saved.get("ignored"):
                continue
            snooze_until = saved.get("snooze_until")
            if snooze_until:
                try:
                    if now.isoformat() < snooze_until:
                        event["status"] = "snoozed"
                        event["snooze_until"] = snooze_until
                        continue
                except TypeError:
                    pass
            event["status"] = "handled" if saved.get("handled_at") else "active"
            if saved.get("last_action_at"):
                event["last_action_at"] = saved["last_action_at"]
            visible.append(event)
        action = (action or "list").lower()
        selected = next(
            (item for item in events if item["event_id"] == event_id),
            None,
        )
        if action != "list":
            if selected is None:
                return ToolResult(False, f"没有找到事件「{event_id}」。")
            if action == "ignore":
                state[event_id] = {
                    **state.get(event_id, {}),
                    "ignored": True,
                    "last_action_at": now.isoformat(timespec="seconds"),
                }
                prefs["financial_event_state"] = state
                ctx.user.preferences = prefs
                ctx.store.touch(ctx.user, source="user:event-center")
                ctx.store.log(
                    "advisor.event_ignored",
                    user_id=ctx.user_id,
                    event_id=event_id,
                )
                return ToolResult(
                    True,
                    f"已忽略事件：{selected['title']}",
                    data={"events": visible, "selected": selected},
                    summary="事件已忽略",
                )
            if action == "snooze":
                until = (now + timedelta(days=1)).isoformat(timespec="seconds")
                state[event_id] = {
                    **state.get(event_id, {}),
                    "snooze_until": until,
                    "last_action_at": now.isoformat(timespec="seconds"),
                }
                prefs["financial_event_state"] = state
                ctx.user.preferences = prefs
                ctx.store.touch(ctx.user, source="user:event-center")
                ctx.store.log(
                    "advisor.event_snoozed",
                    user_id=ctx.user_id,
                    event_id=event_id,
                    until=until,
                )
                return ToolResult(
                    True,
                    f"已将「{selected['title']}」稍后提醒至 {until}。",
                    data={"events": visible, "selected": selected},
                    summary="事件已稍后提醒",
                )
            if action == "handle":
                state[event_id] = {
                    **state.get(event_id, {}),
                    "handled_at": now.isoformat(timespec="seconds"),
                    "last_action_at": now.isoformat(timespec="seconds"),
                }
                prefs["financial_event_state"] = state
                ctx.user.preferences = prefs
                ctx.store.touch(ctx.user, source="user:event-center")
                ctx.store.log(
                    "advisor.event_handled",
                    user_id=ctx.user_id,
                    event_id=event_id,
                )
                return ToolResult(
                    True,
                    f"准备处理「{selected['title']}」。可执行：{selected['action_prompt']}",
                    data={"events": visible, "selected": selected},
                    summary="已打开事件处理建议",
                )
        lines = [f"主动事件中心：{len(visible)} 项"]
        for event in visible[:10]:
            lines.append(
                f"  · [{event['level']}] {event['title']}：{event['detail']}"
            )
        if not visible:
            lines.append("  当前没有需要处理的财务事件。")
        return ToolResult(
            True,
            "\n".join(lines),
            data={
                "events": visible,
                "selected": selected,
                "counts": {
                    "total": len(visible),
                    "danger": sum(1 for item in visible if item["level"] == "danger"),
                    "warning": sum(1 for item in visible if item["level"] == "warning"),
                    "info": sum(1 for item in visible if item["level"] == "info"),
                    "snoozed": sum(
                        1 for item in state.values()
                        if item.get("snooze_until", "") > now.isoformat()
                    ),
                    "ignored": sum(
                        1 for item in state.values() if item.get("ignored")
                    ),
                },
            },
            summary=f"发现 {len(visible)} 项主动事件",
        )


class RiskPlaybookTool(Tool):
    """Create a response plan for a suspicious transaction."""

    name = "risk.playbook"
    category = "智能风控"
    description = "把异常交易转换为可执行的风险处置链"
    risk_level = "low"
    params = [ToolParam("case", "string", False, "异常商户、类型或交易描述")]
    examples = ["给我深圳华强北数码城的风险处置方案", "盗刷后应该怎么处理"]

    def run(self, ctx: ToolContext, case: str = "") -> ToolResult:  # type: ignore[override]
        today = ctx.clock.today()
        rows = ctx.store.transactions_of(
            ctx.user_id,
            (today - timedelta(days=30)).isoformat(),
            today.isoformat(),
        )
        anomalies = ctx.risk.detect_anomalies(ctx.user_id, rows)
        key = str(case or "").strip()
        target = next(
            (
                item for item in anomalies
                if key and (
                    key in item.merchant
                    or key in item.kind
                    or key in item.reason
                )
            ),
            anomalies[0] if anomalies else None,
        )
        if target is None:
            return ToolResult(
                True,
                "最近30天没有发现需要启动处置链的异常交易。",
                data={"playbook": [], "anomaly": None},
                summary="未发现异常交易",
            )
        credit_card = next(
            (
                card for card in ctx.providers.cards.of(ctx.user_id)
                if card.kind == "credit"
            ),
            None,
        )
        card_hint = f"尾号{credit_card.tail}信用卡" if credit_card else "相关银行卡"
        steps = [
            {
                "stage": "contain",
                "title": "先止损",
                "detail": f"冻结{card_hint}，暂停后续交易。",
                "command": f"冻结{card_hint}",
                "needs_confirmation": True,
            },
            {
                "stage": "verify",
                "title": "核对交易",
                "detail": f"确认「{target.merchant}」{target.amount:,.2f} 元是否为本人操作。",
                "command": f"搜索消费记录 {target.merchant}",
                "needs_confirmation": False,
            },
            {
                "stage": "dispute",
                "title": "发起争议",
                "detail": "保存交易流水、时间和理由，进入模拟争议交易通道。",
                "command": f"发起争议交易 {target.txn_id}",
                "needs_confirmation": True,
            },
            {
                "stage": "replace",
                "title": "更换卡片",
                "detail": "确认高风险后挂失并补卡，避免继续暴露卡号。",
                "command": f"挂失{card_hint}",
                "needs_confirmation": True,
            },
            {
                "stage": "monitor",
                "title": "持续监控",
                "detail": "未来7天复核同商户、小额试探和境外交易。",
                "command": "帮我看看有没有异常交易",
                "needs_confirmation": False,
            },
        ]
        lines = [
            f"风险事件处置链：{target.merchant}",
            f"  风险等级：{target.level}；风险分：{target.score:.0f}/100",
            f"  涉及金额：{target.amount:,.2f} 元；日期：{target.date}",
            f"  判定依据：{target.reason}",
            "",
            "建议按以下顺序处理：",
        ]
        for index, step in enumerate(steps, start=1):
            suffix = "（需确认）" if step["needs_confirmation"] else ""
            lines.append(f"  {index}. {step['title']}{suffix}：{step['detail']}")
        lines.append("")
        lines.append("系统不会自动执行冻结、挂失或争议提交；每一步仍需要你的明确确认。")
        return ToolResult(
            True,
            "\n".join(lines),
            data={
                "anomaly": {
                    "txn_id": target.txn_id,
                    "merchant": target.merchant,
                    "amount": target.amount,
                    "date": target.date,
                    "level": target.level,
                    "score": target.score,
                    "reason": target.reason,
                },
                "playbook": steps,
            },
            summary=f"已生成「{target.merchant}」5步风险处置方案",
        )


class RiskDisputeTool(Tool):
    """Submit a simulated dispute for one suspicious transaction."""

    name = "risk.dispute"
    category = "智能风控"
    description = "保存可疑交易证据并提交模拟争议申请"
    risk_level = "high"
    requires_confirmation = True
    params = [
        ToolParam("txn_id", "string", True, "需要发起争议的交易流水号"),
    ]
    examples = ["发起争议交易 TXN000001"]

    def run(self, ctx: ToolContext, txn_id: str) -> ToolResult:  # type: ignore[override]
        txn = ctx.store.transactions.get(str(txn_id or "").strip().upper())
        if txn is None or txn.user_id != ctx.user_id:
            return ToolResult(
                False,
                f"没有找到交易流水 {txn_id or '-'}，请从风险处置链重新发起。",
            )

        existing_id = str((txn.metadata or {}).get("dispute_id") or "")
        if existing_id:
            return ToolResult(
                True,
                f"这笔交易已经提交过模拟争议，争议编号：{existing_id}。",
                data={
                    "dispute": {
                        "dispute_id": existing_id,
                        "txn_id": txn.txn_id,
                        "status": "under_review",
                    }
                },
                summary="争议申请已存在",
            )

        alert = ctx.store.add_alert(
            ctx.user_id,
            "warning",
            f"模拟争议待审核：{txn.merchant}",
            (
                f"交易 {txn.txn_id}，{txn.timestamp[:10]}，"
                f"金额 {abs(txn.amount):,.2f} 元。"
                "已保存交易证据并进入银行模拟审核流程。"
            ),
            source="risk.dispute",
        )
        txn.metadata = dict(txn.metadata or {})
        txn.metadata.update({
            "dispute_id": alert.alert_id,
            "dispute_status": "under_review",
            "dispute_submitted_at": ctx.clock.iso(),
        })
        if "争议处理" not in txn.tags:
            txn.tags.append("争议处理")
        ctx.store.touch(txn)
        ctx.store.log(
            "risk.dispute_submitted",
            user_id=ctx.user_id,
            txn_id=txn.txn_id,
            dispute_id=alert.alert_id,
        )
        return ToolResult(
            True,
            "模拟争议申请已提交。\n"
            f"  争议编号：{alert.alert_id}\n"
            f"  交易流水：{txn.txn_id}\n"
            f"  商户：{txn.merchant}\n"
            f"  争议金额：{abs(txn.amount):,.2f} 元\n"
            "  当前状态：银行模拟审核中\n"
            "原型不会向真实银行提交材料，也不会承诺退款或处理时限。",
            data={
                "dispute": {
                    "dispute_id": alert.alert_id,
                    "txn_id": txn.txn_id,
                    "merchant": txn.merchant,
                    "amount": abs(txn.amount),
                    "status": "under_review",
                }
            },
            summary=f"已提交 {txn.merchant} 的模拟争议",
        )


class AccountReconcileTool(Tool):
    """Check card, account and asset-total consistency."""

    name = "account.reconcile"
    category = "账户服务"
    description = "核对银行卡、关联账户与总资产口径，并给出可人工修复的差异"
    risk_level = "low"
    params = []
    examples = ["帮我做账务对账", "核对银行卡和账户余额", "账实一致吗"]

    def run(self, ctx: ToolContext) -> ToolResult:  # type: ignore[override]
        issues: List[str] = []
        issue_details: List[Dict[str, Any]] = []
        rows: List[List[str]] = []
        cards = ctx.providers.cards.of(ctx.user_id)
        for card in cards:
            account = linked_account(ctx.store, card)
            if account is None:
                issue = f"{card.brand}尾号{card.tail}没有关联账户"
                issues.append(issue)
                issue_details.append({
                    "type": "missing_account",
                    "card_id": card.card_id,
                    "card_tail": card.tail,
                    "message": issue,
                    "repair_commands": [],
                })
                account_text = "未关联"
                difference = 0.0
            elif card.kind == "credit":
                difference = round(card.used - account.balance, 2)
                account_text = f"已用 {account.balance:,.2f}"
                if abs(difference) > 0.01:
                    issue = f"信用卡尾号{card.tail}已用额度与账户相差 {difference:,.2f}"
                    issues.append(issue)
                    issue_details.append({
                        "type": "amount_mismatch",
                        "card_id": card.card_id,
                        "card_tail": card.tail,
                        "message": issue,
                        "card_amount": round(card.used, 2),
                        "account_amount": round(account.balance, 2),
                        "difference": difference,
                        "repair_commands": [
                            f"修复账务差异 卡尾号{card.tail} 以银行卡余额为准",
                            f"修复账务差异 卡尾号{card.tail} 以关联账户余额为准",
                        ],
                    })
            else:
                difference = round(card.balance - account.balance, 2)
                account_text = f"余额 {account.balance:,.2f}"
                if abs(difference) > 0.01:
                    issue = f"卡尾号{card.tail}余额与账户相差 {difference:,.2f}"
                    issues.append(issue)
                    issue_details.append({
                        "type": "amount_mismatch",
                        "card_id": card.card_id,
                        "card_tail": card.tail,
                        "message": issue,
                        "card_amount": round(card.balance, 2),
                        "account_amount": round(account.balance, 2),
                        "difference": difference,
                        "repair_commands": [
                            f"修复账务差异 卡尾号{card.tail} 以银行卡余额为准",
                            f"修复账务差异 卡尾号{card.tail} 以关联账户余额为准",
                        ],
                    })
            rows.append([
                f"{card.brand}·{card.kind}",
                card.tail,
                f"{card.balance:,.2f}" if card.kind != "credit" else f"{card.used:,.2f}",
                account_text,
                "一致" if abs(difference) <= 0.01 and account else "需检查",
            ])
        total_assets = card_asset_total(ctx.store, ctx.user_id)
        expected = round(sum(
            card.balance for card in cards
            if card.kind in {"debit", "virtual"} and card.status == "active"
        ), 2)
        if abs(total_assets - expected) > 0.01:
            issue = (
                f"总资产函数 {total_assets:,.2f} 与有效银行卡余额合计 "
                f"{expected:,.2f} 不一致"
            )
            issues.append(issue)
            issue_details.append({
                "type": "asset_total_mismatch",
                "message": issue,
                "repair_commands": [],
            })
        lines = [
            "账务对账（卡片账 vs 银行账户账 vs 总资产口径）",
            "  用途：发现手工改卡、重复导入、旧数据迁移造成的账实差异；"
            "不会修改真实银行流水。",
            render_table(["卡种", "尾号", "卡账金额", "关联账户", "结果"], rows),
            f"  有效借记卡/虚拟卡资产合计：{expected:,.2f} 元",
        ]
        if issues:
            lines.append("")
            lines.append("发现异常：")
            lines.extend(f"  · {issue}" for issue in issues)
            lines.append(
                "  修复建议：仅在确认哪一侧是真实余额后，选择“以银行卡余额为准”"
                "或“以关联账户余额为准”；修复会二次确认并写入审计日志。"
            )
        else:
            lines.append("  卡余额、关联账户和总资产口径一致，未发现账实差异。")
        absolute_difference = round(sum(
            abs(float(item.get("difference") or 0))
            for item in issue_details
        ), 2)
        return ToolResult(
            True,
            "\n".join(lines),
            data={
                "issues": issues,
                "issue_details": issue_details,
                "asset_total": total_assets,
                "expected_asset_total": expected,
                "cards": len(cards),
                "status": "mismatch" if issues else "matched",
                "difference_count": len(issues),
                "absolute_difference": absolute_difference,
                "purpose": (
                    "核对银行卡展示余额、底层关联账户与总资产计算是否一致，"
                    "用于发现手工修改、重复入账和历史数据迁移造成的账实差异。"
                ),
                "manual_fix_supported": any(
                    bool(item.get("repair_commands"))
                    for item in issue_details
                ),
            },
            summary="账务一致" if not issues else f"发现 {len(issues)} 项账务差异",
        )


class ReconcileAdjustTool(Tool):
    """Apply one explicitly confirmed reconciliation correction."""

    name = "account.reconcile_adjust"
    category = "账户服务"
    description = "按用户指定的真实口径手工修复银行卡与关联账户差异"
    risk_level = "high"
    requires_confirmation = True
    params = [
        ToolParam("card", "string", True, "银行卡名称、尾号或备注"),
        ToolParam(
            "source_of_truth",
            "string",
            True,
            "以哪一侧余额为准",
            enum=["card", "account"],
        ),
        ToolParam("reason", "string", False, "人工修正原因"),
    ]
    examples = [
        "修复账务差异 卡尾号6222 以账户余额为准",
        "修复账务差异 工资卡 以银行卡余额为准",
    ]

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        card: str,
        source_of_truth: str,
        reason: str = "",
    ) -> ToolResult:
        target = ctx.providers.cards.find(ctx.user_id, str(card or "").strip())
        if target is None:
            return ToolResult(False, "没有找到需要修正的银行卡。")
        account = linked_account(ctx.store, target)
        if account is None:
            return ToolResult(
                False,
                f"{target.brand}尾号{target.tail}没有关联账户，不能自动修复。",
            )
        source = str(source_of_truth or "").strip().lower()
        if source not in {"card", "account"}:
            return ToolResult(False, "请选择“以银行卡余额为准”或“以关联账户余额为准”。")

        before_card = round(target.used if target.kind == "credit" else target.balance, 2)
        before_account = round(account.balance, 2)
        if source == "account":
            if target.kind == "credit":
                update_credit_usage(ctx.store, target, account.balance)
            else:
                update_card_balance(ctx.store, target, account.balance)
        else:
            account.balance = round(before_card, 2)
            if target.kind == "credit":
                account.credit_limit = target.credit_limit
            ctx.store.touch(account, source="user:reconcile-adjust")

        after_card = round(target.used if target.kind == "credit" else target.balance, 2)
        after_account = round(account.balance, 2)
        ctx.store.log(
            "account.reconcile_adjusted",
            user_id=ctx.user_id,
            card_id=target.card_id,
            account_id=account.account_id,
            source_of_truth=source,
            before={"card": before_card, "account": before_account},
            after={"card": after_card, "account": after_account},
            reason=str(reason or "用户手工对账修正"),
        )
        source_label = "银行卡余额" if source == "card" else "关联账户余额"
        return ToolResult(
            True,
            "账务差异已按人工指定口径修复。\n"
            f"  银行卡：{target.brand}尾号{target.tail}\n"
            f"  采用口径：{source_label}\n"
            f"  修正前：卡账 {before_card:,.2f} 元 / 账户 {before_account:,.2f} 元\n"
            f"  修正后：卡账 {after_card:,.2f} 元 / 账户 {after_account:,.2f} 元\n"
            "  本次修正已写入审计日志；原型不会修改真实银行账务。",
            data={
                "adjustment": {
                    "card_id": target.card_id,
                    "card_tail": target.tail,
                    "source_of_truth": source,
                    "before": {"card": before_card, "account": before_account},
                    "after": {"card": after_card, "account": after_account},
                }
            },
            summary=f"已修正尾号{target.tail}的账务差异",
        )


class WealthAnalyticsTool(Tool):
    """Estimate volatility, drawdown, fee drag and diversification."""

    name = "wealth.analytics"
    category = "理财操作"
    description = "解释产品回撤、波动、费率拖累、流动性和组合相关性"
    risk_level = "low"
    params = [ToolParam("product", "string", False, "产品名称，不填则分析上一批推荐")]
    examples = ["分析这些理财产品的风险", "最大回撤是多少", "帮我做组合风险分析"]

    def run(self, ctx: ToolContext, product: str = "") -> ToolResult:  # type: ignore[override]
        selected = []
        if product:
            item = ctx.providers.wealth.find(product)
            if item:
                selected = [item]
        if not selected:
            selected_ids = [
                str(item.get("product_id"))
                for item in (ctx.session.get("last_wealth_products") or [])
                if item.get("product_id")
            ]
            selected = [
                ctx.providers.wealth.get(product_id)
                for product_id in selected_ids
            ]
            selected = [item for item in selected if item is not None]
        if not selected:
            selected = list(ctx.providers.wealth.products())[:5]
        summary = portfolio_risk_summary(selected)
        rows = []
        for item, metrics in zip(selected, summary["analytics"]):
            rows.append([
                item.name,
                f"R{item.risk_level}",
                f"{metrics['estimated_annual_volatility_pct']:.2f}%",
                f"{metrics['estimated_max_drawdown_pct']:.2f}%",
                f"{metrics['fee_drag_1y_pct']:.2f}%",
                metrics["liquidity"],
            ])
        lines = [
            "理财产品专业风险分析",
            render_table(
                ["产品", "风险", "估算年化波动", "情景最大回撤", "年度费率拖累", "流动性"],
                rows,
            ),
            f"  组合平均估算波动：{summary['average_volatility_pct']:.2f}%",
            f"  组合最坏情景回撤：{summary['worst_drawdown_pct']:.2f}%",
            f"  分散程度：{summary['diversification']}",
        ]
        if len(summary["correlation_matrix"]) > 1:
            lines.append("")
            lines.append("资产类别相关性矩阵：")
            lines.append(render_table(
                [str(value) for value in summary["correlation_matrix"][0]],
                [
                    [str(value) for value in row]
                    for row in summary["correlation_matrix"][1:]
                ],
            ))
        lines.append("")
        lines.append(
            "波动、回撤与相关性为基于风险等级和资产类别的透明情景估计，"
            "不是历史业绩，也不代表未来收益。"
        )
        return ToolResult(
            True,
            "\n".join(lines),
            data={"analytics": summary, "products": [item.product_id for item in selected]},
            summary=f"已分析 {len(selected)} 只产品的风险与相关性",
        )


class AAPaymentStatusTool(Tool):
    """Show AA collection progress in the local prototype."""

    name = "transfer.aa_status"
    category = "智能转账"
    description = "查看AA收款单的参与人、待收金额和模拟分享文案"
    risk_level = "low"
    params = []
    examples = ["AA收款进度", "谁还没付款", "查看AA状态"]

    def run(self, ctx: ToolContext) -> ToolResult:  # type: ignore[override]
        sheets = [
            sheet for sheet in ctx.store.aa_sheets.values()
            if sheet.user_id == ctx.user_id
        ]
        if not sheets:
            return ToolResult(True, "当前没有AA收款单。", data={"sheets": []})
        rows = []
        payload = []
        for sheet in sorted(sheets, key=lambda item: item.created_at, reverse=True):
            pending = [
                person for person in sheet.participants
                if person.get("status") == "pending"
            ]
            share_text = (
                f"【AA收款】{sheet.title} 共{sheet.total_amount:,.2f}元，"
                f"每人{sheet.participants[0].get('amount', 0):,.2f}元。"
                "这是赛事模拟收款邀请，不产生真实扣款。"
            )
            rows.append([
                sheet.sheet_id,
                sheet.title,
                f"{sheet.total_amount:,.2f}",
                f"{len(pending)}人待付",
                sheet.status,
            ])
            payload.append({
                "sheet_id": sheet.sheet_id,
                "title": sheet.title,
                "total_amount": sheet.total_amount,
                "status": sheet.status,
                "participants": sheet.participants,
                "share_text": share_text,
            })
        return ToolResult(
            True,
            "AA收款进度\n" + render_table(
                ["单号", "名称", "总额", "进度", "状态"],
                rows,
            ),
            data={"sheets": payload},
            summary=f"共有 {len(payload)} 张AA收款单",
        )
