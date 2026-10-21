"""卡片管理工具集。

覆盖赛题要求：卡申请、额度调整、交易限制/解锁、卡片挂失。
"""
from __future__ import annotations

from typing import Any

from finagent.data.models import Card
from finagent.data.provenance import source_badge
from finagent.tools.base import Tool, ToolContext, ToolParam, ToolResult, render_table

KIND_LABEL = {
    "debit": "借记卡",
    "credit": "信用卡",
    "virtual": "虚拟卡",
    "supplementary": "附属卡",
}

STATUS_LABEL = {
    "active": "正常",
    "frozen": "已冻结",
    "lost": "已挂失",
    "cancelled": "已销卡",
    "pending": "待激活",
}

SCOPE_LABEL = {
    "online": "线上交易",
    "overseas": "境外交易",
    "contactless": "小额免密/闪付",
}


def _cards(ctx: ToolContext, status: str = ""):
    if getattr(ctx, "providers", None) is not None:
        return ctx.providers.cards.of(ctx.user_id, status or None)
    return ctx.store.cards_of(ctx.user_id, status or None)


def _find_card(ctx: ToolContext, keyword: str = ""):
    if getattr(ctx, "providers", None) is not None:
        return ctx.providers.cards.find(ctx.user_id, keyword)
    return ctx.store.find_card(ctx.user_id, keyword)


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "开", "开启", "打开", "启用", "允许"}:
        return True
    if text in {"0", "false", "no", "off", "关", "关闭", "禁用", "禁止"}:
        return False
    return default


class CardListTool(Tool):
    """查看名下卡片。"""

    name = "card.list"
    category = "卡片管理"
    description = "查看名下所有银行卡、状态、额度与交易开关"
    risk_level = "low"
    params = []
    examples = ["我有哪些卡", "我的信用卡额度"]

    def run(self, ctx: ToolContext) -> ToolResult:  # type: ignore[override]
        cards = _cards(ctx)
        if not cards:
            return ToolResult(True, "您名下暂无卡片。", summary="无卡片")
        rows = []
        for c in cards:
            limit = f"{c.credit_limit:,.0f} / 已用 {c.used:,.0f}" if c.kind == "credit" else f"日限额 {c.daily_limit:,.0f}"
            switches = "".join(
                [
                    "线上✓ " if c.online_enabled else "线上✗ ",
                    "境外✓ " if c.overseas_enabled else "境外✗ ",
                    "闪付✓" if c.contactless_enabled else "闪付✗",
                ]
            )
            source = source_badge(c.source, c.is_live, c.is_demo)
            rows.append([KIND_LABEL.get(c.kind, c.kind), f"尾号{c.tail}", STATUS_LABEL.get(c.status, c.status),
                         limit, switches, source])
        lines = [
            f"您名下共有 {len(cards)} 张卡片：",
            render_table(["卡种", "卡号", "状态", "额度/限额", "交易开关", "来源"], rows),
        ]
        return ToolResult(True, "\n".join(lines), data={"count": len(cards)}, summary=f"共 {len(cards)} 张卡片")


BANK_BINS = {
    "622202": "工商银行", "622848": "农业银行", "621661": "中国银行",
    "621700": "建设银行", "622588": "招商银行", "622262": "交通银行",
}


def luhn_ok(number: str) -> bool:
    """Luhn 校验，真实银行卡号的基本合法性检查。"""
    digits = [int(ch) for ch in number if ch.isdigit()]
    if len(digits) < 12:
        return False
    checksum = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


