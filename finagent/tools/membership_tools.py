"""Batch membership purchase and next-cycle change tools."""
from __future__ import annotations

import calendar
from datetime import date
from typing import Any, Dict, List

from finagent.data.models import Subscription
from finagent.nlu.membership_parser import (
    apply_membership_options,
    validate_membership_items,
)
from finagent.tools.base import Tool, ToolContext, ToolParam, ToolResult, render_table


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + int(months)
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def cycle_for_term(term_months: int) -> str:
    if term_months == 1:
        return "monthly"
    if term_months == 12:
        return "yearly"
    return "custom"


def _subscription_start(subscription: Subscription | None, today: date) -> date:
    if subscription is None:
        return today
    try:
        current = date.fromisoformat(subscription.expires_at or subscription.next_charge_date)
    except ValueError:
        return today
    return max(current, today)


class MembershipPurchaseTool(Tool):
    """Purchase one or more membership items with one combined debit."""

    name = "membership.purchase"
    category = "订阅管理"
    description = "一次购买多个平台或多个周期的会员，并合并为一笔扣款"
    risk_level = "high"
    requires_confirmation = True
    params = [
        ToolParam("items", "array", True, "会员购买项目列表"),
    ]
    examples = [
        "帮我充腾讯视频3个月60元、Keep12个月199元",
        "给米游社充6个月98元，自动续费，续费价88元",
        "开通B站大会员12个月148元，不自动续费",
        "购买网易云音乐3个月45元，自动续费，续费价同本次价格",
        "给稻壳阅读器充1个月12元、百度网盘6个月150元",
    ]

    def run(self, ctx: ToolContext, items: List[Dict[str, Any]]) -> ToolResult:  # type: ignore[override]
        normalized = apply_membership_options(items, "")
        errors = validate_membership_items(normalized)
        if errors:
            return ToolResult(False, "会员订单未执行：\n" + "\n".join(
                f"  · {error}" for error in errors
            ))

        total = round(sum(float(item["purchase_price"]) for item in normalized), 2)
        account = ctx.store.primary_account(ctx.user_id)
        if account is None:
            return ToolResult(False, "没有找到可用的扣款账户。")
        if account.available < total:
            return ToolResult(
                False,
                f"{account.name}可用余额不足（可用 {account.available:,.2f} 元，"
                f"需要 {total:,.2f} 元）。",
            )

        account.balance = round(account.balance - total, 2)
        ctx.store.touch(account)
        summary = "、".join(
            f"{item['platform']} {item['term_months']}个月"
            for item in normalized
        )
        transaction = ctx.store.add_transaction(
            user_id=ctx.user_id,
            account_id=account.account_id,
            amount=-total,
            merchant="会员充值",
            category="订阅服务",
            channel="APP",
            note=summary,
        )
        order = ctx.store.add_membership_order(
            user_id=ctx.user_id,
            account_id=account.account_id,
            total_amount=total,
            status="completed",
            transaction_id=transaction.txn_id,
        )

        today = ctx.clock.today()
        fulfilled: List[Dict[str, Any]] = []
        failed = 0
        for raw in normalized:
            order_item = ctx.store.add_membership_order_item(
                order_id=order.order_id,
                user_id=ctx.user_id,
                platform=str(raw["platform"]),
                term_months=int(raw["term_months"]),
                purchase_price=float(raw["purchase_price"]),
                auto_renew=bool(raw["auto_renew"]),
                renewal_price=float(raw["renewal_price"]),
                status="pending",
            )
            try:
                matching = [
                    sub for sub in ctx.store.subscriptions_of(ctx.user_id, status="")
                    if sub.merchant == order_item.platform
                ]
                subscription = next(
                    (sub for sub in matching if sub.status == "active"),
                    matching[0] if matching else None,
                )
                start = _subscription_start(subscription, today)
                expiry = add_months(start, order_item.term_months)
                if subscription is None:
                    subscription = Subscription(
                        sub_id=ctx.store.next_id("subscriptions"),
                        user_id=ctx.user_id,
                        merchant=order_item.platform,
                        amount=float(raw["renewal_price"] or raw["purchase_price"]),
                        cycle=cycle_for_term(order_item.term_months),
                        category="会员服务",
                        account_id=account.account_id,
                        next_charge_date=expiry.isoformat(),
                        expires_at=expiry.isoformat(),
                        last_charge_date=today.isoformat(),
                        status="active",
                        auto_renew=order_item.auto_renew,
                        term_months=order_item.term_months,
                        auto_detected=False,
                        source="user:quote",
                    )
                    ctx.store.subscriptions[subscription.sub_id] = subscription
                else:
                    subscription.term_months = order_item.term_months
                    subscription.cycle = cycle_for_term(order_item.term_months)
                    subscription.amount = float(
                        raw["renewal_price"] or raw["purchase_price"]
                    )
                    subscription.auto_renew = order_item.auto_renew
                    subscription.expires_at = expiry.isoformat()
                    subscription.next_charge_date = expiry.isoformat()
                    subscription.last_charge_date = today.isoformat()
                    subscription.status = "active"
                    subscription.pending_term_months = 0
                    subscription.pending_amount = 0.0
                    subscription.pending_auto_renew = False
                    subscription.pending_effective_at = ""
                subscription.account_id = account.account_id
                ctx.store.touch(subscription)
                order_item.status = "fulfilled"
                order_item.subscription_id = subscription.sub_id
                order_item.result = (
                    f"已开通至 {expiry.isoformat()}，"
                    f"{'自动续费' if order_item.auto_renew else '不自动续费'}"
                )
                ctx.store.touch(order_item)
                fulfilled.append({
                    "item_id": order_item.item_id,
                    "platform": subscription.merchant,
                    "term_months": order_item.term_months,
                    "purchase_price": order_item.purchase_price,
                    "auto_renew": order_item.auto_renew,
                    "renewal_price": order_item.renewal_price,
                    "expires_at": expiry.isoformat(),
                    "subscription_id": subscription.sub_id,
                })
            except Exception as exc:
                failed += 1
                order_item.status = "failed"
                order_item.result = str(exc)
                ctx.store.touch(order_item)

        order.status = "partial_failed" if failed else "completed"
        if failed:
            order.failure_reason = f"{failed} 项执行失败"
        ctx.store.touch(order)
        rows = [
            [
                item["platform"],
                f"{item['term_months']} 个月",
                f"{item['purchase_price']:,.2f}",
                "是" if item["auto_renew"] else "否",
                f"{item['renewal_price']:,.2f}" if item["auto_renew"] else "-",
                item["expires_at"],
            ]
            for item in fulfilled
        ]
        lines = [
            f"会员充值已执行，共 {len(fulfilled)} 项，合计 {total:,.2f} 元。",
            render_table(
                ["平台", "周期", "本次价格", "自动续费", "续费价", "到期时间"],
                rows,
            ),
            f"  扣款账户：{account.name}（尾号 {account.tail}）",
            f"  订单号：{order.order_id}  流水号：{transaction.txn_id}",
            "  价格来源：用户报价，未联网核验。",
        ]
        if failed:
            lines.append(f"  有 {failed} 项执行失败，请查看订单明细。")
        return ToolResult(
            not failed,
            "\n".join(lines),
            data={
                "order_id": order.order_id,
                "transaction_id": transaction.txn_id,
                "total": total,
                "items": fulfilled,
                "failed_count": failed,
            },
            summary=f"会员充值 {len(fulfilled)} 项，合计 {total:,.2f} 元",
        )


