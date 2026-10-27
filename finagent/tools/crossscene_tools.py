"""跨场景联动工具集。

这是最能体现"Agent 自主规划"的部分，覆盖赛题示例场景：
    检测我爱人生日 -> 当月锁定 1000 元活期 -> 生日前 2 天订购鲜花、蛋糕

拆成了几个可复用的原子工具：
    goal.create                锁定资金、建立目标
    crossscene.schedule_gift   预约某天为某人下单某件礼物
    crossscene.daily_brief     主动汇总"今天需要关注什么"
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, List, Optional, Tuple

from finagent.data.models import User
from finagent.data.providers import MerchantOfferProvider
from finagent.tools.base import Tool, ToolContext, ToolParam, ToolResult, format_money, render_table
from finagent.tools.transfer_tools import resolve_account
from finagent.utils import parse_date


def lookup_person(ctx: ToolContext, name: str) -> Tuple[Optional[User], Optional[date]]:
    """按称呼找到"人"以及 ta 的生日。

    支持两种线索：
    1. 称呼命中客户画像里的关系（如"老婆" -> preferences.spouse）；
    2. 直接按姓名匹配通讯录 / 模拟用户表。
    """
    store = ctx.store
    user = ctx.user
    prefs = user.preferences or {}
    keyword = (name or "").strip()

    relation_keys = {
        "老婆": "spouse", "妻子": "spouse", "爱人": "spouse", "老公": "spouse", "丈夫": "spouse",
        "妈妈": "mother", "母亲": "mother", "爸爸": "father", "父亲": "father",
    }
    key = relation_keys.get(keyword)
    resolved_name = prefs.get(key, keyword) if key else keyword

    birthday_raw = ""
    relation_groups = {
        "spouse": {"老婆", "妻子", "爱人", "配偶", "丈夫", "老公"},
        "mother": {"妈妈", "母亲", "妈"},
        "father": {"爸爸", "父亲", "爸"},
    }
    wanted_relations = relation_groups.get(key or "", set())
    for item in prefs.get("care_people") or []:
        if not isinstance(item, dict):
            continue
        item_name = str(item.get("name") or "").strip()
        relation = str(item.get("relation") or "").strip()
        if (
            item_name == keyword
            or (item_name and item_name in keyword)
            or (relation and relation in keyword)
            or bool(wanted_relations & {relation})
        ):
            resolved_name = item_name or resolved_name
            birthday_raw = str(item.get("birthday") or "")
            key = "care"
            break

    if not birthday_raw and key == "spouse" and prefs.get("spouse_birthday"):
        birthday_raw = prefs["spouse_birthday"]

    person: Optional[User] = None
    for candidate in store.users.values():
        if candidate.name in {resolved_name, keyword}:
            person = candidate
            break
    if person is None:
        payee = (
            ctx.providers.payees.find(user.user_id, resolved_name)
            if getattr(ctx, "providers", None) is not None
            else store.find_payee(user.user_id, resolved_name)
        )
        if payee:
            resolved_name = payee.name
            for candidate in store.users.values():
                if candidate.name == resolved_name:
                    person = candidate
                    break
    if person is None and resolved_name:
        # The user can edit the spouse's display name without creating a full
        # customer record. Keep the profile-provided name in the conversation.
        person = User(
            user_id=f"profile:{key or 'contact'}",
            name=resolved_name,
            phone="",
            city=user.city,
            birthday=birthday_raw,
            source="user:profile",
            is_demo=False,
        )

    if not birthday_raw and person and person.birthday:
        birthday_raw = person.birthday

    birthday: Optional[date] = None
    if birthday_raw:
        try:
            parsed = date.fromisoformat(birthday_raw)
        except ValueError:
            parsed = None
        if parsed:
            today = ctx.clock.today()
            month, day = parsed.month, parsed.day
            try:
                upcoming = date(today.year, month, day)
            except ValueError:
                upcoming = date(today.year, 3, 1)
            if upcoming < today:
                try:
                    upcoming = date(today.year + 1, month, day)
                except ValueError:
                    upcoming = date(today.year + 1, 3, 1)
            birthday = upcoming
    return person, birthday


class GoalCreateTool(Tool):
    """创建资金目标并锁定活期资金。"""

    name = "goal.create"
    category = "跨场景联动"
    description = "创建一个资金目标（如生日惊喜金、旅行基金），并从活期账户锁定相应金额"
    risk_level = "medium"
    requires_confirmation = True
    params = [
        ToolParam("name", "string", True, "目标名称"),
        ToolParam("target_amount", "number", True, "锁定/目标金额（元）"),
        ToolParam("deadline", "string", False, "目标日期，如 2026-11-20"),
        ToolParam("scenario", "string", False, "场景标识，如 birthday / travel"),
        ToolParam("note", "string", False, "备注"),
    ]
    examples = ["锁定1000块做生日基金", "建一个5000元的旅行基金"]

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        name: str,
        target_amount: Any,
        deadline: str = "",
        scenario: str = "",
        note: str = "",
    ) -> ToolResult:
        store = ctx.store
        try:
            amount = round(float(target_amount), 2)
        except (TypeError, ValueError):
            return ToolResult(False, f"金额「{target_amount}」无法识别。")
        if amount <= 0:
            return ToolResult(False, "锁定金额需要大于 0。")

        account = resolve_account(ctx)
        if account is None:
            return ToolResult(False, "没有找到可用的活期账户。")
        if account.available < amount:
            return ToolResult(
                False,
                f"活期可用余额不足：可用 {account.available:,.2f} 元，需要锁定 {amount:,.2f} 元。",
            )

        goal = (
            ctx.providers.goals.add(
                user_id=ctx.user_id,
                name=name,
                target_amount=amount,
                deadline=deadline or "",
                scenario=scenario,
                account_id=account.account_id,
                note=note,
            )
            if getattr(ctx, "providers", None) is not None
            else store.add_goal(
                user_id=ctx.user_id,
                name=name,
                target_amount=amount,
                deadline=deadline or "",
                scenario=scenario,
                account_id=account.account_id,
                note=note,
            )
        )
        goal.locked_amount = amount
        account.locked = round(account.locked + amount, 2)
        store.touch(goal, account)

        lines = [
            f"已创建资金目标「{name}」，锁定 {amount:,.2f} 元",
            f"  锁定账户：{account.name}（尾号 {account.tail}）",
            f"  账户余额 {account.balance:,.2f} 元，其中锁定 {account.locked:,.2f} 元，"
            f"可用 {account.available:,.2f} 元",
        ]
        if deadline:
            lines.append(f"  目标日期：{deadline}")
        lines.append(f"  目标编号：{goal.goal_id}（可随时说「解冻这笔钱」取消锁定）")
        return ToolResult(
            True,
            "\n".join(lines),
            data={"goal_id": goal.goal_id, "amount": amount, "account": account.account_id},
            summary=f"已锁定 {amount:,.2f} 元到目标「{name}」",
        )


class GoalListTool(Tool):
    """目标 / 心愿清单。"""

    name = "goal.list"
    category = "跨场景联动"
    description = "查看所有资金目标、锁定金额与进度"
    risk_level = "low"
    params = []
    examples = ["我的攒钱目标", "看看我锁了多少钱"]

    def run(self, ctx: ToolContext) -> ToolResult:  # type: ignore[override]
        goals = (
            ctx.providers.goals.of(ctx.user_id)
            if getattr(ctx, "providers", None) is not None
            else ctx.store.goals_of(ctx.user_id)
        )
        if not goals:
            return ToolResult(True, "您还没有设置资金目标。", summary="暂无目标")
        rows = []
        for g in goals:
            progress = g.locked_amount / g.target_amount * 100 if g.target_amount else 0
            rows.append([
                g.name,
                f"{g.target_amount:,.2f}",
                f"{g.locked_amount:,.2f}",
                f"{progress:.0f}%",
                g.deadline or "-",
                {"active": "进行中", "achieved": "已完成", "cancelled": "已取消"}.get(g.status, g.status),
            ])
        total_locked = sum(g.locked_amount for g in goals if g.status == "active")
        lines = [
            f"共有 {len(goals)} 个资金目标，当前锁定 {total_locked:,.2f} 元：",
            render_table(["目标", "目标金额", "已锁定", "进度", "目标日期", "状态"], rows),
        ]
        return ToolResult(
            True,
            "\n".join(lines),
            data={"count": len(goals), "total_locked": round(total_locked, 2)},
            summary=f"{len(goals)} 个目标，锁定 {total_locked:,.2f} 元",
        )


class ScheduleGiftTool(Tool):
    """预约下单（跨场景执行环节）。"""

    name = "crossscene.schedule_gift"
    category = "跨场景联动"
    description = "在指定日期为指定的人预约下单礼物（鲜花、蛋糕、礼物卡等）"
    risk_level = "medium"
    requires_confirmation = True
    params = [
        ToolParam("person", "string", True, "收礼人，如 老婆 / 林雅雯"),
        ToolParam("item", "string", True, "礼物类型，如 鲜花 / 蛋糕"),
        ToolParam("date", "string", True, "执行日期，如 2026-11-18 / 生日前2天"),
        ToolParam("budget", "number", False, "预算（元）"),
        ToolParam("note", "string", False, "贺卡留言"),
    ]
    examples = ["生日前2天给老婆订一束花", "生日当天订蛋糕"]
    DEFAULT_BUDGET = {"鲜花": 268.0, "蛋糕": 328.0, "礼物": 500.0}

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        person: str,
        item: str,
        date: str,
        budget: Any = None,
        note: str = "",
    ) -> ToolResult:
        store = ctx.store
        target_person, birthday = lookup_person(ctx, person)
        display_name = target_person.name if target_person else person

        run_day = parse_date(date, ctx.clock.today(), base=birthday)
        if run_day is None:
            return ToolResult(False, f"没能理解下单日期「{date}」。")

        try:
            max_budget = float(budget) if budget is not None else None
        except (TypeError, ValueError):
            max_budget = None
        merchant_provider = (
            ctx.providers.merchants
            if getattr(ctx, "providers", None) is not None
            else MerchantOfferProvider(store)
        )
        offer = merchant_provider.find_offer(
            item=item,
            budget=max_budget,
            delivery_date=run_day.isoformat(),
            city=ctx.user.city,
        )
        if offer is None:
            catalog_offer = merchant_provider.find_offer(
                item=item,
                budget=None,
                delivery_date=run_day.isoformat(),
                city=ctx.user.city,
            )
            if catalog_offer is not None:
                return ToolResult(
                    False,
                    f"「{item}」的演示商户最低报价为 {catalog_offer.price:,.2f} 元，"
                    "当前预算不足。",
                )
            if max_budget is None or max_budget <= 0:
                return ToolResult(
                    False,
                    f"演示商户目录中没有「{item}」，请提供本次礼物价格后再预约。",
                )
            merchant_name = "用户报价模拟代购"
            money = max_budget
            source = "user:quote"
            delivery_radius = 0.0
            source_note = "用户报价，未联网核验"
        else:
            merchant_name = offer.merchant
            money = offer.price
            source = offer.source
            delivery_radius = offer.delivery_radius_km
            source_note = offer.source

        task = (
            ctx.providers.tasks.add(
                user_id=ctx.user_id,
                kind="gift_order",
                run_date=run_day.isoformat(),
                title=f"为{display_name}在{merchant_name}预订{item}（{money:,.0f} 元）",
                payload={
                    "person": display_name,
                    "item": item,
                    "budget": money,
                    "offer_id": offer.offer_id if offer is not None else "",
                    "merchant": merchant_name,
                    "delivery_radius_km": delivery_radius,
                    "source": source,
                    "is_demo": offer.is_demo if offer is not None else True,
                    "note": note,
                    "channel": "同城即时配送",
                },
            )
            if getattr(ctx, "providers", None) is not None
            else store.add_task(
                user_id=ctx.user_id,
                kind="gift_order",
                run_date=run_day.isoformat(),
                title=f"为{display_name}在{merchant_name}预订{item}（{money:,.0f} 元）",
                payload={
                    "person": display_name,
                    "item": item,
                    "budget": money,
                    "offer_id": offer.offer_id if offer is not None else "",
                    "merchant": merchant_name,
                    "delivery_radius_km": delivery_radius,
                    "source": source,
                    "is_demo": offer.is_demo if offer is not None else True,
                    "note": note,
                    "channel": "同城即时配送",
                },
            )
        )
        lines = [
            f"已预约：{run_day.isoformat()} 为「{display_name}」下单{item}，预算 {money:,.2f} 元",
            f"  商户：{merchant_name}（{source_note}）",
            f"  预约单号：{task.task_id}",
            f"  配送方案：同城即时配送，覆盖半径 {delivery_radius:g} 公里（原型为模拟下单）",
        ]
        if note:
            lines.append(f"  贺卡留言：{note}")
        return ToolResult(
            True,
            "\n".join(lines),
            data={"task_id": task.task_id, "run_date": run_day.isoformat(), "item": item, "budget": money},
            summary=f"已预约 {run_day.isoformat()} 为{display_name}预订{item}",
        )


class DailyBriefTool(Tool):
    """每日简报：把多个业务域的信息聚合成一份"今天要看什么"。"""

    name = "crossscene.daily_brief"
    category = "跨场景联动"
    description = "主动汇总今日待办：异常交易、即将扣费、待执行任务与目标进度"
    risk_level = "low"
    params = []
    examples = ["今天有什么要注意的", "帮我做个每日简报"]

    def run(self, ctx: ToolContext) -> ToolResult:  # type: ignore[override]
        store = ctx.store
        today = ctx.clock.today()
        sections: List[str] = []
        summary_bits: List[str] = []

        since = (today - timedelta(days=6)).isoformat()
        recent = (
            ctx.providers.transactions.of(ctx.user_id, since, today.isoformat())
            if getattr(ctx, "providers", None) is not None
            else store.transactions_of(ctx.user_id, since, today.isoformat())
        )
        anomalies = ctx.risk.detect_anomalies(ctx.user_id, recent)
        if anomalies:
            summary_bits.append(f"{len(anomalies)} 笔异常交易")
            rows = [[a.date, a.merchant, format_money(a.amount), a.reason[:30]] for a in anomalies[:4]]
            sections.append("【风险提醒】\n" + render_table(["日期", "商户", "金额", "原因"], rows))
        else:
            sections.append("【风险提醒】近 7 天没有发现异常交易。")

        upcoming = []
        subscriptions = (
            ctx.providers.subscriptions.of(ctx.user_id, "active")
            if getattr(ctx, "providers", None) is not None
            else store.subscriptions_of(ctx.user_id, status="active")
        )
        for sub in subscriptions:
            if not sub.auto_renew:
                continue
            try:
                delta = (date.fromisoformat(sub.next_charge_date) - today).days
            except ValueError:
                continue
            if 0 <= delta <= 7:
                upcoming.append((sub, delta))
        if upcoming:
            summary_bits.append(f"{len(upcoming)} 项订阅即将扣费")
            rows = [[s.merchant, f"{s.amount:,.2f}", s.next_charge_date, f"{d} 天后"] for s, d in upcoming]
            sections.append("【订阅扣费】\n" + render_table(["商户", "金额", "扣费日", "倒计时"], rows))
        else:
            sections.append("【订阅扣费】未来 7 天没有订阅扣费。")

        tasks_source = (
            ctx.providers.tasks.of(ctx.user_id, "pending")
            if getattr(ctx, "providers", None) is not None
            else store.tasks_of(ctx.user_id, status="pending")
        )
        tasks = [t for t in tasks_source if t.run_date >= today.isoformat()]
        if tasks:
            summary_bits.append(f"{len(tasks)} 项待执行任务")
            rows = [[t.run_date, t.title] for t in tasks[:5]]
            sections.append("【待执行计划】\n" + render_table(["日期", "任务"], rows))
        else:
            sections.append("【待执行计划】暂无待执行任务。")

        goals = (
            ctx.providers.goals.of(ctx.user_id, "active")
            if getattr(ctx, "providers", None) is not None
            else store.goals_of(ctx.user_id, status="active")
        )
        if goals:
            rows = [[g.name, f"{g.locked_amount:,.0f}/{g.target_amount:,.0f}", g.deadline or "-"] for g in goals]
            sections.append("【资金目标】\n" + render_table(["目标", "已锁定/目标", "日期"], rows))

        header = f"每日简报（{today.isoformat()}）"
        if summary_bits:
            header += "：" + "、".join(summary_bits)
        message = header + "\n" + "\n\n".join(sections)
        return ToolResult(
            True,
            message,
            data={"anomalies": len(anomalies), "upcoming_subs": len(upcoming), "tasks": len(tasks)},
            summary="；".join(summary_bits) if summary_bits else "今日没有需要特别关注的事项",
        )