class CardBindTool(Tool):
    """绑定他行银行卡（模拟四大行等真实银行的绑卡流程）。"""

    name = "card.bind"
    category = "卡片管理"
    description = "绑定工商/农业/中国/建设/招商等银行的储蓄卡，需卡号 + 取款密码验证"
    risk_level = "high"
    requires_confirmation = True
    params = [
        ToolParam("card_no", "string", True, "银行卡号（16-19 位）"),
        ToolParam("password", "string", True, "银行卡取款密码（演示环境固定 123456）"),
        ToolParam("bank", "string", False, "发卡行，可根据卡号自动识别"),
        ToolParam("phone", "string", False, "银行预留手机号"),
    ]
    examples = ["绑定一张工商银行的储蓄卡", "添加银行卡"]
    DEMO_PASSWORD = "123456"

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        card_no: Any,
        password: Any = "",
        bank: str = "",
        phone: str = "",
    ) -> ToolResult:
        number = "".join(ch for ch in str(card_no) if ch.isdigit())
        if not (16 <= len(number) <= 19):
            return ToolResult(False, "卡号应为 16-19 位数字，请核对后重新输入。")
        if not luhn_ok(number):
            return ToolResult(False, "卡号校验未通过（Luhn 校验失败），请确认没有输错。")
        bank_name = BANK_BINS.get(number[:6], "")
        if not bank_name:
            return ToolResult(
                False,
                "暂不支持该发卡行。目前支持：工商银行、农业银行、中国银行、建设银行、招商银行、交通银行。",
            )
        if str(password).strip() != self.DEMO_PASSWORD:
            return ToolResult(False, "取款密码不正确，绑定失败。", data={"reason": "bad_password"})
        tail = number[-4:]
        for existing in _cards(ctx):
            if existing.tail == tail and existing.note.startswith(bank_name):
                return ToolResult(True, f"{bank_name}尾号 {tail} 的卡已经绑定过了。", summary="卡片已存在")
        card = Card(
            card_id=ctx.store.next_id("cards"),
            user_id=ctx.user_id,
            kind="debit",
            brand=bank_name,
            tail=tail,
            status="active",
            daily_limit=20000.0,
            single_limit=5000.0,
            overseas_enabled=False,
            issued_at=ctx.clock.iso(),
            note=f"{bank_name}储蓄卡（绑定）",
        )
        ctx.store.cards[card.card_id] = card
        ctx.store.touch(card)
        ctx.store.log("card.bind", card_id=card.card_id, bank=bank_name, tail=tail)
        lines = [
            f"绑定成功：{bank_name} 储蓄卡（尾号 {tail}）",
            f"  卡号：**** **** **** {tail}（仅保存后四位，完整卡号与密码不入库）",
            f"  单笔限额 {card.single_limit:,.0f} 元，日限额 {card.daily_limit:,.0f} 元",
            "  说明：这是模拟绑卡流程；真实绑卡由银行侧完成短信/密码验证，本原型仅做演示。",
        ]
        return ToolResult(
            True, "\n".join(lines),
            data={"card_id": card.card_id, "bank": bank_name, "tail": tail},
            summary=f"已绑定 {bank_name} 尾号 {tail} 的储蓄卡",
        )


