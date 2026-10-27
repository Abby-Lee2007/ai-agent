"""订阅 / 代扣管理工具集。

覆盖赛题要求：自动识别订阅扣费、续费提醒、一键取消订阅。
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List

from finagent.data.models import Subscription
from finagent.data.provenance import source_badge
from finagent.tools.base import Tool, ToolContext, ToolParam, ToolResult, render_table


def _transactions(ctx: ToolContext):
    if getattr(ctx, "providers", None) is not None:
        return ctx.providers.transactions.of(ctx.user_id)
    return ctx.store.transactions_of(ctx.user_id)


def _subscriptions(ctx: ToolContext, status: str = "active"):
    if getattr(ctx, "providers", None) is not None:
        return ctx.providers.subscriptions.of(ctx.user_id, status)
    return ctx.store.subscriptions_of(ctx.user_id, status or None)


def _roll_subscription_expiry(value: date, cycle: str, today: date) -> date:
    """Move an expired subscription forward to its next valid billing date."""
    candidate = value
    while candidate < today:
        if cycle == "weekly":
            candidate += timedelta(days=7)
        elif cycle == "yearly":
            year = candidate.year + 1
            month = candidate.month
            day = min(candidate.day, calendar.monthrange(year, month)[1])
            candidate = date(year, month, day)
        else:
            month_index = candidate.month
            year = candidate.year + (month_index // 12)
            month = month_index % 12 + 1
            day = min(candidate.day, calendar.monthrange(year, month)[1])
            candidate = date(year, month, day)
    return candidate


def detect_subscriptions(ctx: ToolContext) -> List[Dict[str, Any]]:
    """从流水里自动识别周期性扣费。

    判定逻辑（原型版）：
    1. 同一商户出现 >= 4 次支出；
    2. 相邻两次间隔接近一个月（24-36 天）；
    3. 历史金额稳定（订阅是固定价，日常消费金额会剧烈波动）；
    4. 排除房租、加油、话费这类天然按月发生的非订阅支出。
    涨价单独识别，所以只要求"除最近一次外"金额稳定。
    """
    txns = _transactions(ctx)
    grouped: Dict[str, List] = defaultdict(list)
    exclude_categories = {"转账", "还款", "住房", "理财", "收入"}
    for t in txns:
        if t.amount >= 0 or t.category in exclude_categories:
            continue
        grouped[t.merchant].append(t)

    results: List[Dict[str, Any]] = []
    for merchant, rows in grouped.items():
        rows = sorted(rows, key=lambda t: t.timestamp)
        if len(rows) < 4:
            continue
        gaps: List[int] = []
        for prev, cur in zip(rows, rows[1:]):
            try:
                delta = (datetime.fromisoformat(cur.timestamp) - datetime.fromisoformat(prev.timestamp)).days
            except ValueError:
                continue
            gaps.append(delta)
        if not gaps or any(g < 24 or g > 36 for g in gaps):
            continue
        amounts = [abs(t.amount) for t in rows]
        history = amounts[:-1] or amounts
        if min(history) <= 0 or max(history) / min(history) > 1.10:
            continue
        base = max(history)
        latest = amounts[-1]
        hiked = latest > base * 1.2
        results.append(
            {
                "merchant": merchant,
                "amount": latest,
                "occurrences": len(rows),
                "last_date": rows[-1].timestamp[:10],
                "interval_days": round(sum(gaps) / len(gaps), 1),
                "hiked": hiked,
                "previous_amount": base,
                "category": rows[-1].category,
            }
        )
    return sorted(results, key=lambda r: -r["amount"])


class SubscriptionDetectTool(Tool):
    """自动识别订阅。"""

    name = "subscription.detect"
    category = "订阅管理"
    description = "扫描历史流水，自动识别出所有周期性扣费的订阅服务"
    risk_level = "low"
    params = []
    examples = ["帮我看看有哪些自动续费", "哪些服务在偷偷扣钱"]

    def run(self, ctx: ToolContext) -> ToolResult:  # type: ignore[override]
        detected = detect_subscriptions(ctx)
        if not detected:
            return ToolResult(True, "没有从流水里识别到周期性扣费。", summary="未识别到订阅")
        existing = {s.merchant: s for s in _subscriptions(ctx, status="")}
        rows = []
        monthly_total = 0.0
        auto_renew_count = 0
        for item in detected:
            subscription = existing.get(item["merchant"])
            if subscription is not None:
                item["expires_at"] = subscription.expires_at or subscription.next_charge_date
                item["auto_renew"] = bool(
                    subscription.auto_renew and subscription.status == "active"
                )
            else:
                item["expires_at"] = (
                    date.fromisoformat(item["last_date"]) + timedelta(days=30)
                ).isoformat()
                item["auto_renew"] = True
            if item["auto_renew"]:
                monthly_total += item["amount"]
                auto_renew_count += 1
            amount_text = f"{item['amount']:,.2f}"
            if item["hiked"]:
                amount_text += f" ↑（原 {item['previous_amount']:,.2f}）"
            rows.append([item["merchant"], item["category"], amount_text,
                         item["expires_at"], "是" if item["auto_renew"] else "否",
                         f"{item['interval_days']:.0f} 天", item["last_date"],
                         str(item["occurrences"])])
        lines = [
            f"共识别到 {len(detected)} 项订阅，其中 {auto_renew_count} 项目前自动续费，"
            f"合计每月约 {monthly_total:,.2f} 元"
            f"（一年约 {monthly_total * 12:,.2f} 元）：",
            render_table(
                ["商户", "类别", "最近扣费", "到期时间", "自动续费", "周期", "最近日期", "次数"],
                rows,
            ),
            "",
            "  ↑ 表示该订阅近期涨价，建议确认是否继续使用。",
        ]
        hiked = [d for d in detected if d["hiked"]]
        if hiked:
            lines.append(f"  其中有 {len(hiked)} 项涨价：" + "、".join(d["merchant"] for d in hiked))

        # 把识别结果同步到订阅表，保证后续"取消订阅"能按商户找到记录
        for item in detected:
            if item["merchant"] in existing:
                continue
            next_charge_date = (date.fromisoformat(item["last_date"]) + timedelta(days=30)).isoformat()
            sub = Subscription(
                sub_id=ctx.store.next_id("subscriptions"),
                user_id=ctx.user_id,
                merchant=item["merchant"],
                amount=item["amount"],
                category=item["category"],
                last_charge_date=item["last_date"],
                next_charge_date=next_charge_date,
                expires_at=next_charge_date,
                auto_renew=True,
                auto_detected=True,
            )
            ctx.store.touch(sub)
            ctx.store.subscriptions[sub.sub_id] = sub
            existing[sub.merchant] = sub
        return ToolResult(
            True,
            "\n".join(lines),
            data={
                "count": len(detected),
                "auto_renew_count": auto_renew_count,
                "monthly_total": round(monthly_total, 2),
                "items": detected,
            },
            summary=(
                f"识别到 {len(detected)} 项订阅，其中 {auto_renew_count} 项自动续费，"
                f"每月约 {monthly_total:,.2f} 元"
            ),
        )


class SubscriptionListTool(Tool):
    """订阅清单。"""

    name = "subscription.list"
    category = "订阅管理"
    description = "列出所有订阅服务、到期时间、自动续费状态与月度合计"
    risk_level = "low"
    params = []
    examples = ["我的订阅都有什么", "每个月自动扣多少钱"]

    def run(self, ctx: ToolContext) -> ToolResult:  # type: ignore[override]
        subs = _subscriptions(ctx, status="")
        if not subs:
            return ToolResult(True, "您当前没有任何订阅记录。", summary="无订阅")
        rows = []
        for s in subs:
            expires_at = s.expires_at or s.next_charge_date
            auto_renew = "是" if s.auto_renew and s.status == "active" else "否"
            status = {
                "active": "生效中",
                "cancelled": "已取消",
                "paused": "已暂停",
                "trial": "试用中",
            }.get(s.status, s.status)
            if (
                s.status == "active"
                and s.price_history
                and s.amount > max(s.price_history[:-1] or [s.amount]) * 1.2
            ):
                status += " · 涨价提醒"
            rows.append([
                s.merchant,
                s.category,
                f"{s.amount:,.2f}",
                expires_at,
                auto_renew,
                status,
                source_badge(s.source, s.is_live, s.is_demo),
            ])
        monthly = ctx.store.monthly_subscription_total(ctx.user_id)
        auto_renew_count = sum(1 for s in subs if s.auto_renew and s.status == "active")
        cancelled_count = sum(1 for s in subs if s.status == "cancelled")
        lines = [
            f"订阅共 {len(subs)} 项（自动续费 {auto_renew_count} 项，已取消 {cancelled_count} 项），"
            f"月度自动续费合计约 {monthly:,.2f} 元：",
            render_table(["商户", "类别", "金额", "到期时间", "自动续费", "状态", "来源"], rows),
        ]
        return ToolResult(
            True,
            "\n".join(lines),
            data={
                "count": len(subs),
                "monthly_total": monthly,
                "items": [
                    {
                        "sub_id": s.sub_id,
                        "merchant": s.merchant,
                        "category": s.category,
                        "amount": s.amount,
                        "cycle": s.cycle,
                        "next_charge_date": s.next_charge_date,
                        "expires_at": s.expires_at or s.next_charge_date,
                        "last_charge_date": s.last_charge_date,
                        "auto_renew": bool(s.auto_renew and s.status == "active"),
                        "status": s.status,
                        "hiked": bool(
                            s.price_history
                            and s.amount > max(s.price_history[:-1] or [s.amount]) * 1.2
                        ),
                    }
                    for s in subs
                ],
            },
            summary=f"{len(subs)} 项订阅，每月 {monthly:,.2f} 元",
        )


class SubscriptionCancelTool(Tool):
    """一键取消订阅。"""

    name = "subscription.cancel"
    category = "订阅管理"
    description = "取消指定订阅的自动续费，避免继续扣款"
    risk_level = "high"
    requires_confirmation = True
    params = [
        ToolParam("merchant", "string", True, "订阅商户名称，如 腾讯视频VIP"),
    ]
    examples = ["取消腾讯视频会员", "把iCloud订阅取消掉"]

    def run(self, ctx: ToolContext, merchant: str) -> ToolResult:  # type: ignore[override]
        subs = _subscriptions(ctx)
        target = None
        for s in subs:
            if merchant and (merchant in s.merchant or s.merchant in merchant):
                target = s
                break
        if target is None:
            names = "、".join(s.merchant for s in subs[:6]) or "暂无"
            return ToolResult(False, f"没有找到「{merchant}」的订阅。当前有：{names}")

        target.status = "cancelled"
        target.auto_renew = False
        target.expires_at = target.expires_at or target.next_charge_date
        target.pending_term_months = 0
        target.pending_amount = 0.0
        target.pending_auto_renew = False
        target.pending_effective_at = ""
        ctx.store.touch(target)
        for task in ctx.store.tasks_of(ctx.user_id, status="pending"):
            if task.kind == "reminder" and task.payload.get("sub_id") == target.sub_id:
                task.status = "cancelled"
                task.executed_at = ctx.clock.iso()
                task.result = "订阅已取消，续费提醒作废"
                ctx.store.touch(task)
        cancel_task = ctx.store.add_task(
            user_id=ctx.user_id,
            kind="subscription_cancel",
            run_date=ctx.clock.date_str(),
            title=f"已取消 {target.merchant} 自动续费",
            payload={"sub_id": target.sub_id, "merchant": target.merchant},
        )
        cancel_task.status = "done"
        cancel_task.executed_at = ctx.clock.iso()
        cancel_task.result = "自动续费已关闭"
        ctx.store.touch(cancel_task)
        saved = target.amount * (12 if target.cycle == "monthly" else 1)
        lines = [
            f"已取消「{target.merchant}」的自动续费",
            f"  取消后不会再扣费，本年度共可节省约 {saved:,.2f} 元",
            f"  当前订阅周期内仍可使用至 {target.next_charge_date} 到期。",
        ]
        return ToolResult(
            True,
            "\n".join(lines),
            data={"sub_id": target.sub_id, "merchant": target.merchant, "saved": round(saved, 2)},
            summary=f"已取消「{target.merchant}」订阅，年省约 {saved:,.2f} 元",
        )


class SubscriptionResumeTool(Tool):
    """恢复已取消订阅的自动续费。"""

    name = "subscription.resume"
    category = "订阅管理"
    description = "恢复指定订阅的自动续费"
    risk_level = "high"
    requires_confirmation = True
    params = [
        ToolParam("merchant", "string", True, "已取消的订阅商户名称"),
    ]
    examples = ["恢复腾讯视频VIP自动续费", "重新开通Keep会员"]

    def run(self, ctx: ToolContext, merchant: str) -> ToolResult:  # type: ignore[override]
        subs = _subscriptions(ctx, status="")
        target = None
        for subscription in subs:
            if merchant and (
                merchant in subscription.merchant
                or subscription.merchant in merchant
            ):
                target = subscription
                break
        if target is None:
            names = "、".join(s.merchant for s in subs if s.status == "cancelled") or "暂无"
            return ToolResult(False, f"没有找到「{merchant}」的已取消订阅。可恢复：{names}")
        if target.status == "active" and target.auto_renew:
            return ToolResult(
                True,
                f"「{target.merchant}」已经在自动续费中，无需重复恢复。",
                data={
                    "sub_id": target.sub_id,
                    "merchant": target.merchant,
                    "expires_at": target.expires_at or target.next_charge_date,
                    "auto_renew": True,
                },
                summary=f"「{target.merchant}」已处于自动续费状态",
            )

        today = ctx.clock.today()
        current_expiry = target.expires_at or target.next_charge_date
        try:
            expiry = date.fromisoformat(current_expiry)
        except ValueError:
            expiry = today
        next_expiry = _roll_subscription_expiry(expiry, target.cycle, today)

        target.status = "active"
        target.auto_renew = True
        target.expires_at = next_expiry.isoformat()
        target.next_charge_date = next_expiry.isoformat()
        ctx.store.touch(target)

        message = "\n".join([
            f"已恢复「{target.merchant}」的自动续费",
            f"  每期金额：{target.amount:,.2f} 元",
            f"  当前周期到期：{target.expires_at}",
            "  后续将按订阅周期继续自动扣款。",
        ])
        return ToolResult(
            True,
            message,
            data={
                "sub_id": target.sub_id,
                "merchant": target.merchant,
                "expires_at": target.expires_at,
                "auto_renew": True,
            },
            summary=f"已恢复「{target.merchant}」自动续费",
        )


class SubscriptionRemindTool(Tool):
    """续费提醒。"""

    name = "subscription.remind"
    category = "订阅管理"
    description = "查看近期即将扣费的订阅并生成续费提醒"
    risk_level = "low"
    params = [
        ToolParam("days", "integer", False, "提前提醒的天数", default=7),
    ]
    examples = ["最近有哪些订阅要扣费", "帮我设置续费提醒"]

    def run(self, ctx: ToolContext, days: Any = 7) -> ToolResult:  # type: ignore[override]
        try:
            window = int(days)
        except (TypeError, ValueError):
            window = 7
        today = ctx.clock.today()
        upcoming = []
        for s in _subscriptions(ctx):
            if not s.auto_renew:
                continue
            try:
                delta = (date.fromisoformat(s.next_charge_date) - today).days
            except ValueError:
                continue
            if 0 <= delta <= window:
                upcoming.append((s, delta))
        if not upcoming:
            return ToolResult(
                True,
                f"未来 {window} 天内没有需要扣费的订阅。",
                summary=f"{window} 天内无待扣费订阅",
            )
        rows = []
        total = 0.0
        for s, delta in sorted(upcoming, key=lambda x: x[1]):
            total += s.amount
            rows.append([s.next_charge_date, s.merchant, f"{s.amount:,.2f}", f"{delta} 天后"])
            ctx.store.add_task(
                user_id=ctx.user_id,
                kind="reminder",
                run_date=(today + timedelta(days=max(delta - 1, 0))).isoformat(),
                title=f"续费提醒：{s.merchant} 将于 {s.next_charge_date} 扣费 {s.amount:,.2f} 元",
                payload={"sub_id": s.sub_id},
            )
        lines = [
            f"未来 {window} 天内有 {len(upcoming)} 项订阅即将扣费，合计 {total:,.2f} 元：",
            render_table(["扣费日期", "商户", "金额", "倒计时"], rows),
            "  已为每项订阅生成提前 1 天的提醒。如不需要，可以直接说「取消XX订阅」。",
        ]
        return ToolResult(
            True,
            "\n".join(lines),
            data={"count": len(upcoming), "total": round(total, 2)},
            summary=f"{len(upcoming)} 项订阅即将扣费，合计 {total:,.2f} 元",
        )