class SubscriptionChangeCycleTool(Tool):
    """Change the next term of an active subscription without charging now."""

    name = "subscription.change_cycle"
    category = "订阅管理"
    description = "修改已有订阅的下一个续费周期和续费价格"
    risk_level = "medium"
    requires_confirmation = True
    params = [
        ToolParam("merchant", "string", True, "订阅平台名称"),
        ToolParam("term_months", "integer", True, "新的续费周期月数"),
        ToolParam("renewal_price", "number", True, "新的每期续费价格"),
        ToolParam("auto_renew", "boolean", False, "下期是否自动续费", default=True),
    ]
    examples = [
        "把腾讯视频改成12个月续费，续费价288元",
        "Keep下个周期改成6个月，续费价120元",
    ]

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        merchant: str,
        term_months: Any,
        renewal_price: Any,
        auto_renew: bool = True,
    ) -> ToolResult:
        try:
            term = int(term_months)
            price = round(float(renewal_price), 2)
        except (TypeError, ValueError):
            return ToolResult(False, "周期或续费价格无法识别。")
        if term < 1 or term > 36:
            return ToolResult(False, "续费周期必须在 1 到 36 个月之间。")
        if price <= 0:
            return ToolResult(False, "续费价格必须大于 0。")

        target = next(
            (
                subscription
                for subscription in ctx.store.subscriptions_of(ctx.user_id, status="active")
                if merchant and (
                    merchant in subscription.merchant
                    or subscription.merchant in merchant
                )
            ),
            None,
        )
        if target is None:
            return ToolResult(False, f"没有找到可调整周期的「{merchant}」订阅。")
        effective_at = target.expires_at or target.next_charge_date
        target.pending_term_months = term
        target.pending_amount = price
        target.pending_auto_renew = bool(auto_renew)
        target.pending_effective_at = effective_at
        ctx.store.touch(target)
        return ToolResult(
            True,
            "\n".join([
                f"已登记「{target.merchant}」的下期服务计划",
                f"  当前周期：{target.term_months} 个月，续费价 "
                f"{target.amount:,.2f} 元",
                f"  下期周期：{term} 个月，续费价 {price:,.2f} 元",
                f"  生效时间：{effective_at}（当前周期到期后）",
                f"  下期自动续费：{'是' if auto_renew else '否'}",
            ]),
            data={
                "sub_id": target.sub_id,
                "merchant": target.merchant,
                "current_expires_at": effective_at,
                "pending_term_months": term,
                "pending_amount": price,
                "pending_auto_renew": bool(auto_renew),
            },
            summary=f"已登记「{target.merchant}」下期改为 {term} 个月",
        )