class CardApplyTool(Tool):
    """申请新卡。"""

    name = "card.apply"
    category = "卡片管理"
    description = "申请新卡（信用卡/虚拟卡/附属卡），提交后进入审批流程"
    risk_level = "low"
    params = [
        ToolParam("kind", "string", False, "卡类型", enum=["credit", "virtual", "supplementary"], default="virtual"),
        ToolParam("bank", "string", False, "发卡行，如 中国银行/工商银行"),
        ToolParam("limit_request", "number", False, "期望额度（元）"),
        ToolParam("note", "string", False, "备注，如 给老婆办一张附属卡"),
    ]
    examples = ["我想申请一张信用卡", "办一张虚拟卡网购用"]

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        kind: str = "virtual",
        bank: str = "",
        limit_request: Any = None,
        note: str = "",
    ) -> ToolResult:
        kind = (kind or "virtual").strip().lower()
        if kind not in KIND_LABEL:
            kind = "virtual"
        user = ctx.user
        if not user.verified:
            return ToolResult(False, "申请卡片前需要先完成实名认证。")

        defaults = {"credit": 20_000.0, "virtual": 5_000.0, "supplementary": 10_000.0}
        try:
            limit = float(limit_request) if limit_request is not None else defaults[kind]
        except (TypeError, ValueError):
            limit = defaults[kind]

        card = Card(
            card_id=ctx.store.next_id("cards"),
            user_id=user.user_id,
            kind=kind,
            brand=bank or "银联",
            tail="****",
            status="pending",
            credit_limit=limit if kind in {"credit", "supplementary"} else 0.0,
            daily_limit=min(limit, 50_000.0) if kind != "credit" else limit,
            single_limit=min(limit / 5, 5_000.0),
            overseas_enabled=False,
            contactless_enabled=kind != "virtual",
            issued_at=ctx.clock.iso(),
            note=note or "新申请",
        )
        ctx.store.cards[card.card_id] = card
        ctx.store.touch(card)
        task = ctx.store.add_task(
            user_id=user.user_id,
            kind="reminder",
            run_date=(ctx.clock.now().date().isoformat()),
            title=f"{KIND_LABEL[kind]}审批中（申请单 {card.card_id}）",
            payload={"card_id": card.card_id},
        )
        lines = [
            f"申请已提交：{bank + ' ' if bank else ''}{KIND_LABEL[kind]}"
            + (f"（期望额度 {limit:,.0f} 元）" if limit else ""),
            f"  申请编号：{card.card_id}   当前状态：待审批",
            "  审批通过后可一键激活；虚拟卡通常几分钟内即可使用。",
        ]
        if note:
            lines.append(f"  备注：{note}")
        return ToolResult(
            True,
            "\n".join(lines),
            data={"card_id": card.card_id, "task_id": task.task_id, "kind": kind},
            summary=f"已提交{KIND_LABEL[kind]}申请",
        )


class CardLimitAdjustTool(Tool):
    """额度调整。"""

    name = "card.limit_adjust"
    category = "卡片管理"
    description = "调整信用卡额度或借记卡单笔/单日限额"
    risk_level = "medium"
    requires_confirmation = True
    params = [
        ToolParam("card", "string", False, "卡片，如 信用卡/尾号8877"),
        ToolParam("new_limit", "number", True, "新的额度（元）"),
    ]
    examples = ["把信用卡额度提到3万", "把虚拟卡单笔限额降到500"]

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        new_limit: Any,
        card: str = "",
    ) -> ToolResult:
        target = _find_card(ctx, card)
        if target is None:
            return ToolResult(False, "没有找到对应的卡片。")
        try:
            limit = float(new_limit)
        except (TypeError, ValueError):
            return ToolResult(False, f"额度「{new_limit}」无法识别。")
        if limit <= 0:
            return ToolResult(False, "额度必须大于 0。")

        if target.kind == "credit":
            if limit > 200_000:
                return ToolResult(False, "信用卡额度上限为 200,000 元，超出部分需要人工审批。")
            old = target.credit_limit
            target.credit_limit = limit
            field_name = "信用额度"
        else:
            old = target.single_limit
            target.single_limit = min(limit, target.daily_limit)
            field_name = "单笔限额"
        ctx.store.touch(target)

        direction = "提升" if limit > old else "调低"
        lines = [
            f"{KIND_LABEL.get(target.kind, target.kind)}（尾号{target.tail}）{field_name}已{direction}",
            f"  {old:,.0f} 元 → {limit:,.0f} 元，即刻生效",
        ]
        if target.kind == "credit":
            lines.append(f"  调整后可用额度 {target.credit_limit - target.used:,.2f} 元")
        else:
            lines.append(f"  当前单笔限额 {target.single_limit:,.0f} 元，日限额 {target.daily_limit:,.0f} 元")
        return ToolResult(
            True,
            "\n".join(lines),
            data={"card_id": target.card_id, "old": old, "new": limit},
            summary=f"{field_name}已{direction}至 {limit:,.0f} 元",
        )


