"""卡片管理工具集。

覆盖赛题要求：卡申请、额度调整、交易限制/解锁、卡片挂失。
"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from finagent.data.card_ledger import (
    card_asset_total,
    ensure_card_account,
    hash_card_password,
    linked_account,
    sync_card_from_account,
    update_card_balance,
    update_credit_usage,
    verify_card_password,
)
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
        for card in cards:
            sync_card_from_account(ctx.store, card)
        rows = []
        for c in cards:
            if c.kind == "credit":
                limit = f"额度 {c.credit_limit:,.0f} / 已用 {c.used:,.0f}"
                balance = f"可用 {c.available:,.0f}"
            else:
                limit = f"日限额 {c.daily_limit:,.0f}"
                balance = f"{c.balance:,.2f}（可用 {c.available:,.2f}）"
            switches = "".join(
                [
                    "线上✓ " if c.online_enabled else "线上✗ ",
                    "境外✓ " if c.overseas_enabled else "境外✗ ",
                    "闪付✓" if c.contactless_enabled else "闪付✗",
                ]
            )
            source = source_badge(c.source, c.is_live, c.is_demo)
            rows.append([
                KIND_LABEL.get(c.kind, c.kind), f"尾号{c.tail}",
                STATUS_LABEL.get(c.status, c.status), balance, limit,
                switches, source,
            ])
        lines = [
            f"您名下共有 {len(cards)} 张卡片：",
            render_table(
                ["卡种", "卡号", "状态", "余额/可用", "额度/限额", "交易开关", "来源"],
                rows,
            ),
        ]
        return ToolResult(True, "\n".join(lines), data={"count": len(cards)}, summary=f"共 {len(cards)} 张卡片")


# 演示用 BIN 样本。真实银行的 BIN 会持续新增，生产系统应接入银行或卡组织
# 的实时 BIN 数据库；这里覆盖常见储蓄卡前缀，用于展示格式与发卡行校验。
BANK_CARD_RULES = {
    "工商银行": {
        "prefixes": (
            "621226", "621281", "621558", "621559", "621618", "621722",
            "621723", "622200", "622202", "622203", "622208",
        ),
        "lengths": (16, 17, 18, 19),
    },
    "农业银行": {
        "prefixes": (
            "621282", "621336", "621671", "622821", "622822", "622823",
            "622824", "622825", "622826", "622827", "622828", "622841",
            "622845", "622846", "622848",
        ),
        "lengths": (16, 17, 18, 19),
    },
    "中国银行": {
        "prefixes": (
            "621660", "621661", "621663", "621665", "621666", "621667",
            "621668", "621669", "621672", "621725", "623573",
        ),
        "lengths": (16, 17, 18, 19),
    },
    "建设银行": {
        "prefixes": (
            "621081", "621082", "621466", "621467", "621488", "621700",
            "622280", "622700", "622707", "623211", "623668",
        ),
        "lengths": (16, 17, 18, 19),
    },
    "招商银行": {
        "prefixes": (
            "621483", "621485", "621486", "622575", "622576", "622577",
            "622579", "622580", "622581", "622582", "622588",
        ),
        "lengths": (16, 17, 18, 19),
    },
    "交通银行": {
        "prefixes": (
            "621002", "621069", "621436", "622260", "622261", "622262",
            "622269", "622272", "622273",
        ),
        "lengths": (16, 17, 18, 19),
    },
}

BANK_BINS = {
    prefix: bank
    for bank, rule in BANK_CARD_RULES.items()
    for prefix in rule["prefixes"]
}
SUPPORTED_BANKS = tuple(BANK_CARD_RULES)


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


def detect_bank_by_bin(number: str) -> str:
    """Return the bank for the longest matching BIN sample."""
    matches = [
        (prefix, bank)
        for prefix, bank in BANK_BINS.items()
        if number.startswith(prefix)
    ]
    if not matches:
        return ""
    return max(matches, key=lambda item: len(item[0]))[1]


def validate_phone_number(value: Any) -> dict[str, Any]:
    """Validate a mainland China mobile number in local 11-digit format."""
    raw = str(value or "").strip()
    if not raw:
        return {"ok": False, "code": "empty", "message": "手机号不能为空。"}
    if re.search(r"[^\d\s-]", raw):
        return {
            "ok": False,
            "code": "phone_format",
            "message": "手机号格式不符：只能输入数字，可保留空格或短横线，不要填写 +86 或其他文字。",
        }
    digits = re.sub(r"[\s-]", "", raw)
    if len(digits) != 11:
        return {
            "ok": False,
            "code": "phone_length",
            "message": f"手机号格式不符：当前为 {len(digits)} 位，应为 11 位数字。",
        }
    if not re.fullmatch(r"1[3-9]\d{9}", digits):
        return {
            "ok": False,
            "code": "phone_prefix",
            "message": "手机号格式不符：11 位手机号应以 13-19 开头，请检查首位数字。",
        }
    return {"ok": True, "phone": digits}


def validate_card_number(value: Any, selected_bank: str = "") -> dict[str, Any]:
    """Validate card syntax, UnionPay BIN pattern, issuing bank, and Luhn."""
    raw = str(value or "").strip()
    if not raw:
        return {"ok": False, "code": "empty", "message": "银行卡号不能为空。"}
    if re.search(r"[^\d\s-]", raw):
        return {
            "ok": False,
            "code": "card_format",
            "message": "卡号格式不符：只能包含数字，可使用空格或短横线分隔。",
        }
    number = re.sub(r"[\s-]", "", raw)
    if not (16 <= len(number) <= 19):
        return {
            "ok": False,
            "code": "card_length",
            "message": f"卡号格式不符：当前为 {len(number)} 位，境内银联储蓄卡通常应为 16-19 位。",
        }
    if not number.startswith("62"):
        return {
            "ok": False,
            "code": "card_prefix",
            "message": (
                f"卡号开头格式不符：当前前两位是 {number[:2]}，"
                "境内银联储蓄卡通常以 62 开头，请检查是否漏输、错输或混淆了卡号。"
            ),
        }

    selected = (selected_bank or "").strip()
    if selected and selected not in BANK_CARD_RULES:
        return {
            "ok": False,
            "code": "bank_unsupported",
            "message": f"暂不支持「{selected}」，目前支持：{'、'.join(SUPPORTED_BANKS)}。",
        }

    detected = detect_bank_by_bin(number)
    if selected and detected and selected != detected:
        return {
            "ok": False,
            "code": "bank_mismatch",
            "message": (
                f"卡号与发卡行不匹配：前 6 位 {number[:6]} 属于{detected}，"
                f"但你选择的是{selected}。请核对卡号或重新选择发卡行。"
            ),
        }
    if selected and not detected:
        return {
            "ok": False,
            "code": "bank_bin_unknown",
            "message": (
                f"无法确认发卡行：前 6 位 {number[:6]} 不在{selected}的演示 BIN 样本中。"
                "请核对卡号开头，或选择正确银行。该 BIN 库不是全量真实银行数据。"
            ),
        }
    if not selected and not detected:
        return {
            "ok": False,
            "code": "bank_unknown",
            "message": (
                f"无法从卡号前 6 位 {number[:6]} 识别发卡行。"
                "请同时告诉我是哪家银行，并检查卡号开头是否正确。"
            ),
        }
    if not luhn_ok(number):
        return {
            "ok": False,
            "code": "luhn",
            "message": "卡号校验未通过（Luhn 模 10 校验失败），通常是卡号某一位数字输错。",
        }

    rule = BANK_CARD_RULES[selected or detected]
    if len(number) not in rule["lengths"]:
        return {
            "ok": False,
            "code": "bank_length",
            "message": (
                f"卡号长度与发卡行格式不匹配：{selected or detected}"
                f"的演示规则支持 {'/'.join(str(x) for x in rule['lengths'])} 位，"
                f"当前为 {len(number)} 位。"
            ),
        }
    return {"ok": True, "number": number, "bank": selected or detected}


def evaluate_card_password(value: Any) -> dict[str, Any]:
    """Evaluate a six-digit bank PIN without retaining the clear value."""
    password = str(value or "").strip()
    if not re.fullmatch(r"[0-9]{6}", password):
        return {
            "ok": False,
            "acceptable": False,
            "strength": "不合规",
            "score": 0,
            "message": "取款密码必须为 6 位数字。",
            "reasons": ["长度或字符格式不符"],
        }

    digits = [int(ch) for ch in password]
    unique_count = len(set(digits))
    reasons = []
    score = 40
    common = {
        "000000", "111111", "222222", "333333", "444444", "555555",
        "666666", "777777", "888888", "999999", "123456", "654321",
        "112233", "121212", "131313", "888888", "666888",
    }
    if password in common:
        reasons.append("命中常见弱密码")
        score -= 40
    if unique_count == 1:
        reasons.append("六位数字完全相同")
        score -= 35
    elif password[:3] * 2 == password or password[:2] * 3 == password:
        reasons.append("由短数字重复拼接")
        score -= 25

    differences = [digits[i + 1] - digits[i] for i in range(5)]
    if all(diff == 1 for diff in differences):
        reasons.append("连续递增数字")
        score -= 30
    elif all(diff == -1 for diff in differences):
        reasons.append("连续递减数字")
        score -= 30

    if unique_count >= 4:
        score += 15
    if unique_count >= 5:
        score += 10
    if len({tuple(digits[i:i + 2]) for i in range(5)}) >= 4:
        score += 5
    score = max(0, min(100, score))
    if score >= 70:
        strength = "强"
    elif score >= 50:
        strength = "中"
    else:
        strength = "弱"
    if not reasons:
        reasons.append("未命中常见密码、连续数字或简单重复模式")
    return {
        "ok": True,
        "acceptable": strength != "弱",
        "strength": strength,
        "score": score,
        "message": f"密码强度：{strength}（{score}/100）。" + "；".join(reasons),
        "reasons": reasons,
    }


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
        ToolParam("phone", "string", True, "银行预留手机号（11 位）"),
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
        card_check = validate_card_number(card_no, bank)
        if not card_check["ok"]:
            return ToolResult(
                False,
                card_check["message"],
                data={"reason": card_check["code"]},
            )
        phone_check = validate_phone_number(phone)
        if not phone_check["ok"]:
            return ToolResult(
                False,
                phone_check["message"],
                data={"reason": phone_check["code"]},
            )
        number = card_check["number"]
        bank_name = card_check["bank"]
        phone_digits = phone_check["phone"]
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
            balance=0.0,
            daily_limit=20000.0,
            single_limit=5000.0,
            payment_password_hash=hash_card_password(str(password)),
            overseas_enabled=False,
            issued_at=ctx.clock.iso(),
            note=f"{bank_name}储蓄卡（绑定）",
        )
        ctx.store.cards[card.card_id] = card
        ctx.store.touch(card)
        ensure_card_account(
            ctx.store,
            card,
            account_kind="current",
            account_name=f"{bank_name}储蓄卡账户",
        )
        ctx.store.log("card.bind", card_id=card.card_id, bank=bank_name, tail=tail)
        lines = [
            f"绑定成功：{bank_name} 储蓄卡（尾号 {tail}）",
            f"  卡号：**** **** **** {tail}（仅保存后四位，完整卡号与密码不入库）",
            f"  预留手机号：{phone_digits[:3]}****{phone_digits[-4:]}",
            f"  当前余额：{card.balance:,.2f} 元（演示环境新绑定卡初始余额为 0）",
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
    description = "申请储蓄卡、信用卡、虚拟卡或附属卡；不同卡种使用不同资料和审批流程"
    risk_level = "medium"
    params = [
        ToolParam(
            "kind", "string", False, "卡类型",
            enum=["debit", "credit", "virtual", "supplementary"],
        ),
        ToolParam("bank", "string", False, "发卡行，如 中国银行/工商银行"),
        ToolParam("phone", "string", False, "银行预留手机号（11 位）"),
        ToolParam("account_class", "string", False, "储蓄卡账户类型：I类户/II类户"),
        ToolParam("branch", "string", False, "开户城市或网点"),
        ToolParam("employment", "string", False, "就业情况"),
        ToolParam("monthly_income", "number", False, "税后月收入"),
        ToolParam("existing_debt", "number", False, "每月现有负债还款额"),
        ToolParam("credit_purpose", "string", False, "信用卡主要用途"),
        ToolParam("statement_mode", "string", False, "流水核验方式：uploaded/demo"),
        ToolParam("consent", "boolean", False, "是否同意模拟信用评估"),
        ToolParam("card_password", "string", False, "新储蓄卡 6 位取款密码"),
        ToolParam("card_password_confirm", "string", False, "再次输入取款密码"),
        ToolParam("limit_request", "number", False, "期望额度（元）"),
        ToolParam("note", "string", False, "备注，如 给老婆办一张附属卡"),
    ]
    examples = ["开一张工商银行的储蓄卡", "申请一张中国银行信用卡", "办一张虚拟卡网购用"]

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        kind: str = "debit",
        bank: str = "",
        phone: Any = "",
        account_class: str = "",
        branch: str = "",
        employment: str = "",
        monthly_income: Any = None,
        existing_debt: Any = None,
        credit_purpose: str = "",
        statement_mode: str = "",
        consent: Any = False,
        card_password: Any = "",
        card_password_confirm: Any = "",
        limit_request: Any = None,
        note: str = "",
    ) -> ToolResult:
        kind = (kind or "debit").strip().lower()
        if kind not in KIND_LABEL:
            return ToolResult(False, f"无法识别卡种「{kind}」，请选择储蓄卡、信用卡、虚拟卡或附属卡。")
        user = ctx.user
        if not user.verified:
            return ToolResult(False, "申请卡片前需要先完成实名认证。")

        bank_name = bank or "银联"
        if kind == "debit":
            return self._open_debit_card(
                ctx,
                bank_name=bank_name,
                phone=phone,
                account_class=account_class,
                branch=branch,
                card_password=card_password,
                card_password_confirm=card_password_confirm,
                note=note,
            )
        if kind == "credit":
            return self._apply_credit_card(
                ctx,
                bank_name=bank_name,
                phone=phone,
                employment=employment,
                monthly_income=monthly_income,
                existing_debt=existing_debt,
                credit_purpose=credit_purpose,
                statement_mode=statement_mode,
                consent=consent,
                limit_request=limit_request,
                note=note,
            )

        defaults = {"virtual": 5_000.0, "supplementary": 10_000.0}
        try:
            limit = float(limit_request) if limit_request is not None else defaults[kind]
        except (TypeError, ValueError):
            limit = defaults[kind]
        card = Card(
            card_id=ctx.store.next_id("cards"),
            user_id=user.user_id,
            kind=kind,
            brand=bank_name,
            tail="****",
            status="pending",
            credit_limit=limit if kind == "supplementary" else 0.0,
            daily_limit=min(limit, 50_000.0) if kind != "credit" else limit,
            single_limit=min(limit / 5, 5_000.0),
            overseas_enabled=False,
            contactless_enabled=kind != "virtual",
            issued_at=ctx.clock.iso(),
            note=note or "新申请",
        )
        ctx.store.cards[card.card_id] = card
        ctx.store.touch(card)
        ensure_card_account(
            ctx.store,
            card,
            account_kind="current" if kind == "virtual" else "credit",
            account_name=f"{bank_name}{KIND_LABEL[kind]}账户",
        )
        task = ctx.store.add_task(
            user_id=user.user_id,
            kind="reminder",
            run_date=(ctx.clock.now().date().isoformat()),
            title=f"{KIND_LABEL[kind]}审批中（申请单 {card.card_id}）",
            payload={"card_id": card.card_id},
        )
        lines = [
            f"申请已提交：{bank_name} {KIND_LABEL[kind]}"
            + (f"（期望额度 {limit:,.0f} 元）" if limit else ""),
            f"  申请编号：{card.card_id}   当前状态：待审批",
            "  预计审批时间：几分钟至 1 个工作日（演示环境）。",
            "  系统会在状态变化时提醒；审批通过后可一键激活。",
        ]
        if note:
            lines.append(f"  备注：{note}")
        return ToolResult(
            True,
            "\n".join(lines),
            data={"card_id": card.card_id, "task_id": task.task_id, "kind": kind},
            summary=f"已提交{KIND_LABEL[kind]}申请",
        )

    @staticmethod
    def _open_debit_card(
        ctx: ToolContext,
        *,
        bank_name: str,
        phone: Any,
        account_class: str,
        branch: str,
        card_password: Any,
        card_password_confirm: Any,
        note: str,
    ) -> ToolResult:
        phone_check = validate_phone_number(phone)
        if not phone_check["ok"]:
            return ToolResult(False, phone_check["message"])
        if account_class not in {"I类户", "II类户"}:
            return ToolResult(False, "请选择储蓄卡账户类型：I类户或II类户。")
        if not branch.strip():
            return ToolResult(False, "请提供开户城市或网点。")
        if str(card_password) != str(card_password_confirm):
            return ToolResult(False, "两次输入的取款密码不一致，开户未执行。")
        password_check = evaluate_card_password(card_password)
        if not password_check["ok"] or not password_check["acceptable"]:
            return ToolResult(
                False,
                password_check["message"] + " 密码强度不足，开户未执行。",
                data={"reason": "weak_password"},
            )

        card_id = ctx.store.next_id("cards")
        sequence = re.sub(r"\D", "", card_id)[-4:].zfill(4)
        card = Card(
            card_id=card_id,
            user_id=ctx.user_id,
            kind="debit",
            brand=bank_name,
            tail=sequence,
            status="active",
            balance=0.0,
            daily_limit=20_000.0,
            single_limit=5_000.0,
            payment_password_hash=hash_card_password(str(card_password)),
            online_enabled=True,
            overseas_enabled=False,
            contactless_enabled=True,
            issued_at=ctx.clock.iso(),
            note=note or f"{bank_name}{account_class}储蓄卡（开户）",
        )
        ctx.store.cards[card.card_id] = card
        ctx.store.touch(card)
        ensure_card_account(
            ctx.store,
            card,
            account_kind="current",
            account_name=f"{bank_name}{account_class}账户",
        )
        ctx.store.log(
            "card.open_debit",
            card_id=card.card_id,
            bank=bank_name,
            account_class=account_class,
            branch=branch,
        )
        phone_digits = phone_check["phone"]
        lines = [
            "【储蓄卡开户成功】",
            f"  发卡行：{bank_name}",
            f"  卡种：{account_class}储蓄卡（演示卡，尾号 {card.tail}）",
            f"  开户城市/网点：{branch}",
            f"  预留手机号：{phone_digits[:3]}****{phone_digits[-4:]}",
            f"  取款密码：已设置并二次确认（强度 {password_check['strength']}，"
            f"{password_check['score']}/100；明文密码未保存）",
            f"  当前余额：{card.balance:,.2f} 元",
            "  当前状态：正常（仅为本原型生成的模拟卡）",
            f"  单笔限额 {card.single_limit:,.0f} 元，日限额 {card.daily_limit:,.0f} 元",
            "  已同步到「资料与卡片设置」。",
        ]
        return ToolResult(
            True,
            "\n".join(lines),
            data={
                "card_id": card.card_id,
                "bank": bank_name,
                "tail": card.tail,
                "kind": "debit",
                "status": card.status,
                "password_strength": password_check["strength"],
                "password_score": password_check["score"],
            },
            summary=f"已开立 {bank_name}{account_class}储蓄卡",
        )

    @staticmethod
    def _apply_credit_card(
        ctx: ToolContext,
        *,
        bank_name: str,
        phone: Any,
        employment: str,
        monthly_income: Any,
        existing_debt: Any,
        credit_purpose: str,
        statement_mode: str,
        consent: Any,
        limit_request: Any,
        note: str,
    ) -> ToolResult:
        phone_check = validate_phone_number(phone)
        if not phone_check["ok"]:
            return ToolResult(False, phone_check["message"])
        if not str(employment or "").strip():
            return ToolResult(False, "请补充就业情况。")
        if not str(credit_purpose or "").strip():
            return ToolResult(False, "请补充信用卡主要用途。")
        if consent is not True:
            return ToolResult(False, "未获得模拟信用评估授权，信用卡申请未提交。")
        try:
            income = float(monthly_income)
            debt = float(existing_debt)
            requested = float(limit_request)
        except (TypeError, ValueError):
            return ToolResult(False, "月收入、每月负债和期望额度必须是数字。")
        if income <= 0:
            return ToolResult(False, "税后月收入必须大于 0。")
        if debt < 0:
            return ToolResult(False, "每月负债不能为负数。")
        if requested <= 0:
            return ToolResult(False, "期望额度必须大于 0。")

        statement = CardApplyTool._credit_statement_profile(ctx, statement_mode)
        if statement is None:
            return ToolResult(
                False,
                "请选择有效的流水核验方式：上传当前会话流水，或明确选择演示流水。",
            )
        debt_ratio = debt / income
        score = 55
        if income >= 10_000:
            score += 15
        if income >= 20_000:
            score += 10
        if debt_ratio < 0.3:
            score += 15
        elif debt_ratio < 0.5:
            score += 8
        else:
            score -= 20
        if requested <= income * 0.5:
            score += 10
        else:
            score -= 12
        score += statement["score_adjust"]
        score = max(0, min(100, score))

        if score >= 70:
            decision = "通过"
            status_text = "模拟预审通过，待激活"
        elif score >= 50:
            decision = "转人工复核"
            status_text = "待人工复核"
        else:
            decision = "未通过"
            status_text = "模拟预审未通过"

        reference_limit = min(requested, max(1_000.0, income * 0.5))
        if decision == "转人工复核":
            reference_limit = min(reference_limit, 10_000.0)
        review_date = (ctx.clock.now() + timedelta(days=1)).date().isoformat()
        phone_digits = phone_check["phone"]

        card_id = ""
        if decision != "未通过":
            card_id = ctx.store.next_id("cards")
            application_note = (
                f"{note or '信用卡申请'}｜就业：{employment}｜"
                f"月收入：{income:,.0f}｜月负债：{debt:,.0f}｜"
                f"用途：{credit_purpose}｜流水：{statement['label']}｜预审：{decision}"
            )
            card = Card(
                card_id=card_id,
                user_id=ctx.user_id,
                kind="credit",
                brand=bank_name,
                tail="****",
                status="pending",
                balance=0.0,
                credit_limit=reference_limit,
                daily_limit=reference_limit,
                single_limit=min(reference_limit / 5, 5_000.0),
                online_enabled=True,
                overseas_enabled=False,
                contactless_enabled=True,
                issued_at=ctx.clock.iso(),
                note=application_note,
            )
            ctx.store.cards[card.card_id] = card
            ctx.store.touch(card)
            ensure_card_account(
                ctx.store,
                card,
                account_kind="credit",
                account_name=f"{bank_name}信用卡账户",
            )
            task = ctx.store.add_task(
                user_id=ctx.user_id,
                kind="reminder",
                run_date=review_date,
                title=f"{bank_name}信用卡终审进度（申请单 {card.card_id}）",
                payload={"card_id": card.card_id, "status": "pending_review"},
            )
        else:
            task = ctx.store.add_task(
                user_id=ctx.user_id,
                kind="reminder",
                run_date=review_date,
                title=f"{bank_name}信用卡申请结果通知",
                payload={"status": "preliminary_rejected"},
            )
            card_id = ctx.store.next_id("card_applications")

        ctx.store.log(
            "card.apply_credit",
            application_id=card_id,
            bank=bank_name,
            decision=decision,
            score=score,
        )
        lines = [
            "【信用卡申请 · 模拟预审完成】",
            f"  发卡行：{bank_name}",
            f"  申请编号：{card_id}",
            f"  模拟预审结果：{decision}（评分 {score}/100）",
            f"  参考额度：{reference_limit:,.0f} 元（最终结果以模拟终审为准）",
            f"  当前状态：{status_text}",
            f"  评估依据：月收入 {income:,.0f} 元、月负债 {debt:,.0f} 元、"
            f"负债率 {debt_ratio * 100:.1f}%、期望额度 {requested:,.0f} 元",
            f"  主要用途：{credit_purpose}",
            f"  流水核验：{statement['label']}，覆盖 {statement['month_count']} 个月、"
            f"{statement['count']} 笔；月均支出 {statement['avg_monthly_expense']:,.2f} 元",
            f"  主要消费类别：{statement['top_categories']}",
            f"  流水风险：命中 {statement['anomaly_count']} 项异常特征；"
            f"流水质量：{statement['quality']}",
            f"  预计终审时间：1-3 个工作日；演示环境预计 {review_date} 更新进度",
            f"  预留手机号：{phone_digits[:3]}****{phone_digits[-4:]}",
            "  说明：这是模拟评分，不会查询真实征信，也不代表真实银行审批结果。",
        ]
        return ToolResult(
            True,
            "\n".join(lines),
            data={
                "application_id": card_id,
                "card_id": card_id if decision != "未通过" else "",
                "task_id": task.task_id,
                "kind": "credit",
                "decision": decision,
                "score": score,
                "reference_limit": reference_limit,
                "statement_mode": statement["mode"],
                "statement_count": statement["count"],
                "statement_anomaly_count": statement["anomaly_count"],
                "statement_quality": statement["quality"],
            },
            summary=f"信用卡模拟预审结果：{decision}",
        )

    @staticmethod
    def _credit_statement_profile(ctx: ToolContext, mode: str) -> dict[str, Any] | None:
        """Build a credit-review summary from uploaded or explicitly simulated rows."""
        if mode not in {"uploaded", "demo"}:
            return None
        transactions = ctx.store.transactions_of(ctx.user_id)
        imported = [
            txn for txn in transactions
            if "receipt_import" in txn.tags or txn.source.startswith("upload:")
        ]
        if mode == "uploaded":
            rows = imported
            label = "已上传流水"
        else:
            cutoff = (ctx.clock.now() - timedelta(days=183)).isoformat()
            rows = [
                txn for txn in transactions
                if txn.timestamp >= cutoff and txn not in imported
            ]
            label = "演示流水（模拟数据）"
        rows = [txn for txn in rows if txn.status == "success"]
        if not rows:
            return None

        expenses = [txn for txn in rows if txn.amount < 0]
        incomes = [txn for txn in rows if txn.amount > 0]
        expense_total = sum(abs(txn.amount) for txn in expenses)
        income_total = sum(txn.amount for txn in incomes)
        months = {txn.timestamp[:7] for txn in rows if len(txn.timestamp) >= 7}
        month_count = max(1, len(months))
        avg_monthly_expense = expense_total / month_count
        by_category: dict[str, float] = {}
        for txn in expenses:
            by_category[txn.category] = by_category.get(txn.category, 0.0) + abs(txn.amount)
        top = sorted(by_category.items(), key=lambda item: item[1], reverse=True)[:3]
        top_categories = "、".join(
            f"{name} {amount:,.0f} 元" for name, amount in top
        ) or "无支出记录"
        anomalies = ctx.risk.detect_anomalies(
            ctx.user_id,
            txns=expenses,
            include_subscriptions=False,
        )

        score_adjust = 0
        if mode == "uploaded":
            score_adjust += 5
        if len(rows) >= 20:
            score_adjust += 8
        elif len(rows) >= 5:
            score_adjust += 3
        else:
            score_adjust -= 10
        if month_count >= 3:
            score_adjust += 4
        elif month_count >= 2:
            score_adjust += 2
        expense_ratio = expense_total / max(income_total, 1.0)
        if expense_ratio <= 0.7:
            score_adjust += 6
        elif expense_ratio <= 1.0:
            score_adjust += 2
        else:
            score_adjust -= 8
        if len(anomalies) >= 5:
            score_adjust -= 12
        elif anomalies:
            score_adjust -= 4

        quality = "良好" if score_adjust >= 8 else ("一般" if score_adjust >= 0 else "样本不足")
        return {
            "mode": mode,
            "label": label,
            "count": len(rows),
            "month_count": month_count,
            "expense_total": round(expense_total, 2),
            "income_total": round(income_total, 2),
            "avg_monthly_expense": round(avg_monthly_expense, 2),
            "top_categories": top_categories,
            "anomaly_count": len(anomalies),
            "score_adjust": score_adjust,
            "quality": quality,
        }


class CreditCardRepayTool(Tool):
    """Repay a credit card from one of the customer's deposit cards."""

    name = "card.repay"
    category = "卡片管理"
    description = "使用本人借记卡偿还信用卡账单，需要付款卡交易密码"
    risk_level = "medium"
    requires_confirmation = True
    params = [
        ToolParam("card", "string", True, "待还款信用卡，如 信用卡/尾号8877"),
        ToolParam("from_card", "string", True, "付款借记卡，如 工资卡/尾号6222"),
        ToolParam("amount", "number", True, "还款金额（元）"),
        ToolParam("payment_password", "string", True, "付款卡 6 位交易密码"),
    ]
    examples = ["用工资卡还信用卡3000元", "从尾号6222还信用卡1000块"]

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        card: str,
        from_card: str,
        amount: Any,
        payment_password: str,
    ) -> ToolResult:
        from finagent.tools.transfer_tools import resolve_payment_card

        try:
            value = round(float(amount), 2)
        except (TypeError, ValueError):
            return ToolResult(False, "还款金额无法识别。")
        if value <= 0:
            return ToolResult(False, "还款金额必须大于 0。")
        target = _find_card(ctx, card)
        source = resolve_payment_card(ctx, from_card)
        if target is None or target.kind != "credit":
            return ToolResult(False, "没有找到待还款的信用卡。")
        if source is None:
            return ToolResult(False, "没有找到可用的付款借记卡。")
        if target.status != "active":
            return ToolResult(False, f"信用卡当前状态为 {STATUS_LABEL.get(target.status, target.status)}，不能还款。")
        if not verify_card_password(source, payment_password):
            return ToolResult(False, "付款卡交易密码验证失败，信用卡还款未执行。")
        if source.available < value:
            return ToolResult(
                False,
                f"付款卡余额不足：可用 {source.available:,.2f} 元，需要 {value:,.2f} 元。",
            )
        if value > target.used:
            return ToolResult(
                False,
                f"还款金额超过信用卡当前欠款 {target.used:,.2f} 元。",
            )
        decision = ctx.risk.check_operation(
            "card.repay",
            {"amount": value, "account": source.card_id},
            ctx.user,
            account=source,
        )
        if decision.blocked:
            return ToolResult(False, "信用卡还款未执行：" + decision.summary())

        source_before = source.balance
        used_before = target.used
        update_card_balance(ctx.store, source, source_before - value)
        update_credit_usage(ctx.store, target, used_before - value)
        txn = ctx.store.add_transaction(
            user_id=ctx.user_id,
            account_id=source.account_id,
            amount=-value,
            merchant=f"{target.brand}信用卡还款",
            category="信用卡还款",
            channel="本行还款",
            counterparty=f"信用卡尾号{target.tail}",
            note="信用卡账单还款",
            tags=["credit_card_repayment", f"card:{target.card_id}"],
        )
        asset_total_after = card_asset_total(ctx.store, ctx.user_id)
        lines = [
            f"信用卡还款成功：{value:,.2f} 元",
            f"  付款卡：{source.brand}（尾号 {source.tail}） "
            f"{source_before:,.2f} → {source.balance:,.2f} 元",
            f"  信用卡：尾号 {target.tail} 已用额度 "
            f"{used_before:,.2f} → {target.used:,.2f} 元",
            f"  信用卡可用额度：{target.available:,.2f} 元",
            f"  还款流水：{txn.txn_id}",
            f"  银行卡资产总额：{asset_total_after:,.2f} 元",
        ]
        if decision.reasons:
            lines.append("  风控提示：" + decision.summary())
        return ToolResult(
            True,
            "\n".join(lines),
            data={
                "credit_card_id": target.card_id,
                "payment_card_id": source.card_id,
                "amount": value,
                "credit_used_after": target.used,
                "payment_card_balance_after": source.balance,
                "txn_id": txn.txn_id,
                "asset_total_after": asset_total_after,
            },
            summary=f"已用 {source.brand} 尾号 {source.tail} 还款 {value:,.2f} 元",
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
            account_id=target.account_id,
            brand=target.brand,
            tail="****",
            status="pending",
            balance=target.balance,
            credit_limit=target.credit_limit,
            used=target.used,
            payment_password_hash=target.payment_password_hash,
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

