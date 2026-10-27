"""智能转账工具集。

覆盖赛题要求：
- 按人名 / 手机号 / 备注转账；
- 定时转账（一次性 / 每月重复）；
- 拆分 AA 收款。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, List, Optional

from finagent.data.card_ledger import (
    card_asset_total,
    linked_account,
    update_card_balance,
    verify_card_password,
)
from finagent.data.models import Account, Card
from finagent.data.providers import build_data_providers
from finagent.tools.base import Tool, ToolContext, ToolParam, ToolResult, render_table
from finagent.utils import parse_date

CHANNEL_LABELS = {
    "phone": "手机号（数字人民币）",
    "wechat": "微信",
    "alipay": "支付宝",
    "bank": "银行卡",
}


def channel_account(payee, channel: str) -> str:
    """按渠道取收款账号。"""
    if payee is None:
        return ""
    if channel == "wechat":
        return payee.wechat
    if channel == "alipay":
        return payee.alipay
    if channel == "phone":
        return payee.wallet_phone or payee.phone
    if channel == "bank":
        return f"**** **** **** {payee.tail}" if payee.tail else ""
    return ""


def set_payee_channel_account(payee, channel: str, account: str) -> str:
    """Bind a user-supplied receiving account to the selected transfer channel."""
    value = str(account or "").strip()
    if not value:
        return ""
    if channel == "phone":
        digits = "".join(ch for ch in value if ch.isdigit())
        payee.wallet_phone = digits
        if not payee.phone:
            payee.phone = digits
    elif channel == "wechat":
        payee.wechat = value
    elif channel == "alipay":
        payee.alipay = value
    elif channel == "bank":
        digits = "".join(ch for ch in value if ch.isdigit())
        if digits:
            payee.tail = digits[-4:]
    return value


def resolve_account(ctx: ToolContext, keyword: str = "") -> Optional[Account]:
    """把"信用卡 / 零钱 / 活期"这类口语映射到具体账户。"""
    providers = ctx.providers or build_data_providers(ctx.store)
    return providers.accounts.resolve(ctx.user_id, keyword)


def resolve_payment_card(ctx: ToolContext, keyword: str = "") -> Optional[Card]:
    """Resolve a spendable bank card while keeping old account labels usable."""
    providers = ctx.providers or build_data_providers(ctx.store)
    cards = [
        card for card in providers.cards.of(ctx.user_id)
        if card.kind in {"debit", "virtual"} and card.status == "active"
    ]
    key = (keyword or "").strip()
    key = re.sub(r"(?:卡中|卡里|卡上|里面|中|里|上)$", "卡", key)
    key = key.strip("从给到 ")
    if not key:
        return next(
            (card for card in cards if linked_account(ctx.store, card) and
             linked_account(ctx.store, card).kind == "current"),
            cards[0] if cards else None,
        )
    aliases = {
        "活期": "current", "工资卡": "current", "储蓄卡": "savings",
        "定期": "savings", "钱包": "wallet", "零钱": "wallet",
        "虚拟卡": "virtual",
    }
    for label, account_kind in aliases.items():
        if label in key:
            for card in cards:
                if account_kind == "virtual" and card.kind == "virtual":
                    return card
                account = linked_account(ctx.store, card)
                if account and account.kind == account_kind:
                    return card
    digits = "".join(ch for ch in key if ch.isdigit())
    for card in cards:
        if card.card_id == key:
            return card
        if digits and card.tail == digits[-4:]:
            return card
        if key and (key in card.brand or key in card.note):
            return card
    return None


def resolve_owned_card(ctx: ToolContext, keyword: str = "") -> Optional[Card]:
    """Resolve any owned card, including frozen, lost, or credit cards.

    Payment operations use resolve_payment_card for executable cards. This
    helper is for diagnostics so the agent can explain why a nominated card
    cannot be used instead of silently falling back to another card.
    """
    key = (keyword or "").strip().strip("从给到向 ")
    if not key:
        return None
    providers = ctx.providers or build_data_providers(ctx.store)
    return providers.cards.find(ctx.user_id, key)


class TransferTool(Tool):
    """立即转账。"""

    name = "transfer.send"
    category = "智能转账"
    description = "按收款人姓名/手机号/备注向他人转账，立即到账"
    risk_level = "medium"
    params = [
        ToolParam("payee", "string", True, "收款人姓名、备注昵称或手机号"),
        ToolParam("amount", "number", True, "转账金额（元）"),
        ToolParam("channel", "string", True, "收款方式", enum=["phone", "wechat", "alipay", "bank"]),
        ToolParam("note", "string", False, "转账备注"),
        ToolParam("account", "string", False, "付款银行卡，如 工资卡/尾号6222，默认活期卡"),
        ToolParam("payee_account", "string", False, "本次提供的收款账号"),
        ToolParam("payment_password", "string", True, "付款卡 6 位交易密码"),
    ]
    examples = ["给老婆转2000块，用微信", "转500给13700003333，用支付宝", "给房东转3800房租，用银行卡"]

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        payee: str,
        amount: Any,
        channel: str = "",
        note: str = "",
        account: str = "",
        payee_account: str = "",
        payment_password: str = "",
        payment_verified: bool = False,
    ) -> ToolResult:
        user = ctx.user
        store = ctx.store
        providers = ctx.providers or build_data_providers(store)
        try:
            value = round(float(amount), 2)
        except (TypeError, ValueError):
            return ToolResult(False, f"金额「{amount}」无法识别，请用数字金额，例如 500 或 1.5万。")

        payer = resolve_payment_card(ctx, account)
        payee_obj = providers.payees.find(user.user_id, payee)
        channel = (channel or "phone").strip().lower()
        if channel not in CHANNEL_LABELS:
            channel = "phone"

        # 二次风控：调度层已经拦过一次，这里做最终校验，防止绕过。
        decision = ctx.risk.check_transfer(user, payer, payee_obj, payee, value)
        if decision.blocked:
            return ToolResult(False, "转账未执行：" + decision.summary(), data={"blocked": True})

        if payer is None:
            return ToolResult(False, "没有找到可用的付款银行卡，请指定工资卡、尾号或银行卡名称。")
        if payer.kind == "credit":
            return ToolResult(False, "当前不支持信用卡直接对外转账，请选择借记卡付款。")
        if not payment_verified and not verify_card_password(payer, payment_password):
            return ToolResult(
                False,
                "付款卡交易密码验证失败，转账未执行。",
                data={"reason": "bad_payment_password"},
            )
        if payer.available < value:
            return ToolResult(
                False,
                f"付款卡余额不足：可用 {payer.available:,.2f} 元，需要 {value:,.2f} 元。",
            )

        # 收款人不在通讯录时，先落库为常用联系人（后续可按备注直接转）
        if payee_obj is None:
            phone = "".join(ch for ch in payee if ch.isdigit())
            payee_obj = providers.payees.remember(
                user.user_id,
                name=payee,
                phone=phone if len(phone) >= 11 else "",
                alias="",
                relation="其他",
            )
            if channel == "phone" and phone:
                payee_obj.wallet_phone = phone
        else:
            payee_obj.last_used = ctx.clock.iso()

        target_account = channel_account(payee_obj, channel)
        if payee_account:
            target_account = set_payee_channel_account(
                payee_obj,
                channel,
                payee_account,
            )
            ctx.store.touch(payee_obj)
        if not target_account:
            return ToolResult(
                False,
                f"「{payee_obj.name}」还没有绑定{CHANNEL_LABELS[channel]}收款账号，"
                f"换一种收款方式，或先补充对方的{CHANNEL_LABELS[channel]}号。",
            )

        # 记一笔转出流水；收款方在同一份模拟数据里同步入账，方便演示
        txn = store.add_transaction(
            user_id=user.user_id,
            account_id=payer.account_id,
            amount=-value,
            merchant=payee_obj.name,
            category="转账",
            channel="转账",
            counterparty=payee_obj.phone or payee_obj.name,
            note=(note or "转账") + f"（{CHANNEL_LABELS[channel]}）",
            tags=["transfer", channel],
        )
        update_card_balance(ctx.store, payer, payer.balance - value)
        ctx.store.touch(payee_obj)
        self._credit_payee(ctx, payee_obj, value)
        asset_total_after = card_asset_total(ctx.store, ctx.user_id)

        lines = [
            f"转账成功（{CHANNEL_LABELS[channel]}）：{value:,.2f} 元已转给「{payee_obj.name}」",
            f"  收款账号：{target_account}",
            f"  付款银行卡：{payer.brand}（尾号 {payer.tail}）  余额 {payer.balance:,.2f} 元",
            f"  流水号：{txn.txn_id}",
            f"  银行卡资产总额：{asset_total_after:,.2f} 元",
        ]
        if note:
            lines.append(f"  备注：{note}")
        if decision.reasons:
            lines.append(f"  风控提示：{decision.summary()}")
        return ToolResult(
            True,
            "\n".join(lines),
            data={
                "txn_id": txn.txn_id,
                "payee": payee_obj.name,
                "amount": value,
                "account": payer.account_id,
                "card_id": payer.card_id,
                "card_balance": payer.balance,
                "asset_total_after": asset_total_after,
                "risk": decision.summary(),
            },
            summary=f"已向{payee_obj.name}转账 {value:,.2f} 元",
        )

    @staticmethod
    def _credit_payee(ctx: ToolContext, payee, value: float) -> None:
        """如果收款人也是本原型里的模拟用户，则同步给 ta 记一笔入账，方便演示双向流水。"""
        for user in ctx.store.users.values():
            if user.name == payee.name:
                providers = ctx.providers or build_data_providers(ctx.store)
                account = providers.accounts.default_for_payment(user.user_id)
                if account:
                    account.balance = round(account.balance + value, 2)
                    ctx.store.touch(account)
                    ctx.store.add_transaction(
                        user_id=user.user_id,
                        account_id=account.account_id,
                        amount=value,
                        merchant=ctx.user.name,
                        category="转账",
                        channel="转账",
                        counterparty=ctx.user.name,
                        note="收到转账",
                    )
                return


class InternalTransferTool(Tool):
    """Move funds between two cards owned by the same customer."""

    name = "transfer.internal"
    category = "智能转账"
    description = "在本人不同银行卡之间划转资金，需要付款卡交易密码"
    risk_level = "medium"
    params = [
        ToolParam("from_card", "string", True, "付款银行卡"),
        ToolParam("to_card", "string", True, "收款银行卡"),
        ToolParam("amount", "number", True, "划转金额（元）"),
        ToolParam("payment_password", "string", True, "付款卡 6 位交易密码"),
        ToolParam("note", "string", False, "划转备注"),
    ]
    examples = ["从工资卡转1000到定期卡", "尾号6222转500到3011"]

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        from_card: str,
        to_card: str,
        amount: Any,
        payment_password: str,
        note: str = "",
    ) -> ToolResult:
        try:
            value = round(float(amount), 2)
        except (TypeError, ValueError):
            return ToolResult(False, "划转金额无法识别。")
        if value <= 0:
            return ToolResult(False, "划转金额必须大于 0。")
        source = resolve_payment_card(ctx, from_card)
        target = resolve_payment_card(ctx, to_card)
        if source is None or target is None:
            return ToolResult(False, "没有找到付款卡或收款卡，请使用银行卡名称、尾号或绑定备注。")
        if source.card_id == target.card_id:
            return ToolResult(False, "付款卡和收款卡不能是同一张卡。")
        if not verify_card_password(source, payment_password):
            return ToolResult(False, "付款卡交易密码验证失败，本行内部转账未执行。")
        if source.available < value:
            return ToolResult(
                False,
                f"付款卡余额不足：可用 {source.available:,.2f} 元，需要 {value:,.2f} 元。",
            )
        decision = ctx.risk.check_operation(
            "transfer.send",
            {"amount": value, "account": source.card_id},
            ctx.user,
            account=source,
        )
        if decision.blocked:
            return ToolResult(False, "内部转账未执行：" + decision.summary())

        source_before = source.balance
        target_before = target.balance
        update_card_balance(ctx.store, source, source_before - value)
        update_card_balance(ctx.store, target, target_before + value)
        out_txn = ctx.store.add_transaction(
            user_id=ctx.user_id,
            account_id=source.account_id,
            amount=-value,
            merchant=f"本人 {target.brand} 尾号{target.tail}",
            category="转账",
            channel="本行转账",
            counterparty=f"本人尾号{target.tail}",
            note=note or "本人银行卡内部划转",
            tags=["internal_transfer", "internal_transfer_out"],
        )
        in_txn = ctx.store.add_transaction(
            user_id=ctx.user_id,
            account_id=target.account_id,
            amount=value,
            merchant=f"本人 {source.brand} 尾号{source.tail}",
            category="转账",
            channel="本行转账",
            counterparty=f"本人尾号{source.tail}",
            note=note or "本人银行卡内部划转",
            tags=["internal_transfer", "internal_transfer_in"],
        )
        asset_total_after = card_asset_total(ctx.store, ctx.user_id)
        lines = [
            f"本人银行卡内部划转成功：{value:,.2f} 元",
            f"  转出卡：{source.brand}（尾号 {source.tail}） "
            f"{source_before:,.2f} → {source.balance:,.2f} 元",
            f"  转入卡：{target.brand}（尾号 {target.tail}） "
            f"{target_before:,.2f} → {target.balance:,.2f} 元",
            f"  转出流水：{out_txn.txn_id}；转入流水：{in_txn.txn_id}",
            f"  银行卡资产总额：{asset_total_after:,.2f} 元（内部划转不改变总额）。",
        ]
        if decision.reasons:
            lines.append("  风控提示：" + decision.summary())
        return ToolResult(
            True,
            "\n".join(lines),
            data={
                "amount": value,
                "from_card_id": source.card_id,
                "to_card_id": target.card_id,
                "from_balance": source.balance,
                "to_balance": target.balance,
                "out_txn_id": out_txn.txn_id,
                "in_txn_id": in_txn.txn_id,
                "asset_total_unchanged": True,
                "asset_total_after": asset_total_after,
            },
            summary=f"已在本人银行卡间划转 {value:,.2f} 元",
        )


class ScheduledTransferTool(Tool):
    """定时转账。"""

    name = "transfer.schedule"
    category = "智能转账"
    description = "设置定时/周期性转账，到指定日期自动执行"
    risk_level = "medium"
    params = [
        ToolParam("payee", "string", True, "收款人姓名、备注昵称或手机号"),
        ToolParam("amount", "number", True, "每期转账金额（元）"),
        ToolParam("when", "string", True, "执行日期，如 明天 / 2026-11-01 / 下个月5号"),
        ToolParam("channel", "string", True, "收款方式", enum=["phone", "wechat", "alipay", "bank"]),
        ToolParam("repeat", "string", False, "重复方式", enum=["once", "monthly", "weekly"]),
        ToolParam("note", "string", False, "转账备注"),
        ToolParam("account", "string", False, "付款银行卡，如 工资卡/尾号6222"),
        ToolParam("payee_account", "string", False, "本次提供的收款账号"),
        ToolParam("payment_password", "string", True, "付款卡 6 位交易密码"),
    ]
    examples = ["下个月5号自动给房东转3800房租", "明天给妈妈转2000"]

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        payee: str,
        amount: Any,
        when: str,
        channel: str = "",
        repeat: str = "once",
        note: str = "",
        account: str = "",
        payee_account: str = "",
        payment_password: str = "",
    ) -> ToolResult:
        user = ctx.user
        store = ctx.store
        providers = ctx.providers or build_data_providers(store)
        try:
            value = round(float(amount), 2)
        except (TypeError, ValueError):
            return ToolResult(False, f"金额「{amount}」无法识别，请用数字金额。")

        run_day: Optional[date] = parse_date(when, ctx.clock.today())
        if run_day is None:
            return ToolResult(False, f"没能理解执行时间「{when}」，可以试试「明天」「下个月5号」「2026-12-01」。")

        payee_obj = providers.payees.find(user.user_id, payee)
        if payee_obj is None:
            phone = "".join(ch for ch in payee if ch.isdigit())
            payee_obj = providers.payees.remember(
                user.user_id, name=payee, phone=phone if len(phone) >= 11 else "", relation="其他"
            )

        repeat_label = {"once": "一次性", "monthly": "每月", "weekly": "每周"}.get(repeat, "一次性")
        channel = (channel or "phone").strip().lower()
        channel_label = CHANNEL_LABELS.get(channel, CHANNEL_LABELS["phone"])
        if payee_account:
            set_payee_channel_account(payee_obj, channel, payee_account)
            ctx.store.touch(payee_obj)
        if not channel_account(payee_obj, channel):
            return ToolResult(
                False,
                f"「{payee_obj.name}」还没有绑定{channel_label}收款账号，"
                "本次定时转账未创建。",
            )
        payer = resolve_payment_card(ctx, account)
        if payer is None:
            return ToolResult(False, "没有找到可用的付款银行卡。")
        if not verify_card_password(payer, payment_password):
            return ToolResult(False, "付款卡交易密码验证失败，定时转账未创建。")
        task = providers.tasks.add(
            user_id=user.user_id,
            kind="scheduled_transfer",
            run_date=run_day.isoformat(),
            title=f"{repeat_label}转账给{payee_obj.name} {value:,.2f} 元",
            payload={
                "payee": payee_obj.name,
                "payee_id": payee_obj.payee_id,
                "amount": value,
                "channel": channel,
                "note": note or "定时转账",
                "account": payer.card_id,
                "payment_verified": True,
            },
            repeat=repeat,
        )
        lines = [
            f"定时转账已创建：{repeat_label}向「{payee_obj.name}」转 {value:,.2f} 元（{channel_label}）",
            f"  首次执行：{run_day.isoformat()}",
            f"  付款银行卡：{payer.brand}（尾号 {payer.tail}）",
            f"  计划编号：{task.task_id}",
            "  每次执行前会校验余额与收款账号状态；失败会生成提醒，不会静默跳过。",
            "  到期后由 Agent 自动执行，也可用 /advance 命令把时间快进来演示。",
        ]
        return ToolResult(
            True,
            "\n".join(lines),
            data={"task_id": task.task_id, "run_date": run_day.isoformat(), "repeat": repeat},
            summary=f"已设置 {run_day.isoformat()} {repeat_label}转账 {value:,.2f} 元给{payee_obj.name}",
        )


class SplitBillTool(Tool):
    """拆分 AA 收款。"""

    name = "transfer.split_aa"
    category = "智能转账"
    description = "把一笔共同消费按人数平摊，向其他人发起 AA 收款"
    risk_level = "low"
    params = [
        ToolParam("total", "number", True, "消费总金额（元）"),
        ToolParam("participants", "array", True, "参与人列表，含自己，如 ['我','王强','李思琪']"),
        ToolParam("channel", "string", False, "收款方式", enum=["wechat", "alipay", "phone"]),
        ToolParam("note", "string", False, "消费说明，如 团建聚餐"),
        ToolParam("payer", "string", False, "谁先垫付的，默认本人"),
    ]
    examples = ["昨晚聚餐花了960，我、王强、李思琪AA", "AA收款 3000块 4个人"]

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        total: Any,
        participants: List[str],
        channel: str = "wechat",
        note: str = "",
        payer: str = "",
    ) -> ToolResult:
        store = ctx.store
        user = ctx.user
        try:
            amount_total = round(float(total), 2)
        except (TypeError, ValueError):
            return ToolResult(False, f"总金额「{total}」无法识别。")

        names: List[str] = []
        if isinstance(participants, str):
            raw = participants.replace("，", ",").replace("、", ",").replace("和", ",")
            names = [x.strip() for x in raw.split(",") if x.strip()]
        else:
            names = [str(x).strip() for x in participants if str(x).strip()]
        names = [n for n in names if n]
        if len(names) < 2:
            return ToolResult(False, "AA 至少需要两位参与人，请告诉我都有谁，例如「我、王强、李思琪」。")
        if amount_total <= 0:
            return ToolResult(False, "总金额需要大于 0。")

        share = round(amount_total / len(names), 2)
        others = [n for n in names if n not in {"我", "本人", user.name}]
        if not others:
            return ToolResult(False, "除了你以外还需要至少一位参与人。")

        rows = []
        participants_data = []
        for name in names:
            is_me = name in {"我", "本人", user.name}
            status = "已垫付" if is_me else "待收款"
            label = name if name in {"我", "本人"} else (name + ("（我）" if is_me else ""))
            rows.append([label, f"{share:,.2f}", status])
            participants_data.append(
                {"name": name, "amount": share, "is_self": is_me, "status": "paid" if is_me else "pending"}
            )

        channel = (channel or "wechat").strip().lower()
        if channel not in CHANNEL_LABELS:
            channel = "wechat"
        channel_label = CHANNEL_LABELS[channel]
        sheet = store.add_aa_sheet(
            user_id=user.user_id,
            title=note or "AA 收款",
            total_amount=amount_total,
            participants=participants_data,
            note=f"{len(names)} 人平摊，{channel_label}收款",
        )
        table = render_table(["参与人", "应付金额", "状态"], rows)
        message = "\n".join(
            [
                f"AA 收款单已创建（{sheet.sheet_id}）：{note or '共同消费'}，总金额 {amount_total:,.2f} 元",
                table,
                f"  收款方式：{channel_label}",
                f"  已向 {len(others)} 位好友发起收款，每人 {share:,.2f} 元，收齐后自动入账活期。",
                "  说明：原型生成的是模拟收款单与分享链接；真实接入微信收款需要商户号与官方支付 API。",
            ]
        )
        return ToolResult(
            True,
            message,
            data={"sheet_id": sheet.sheet_id, "share": share, "others": others, "total": amount_total},
            summary=f"AA 收款 {amount_total:,.2f} 元已按 {len(names)} 人平摊，每人 {share:,.2f} 元",
        )


class ListScheduledTransferTool(Tool):
    """查看已设置的定时任务。"""

    name = "transfer.list_scheduled"
    category = "智能转账"
    description = "查看所有待执行的定时转账与预约任务"
    risk_level = "low"
    params = []
    examples = ["我有哪些定时转账"]

    def run(self, ctx: ToolContext) -> ToolResult:  # type: ignore[override]
        providers = ctx.providers or build_data_providers(ctx.store)
        tasks = providers.tasks.of(ctx.user_id, "pending")
        if not tasks:
            return ToolResult(True, "当前没有待执行的定时任务。", data={"count": 0}, summary="没有定时任务")
        rows = [
            [t.run_date, t.title, {"scheduled_transfer": "定时转账", "gift_order": "预约下单",
                                   "reminder": "提醒", "subscription_cancel": "取消订阅"}.get(t.kind, t.kind),
             {"once": "一次性", "monthly": "每月", "weekly": "每周"}.get(t.repeat, t.repeat)]
            for t in tasks
        ]
        table = render_table(["执行日期", "任务", "类型", "周期"], rows)
        return ToolResult(
            True,
            f"待执行任务共 {len(tasks)} 项：\n{table}",
            data={"count": len(tasks)},
            summary=f"共 {len(tasks)} 项待执行任务",
        )