class CardControlTool(Tool):
    """交易开关（交易限制 / 解锁）。"""

    name = "card.control"
    category = "卡片管理"
    description = "开关银行卡的线上交易、境外交易、小额免密功能"
    risk_level = "medium"
    requires_confirmation = True
    params = [
        ToolParam("card", "string", False, "卡片，如 信用卡/尾号8877"),
        ToolParam("scope", "string", True, "要调整的开关", enum=["online", "overseas", "contactless"]),
        ToolParam("enable", "boolean", True, "true=开启，false=关闭"),
    ]
    examples = ["关闭信用卡的境外交易", "把虚拟卡线上交易打开"]

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        scope: str,
        enable: Any = True,
        card: str = "",
    ) -> ToolResult:
        target = _find_card(ctx, card)
        if target is None:
            return ToolResult(False, "没有找到对应的卡片。")
        scope_key = (scope or "").strip().lower()
        aliases = {"境外": "overseas", "海外": "overseas", "线上": "online", "网络": "online",
                   "免密": "contactless", "闪付": "contactless", "云闪付": "contactless"}
        scope_key = aliases.get(scope_key, scope_key)
        if scope_key not in SCOPE_LABEL:
            return ToolResult(False, "可以设置的范围有：线上交易、境外交易、小额免密。")
        value = _as_bool(enable)
        attr = {"online": "online_enabled", "overseas": "overseas_enabled", "contactless": "contactless_enabled"}[scope_key]
        before = getattr(target, attr)
        setattr(target, attr, value)
        ctx.store.touch(target)
        action = "已开启" if value else "已关闭"
        lines = [
            f"{KIND_LABEL.get(target.kind, target.kind)}（尾号{target.tail}）{SCOPE_LABEL[scope_key]}{action}",
            f"  变更前：{'开启' if before else '关闭'} → 变更后：{'开启' if value else '关闭'}（即刻生效）",
        ]
        if before == value:
            lines.append("  说明：该开关此前就是这个状态，本次没有产生实际变更。")
        if scope_key == "overseas" and not value:
            lines.append("  提示：关闭境外交易可以有效防范境外盗刷，出国前记得再打开。")
        return ToolResult(
            True,
            "\n".join(lines),
            data={"card_id": target.card_id, "scope": scope_key, "enable": value},
            summary=f"{SCOPE_LABEL[scope_key]}{action}",
        )


class CardFreezeTool(Tool):
    """冻结 / 解冻卡片。"""

    name = "card.freeze"
    category = "卡片管理"
    description = "冻结卡片以暂停全部交易（可随时解冻）"
    risk_level = "high"
    requires_confirmation = True
    params = [
        ToolParam("card", "string", False, "卡片，如 信用卡/尾号8877"),
        ToolParam("reason", "string", False, "冻结原因"),
    ]
    examples = ["把信用卡冻结", "我的卡好像被盗刷了，先冻结"]

    def run(self, ctx: ToolContext, card: str = "", reason: str = "") -> ToolResult:  # type: ignore[override]
        target = _find_card(ctx, card)
        if target is None:
            return ToolResult(False, "没有找到对应的卡片。")
        if target.status == "frozen":
            return ToolResult(True, f"{KIND_LABEL.get(target.kind, target.kind)}（尾号{target.tail}）已经是冻结状态。",
                              summary="卡片已处于冻结状态")
        target.status = "frozen"
        ctx.store.touch(target)
        ctx.store.log("card.freeze", card_id=target.card_id, reason=reason)
        lines = [
            f"已冻结{KIND_LABEL.get(target.kind, target.kind)}（尾号{target.tail}）",
            "  冻结后该卡所有交易都会被拒绝，包括线上、线下和代扣。",
            "  确认安全后可随时说「解冻信用卡」恢复使用。",
        ]
        if reason:
            lines.append(f"  冻结原因：{reason}")
        return ToolResult(True, "\n".join(lines), data={"card_id": target.card_id},
                          summary=f"已冻结尾号{target.tail}的卡片")


class CardUnlockTool(Tool):
    """解冻卡片。"""

    name = "card.unlock"
    category = "卡片管理"
    description = "解除卡片冻结状态，恢复全部交易"
    risk_level = "medium"
    requires_confirmation = True
    params = [
        ToolParam("card", "string", False, "卡片，如 信用卡/尾号8877"),
    ]
    examples = ["解冻信用卡"]

    def run(self, ctx: ToolContext, card: str = "") -> ToolResult:  # type: ignore[override]
        target = _find_card(ctx, card)
        if target is None:
            return ToolResult(False, "没有找到对应的卡片。")
        if target.status == "lost":
            return ToolResult(False, f"尾号{target.tail}的卡片已挂失，无法解冻，请先领取新卡。")
        target.status = "active"
        target.online_enabled = True
        if target.kind != "virtual":
            target.contactless_enabled = True
        ctx.store.touch(target)
        ctx.store.log("card.unlock", card_id=target.card_id)
        return ToolResult(
            True,
            f"已解冻{KIND_LABEL.get(target.kind, target.kind)}（尾号{target.tail}），现已恢复正常交易。",
            data={"card_id": target.card_id},
            summary=f"已解冻尾号{target.tail}的卡片",
        )


class CardReportLostTool(Tool):
    """卡片挂失（不可逆操作，包含补卡流程）。"""

    name = "card.report_lost"
    category = "卡片管理"
    description = "挂失卡片并申请补发新卡，挂失后原卡永久失效"
    risk_level = "high"
    requires_confirmation = True
    params = [
        ToolParam("card", "string", False, "卡片，如 信用卡/尾号8877"),
    ]
    examples = ["我的卡丢了，帮我挂失", "挂失信用卡"]

    def run(self, ctx: ToolContext, card: str = "") -> ToolResult:  # type: ignore[override]
        target = _find_card(ctx, card)
        if target is None:
            return ToolResult(False, "没有找到对应的卡片。")

        target.status = "lost"
        ctx.store.touch(target)
        # 补卡流程：新卡待激活 + 生成一条提醒任务
        new_card = Card(
            card_id=ctx.store.next_id("cards"),
            user_id=ctx.user_id,
            kind=target.kind,
            brand=target.brand,
            tail="****",
            status="pending",
            credit_limit=target.credit_limit,
            daily_limit=target.daily_limit,
            single_limit=target.single_limit,
            issued_at=ctx.clock.iso(),
            note=f"补卡（原卡尾号{target.tail}）",
        )
        ctx.store.cards[new_card.card_id] = new_card
        ctx.store.touch(new_card)
        task = ctx.store.add_task(
            user_id=ctx.user_id,
            kind="reminder",
            run_date=ctx.clock.now().date().isoformat(),
            title=f"补卡进度：{KIND_LABEL.get(target.kind, target.kind)}",
            payload={"old_card": target.card_id, "new_card": new_card.card_id},
        )
        ctx.store.log("card.report_lost", card_id=target.card_id, new_card=new_card.card_id)
        lines = [
            f"已挂失{KIND_LABEL.get(target.kind, target.kind)}（尾号{target.tail}），该卡即刻起无法用于任何交易。",
            f"  补卡申请编号：{new_card.card_id}   预计 3-5 个工作日寄出",
            f"  进度提醒任务：{task.task_id}",
            "  温馨提示：原卡的自动扣款协议（如订阅、还款）会一并转移到新卡。",
        ]
        return ToolResult(
            True,
            "\n".join(lines),
            data={"old_card": target.card_id, "new_card": new_card.card_id},
            summary=f"已挂失尾号{target.tail}的卡片并提交补卡",
        )

