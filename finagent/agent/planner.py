"""任务规划器。

输入"意图 + 槽位"，输出"要调用哪些工具、每一步的参数是什么"。
大多数意图是单步的；但下面这些需要真正的多步规划：
    生日惊喜   -> [锁定资金, 预约鲜花, 预约蛋糕]
    卡片挂失   -> [先冻结止损, 再挂失补卡]
规划器只关心"做什么"，不关心"能不能做"（风控）和"怎么执行"（调度器）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Tuple

from finagent.agent.registry import ToolRegistry
from finagent.nlu.intent_schema import Intent
from finagent.tools.base import ToolContext
from finagent.tools.card_tools import BANK_BINS, evaluate_card_password
from finagent.tools.crossscene_tools import lookup_person
from finagent.data.providers import MerchantOfferProvider
from finagent.tools.transfer_tools import channel_account, resolve_account
from finagent.utils import parse_date


@dataclass
class PlanStep:
    """一个工具调用步骤。"""

    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    note: str = ""


@dataclass
class Plan:
    """一份执行计划。"""

    intent: str
    steps: List[PlanStep] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    question: str = ""
    description: str = ""
    slots: Dict[str, Any] = field(default_factory=dict)

    def is_ready(self) -> bool:
        return not self.missing and bool(self.steps)

    def tools(self) -> List[str]:
        return [step.tool for step in self.steps]


# 意图 -> [(工具名, {工具参数: 槽位名})]
SIMPLE_PLANS: Dict[str, List[Tuple[str, Dict[str, str]]]] = {
    "transfer.send": [("transfer.send", {"payee": "payee", "amount": "amount", "channel": "channel",
                                         "note": "note", "account": "account"})],
    "transfer.schedule": [("transfer.schedule", {"payee": "payee", "amount": "amount", "when": "when",
                                                 "channel": "channel",
                                                 "repeat": "repeat", "note": "note"})],
    "transfer.split_aa": [("transfer.split_aa", {"total": "total", "participants": "participants",
                                                 "channel": "channel",
                                                 "note": "note"})],
    "transfer.list_scheduled": [("transfer.list_scheduled", {})],
    "bill.analyze": [("bill.analyze", {"period": "period", "category": "category"})],
    "bill.supported_formats": [("bill.supported_formats", {})],
    "bill.anomaly": [("bill.anomaly", {"period": "period"})],
    "bill.report": [("bill.report", {"period": "period", "mode": "mode"})],
    "bill.search": [("bill.search", {"keyword": "keyword", "period": "period"})],
    "bill.scan_receipt": [("bill.scan_receipt", {"image": "image"})],
    "wealth.risk_assess": [("wealth.risk_assess", {})],
    "wealth.recommend": [("wealth.recommend", {"risk_level": "risk_level", "amount": "amount",
                                               "horizon": "horizon", "asset_class": "asset_class",
                                               "purpose": "purpose"})],
    "wealth.subscribe": [("wealth.subscribe", {"product": "product", "amount": "amount",
                                               "account": "account"})],
    "wealth.redeem": [("wealth.redeem", {"product": "product", "amount": "amount"})],
    "wealth.portfolio": [("wealth.portfolio", {})],
    "card.list": [("card.list", {})],
    "card.apply": [("card.apply", {"kind": "kind", "bank": "bank",
                                   "limit_request": "limit_request", "note": "note"})],
    "card.bind": [("card.bind", {"card_no": "card_no", "password": "password",
                                 "bank": "bank", "phone": "phone"})],
    "card.limit_adjust": [("card.limit_adjust", {"card": "card", "new_limit": "new_limit"})],
    "card.control": [("card.control", {"card": "card", "scope": "scope", "enable": "enable"})],
    "card.freeze": [("card.freeze", {"card": "card", "reason": "reason"})],
    "card.unlock": [("card.unlock", {"card": "card"})],
    "card.report_lost": [("card.freeze", {"card": "card"}),
                         ("card.report_lost", {"card": "card"})],
    "subscription.detect": [("subscription.detect", {})],
    "subscription.list": [("subscription.list", {})],
    "subscription.cancel": [("subscription.cancel", {"merchant": "merchant"})],
    "subscription.resume": [("subscription.resume", {"merchant": "merchant"})],
    "subscription.change_cycle": [
        ("subscription.change_cycle", {
            "merchant": "merchant",
            "term_months": "term_months",
            "renewal_price": "renewal_price",
            "auto_renew": "auto_renew",
        }),
    ],
    "membership.purchase": [("membership.purchase", {"items": "items"})],
    "subscription.remind": [("subscription.remind", {"days": "days"})],
    "goal.create": [("goal.create", {"name": "name", "target_amount": "target_amount",
                                     "deadline": "deadline", "scenario": "scenario"})],
    "goal.list": [("goal.list", {})],
    "account.balance": [("account.balance", {})],
    "profile.show": [("profile.show", {})],
    "help": [("help", {})],
    "agent.about": [("agent.about", {})],
    "crossscene.daily_brief": [("crossscene.daily_brief", {})],
    "crossscene.gift_plan": [
        ("crossscene.schedule_gift", {
            "person": "person",
            "item": "item",
            "date": "date",
            "budget": "budget",
        }),
    ],
}

DEFAULT_QUESTIONS = {
    "payee": "请问要转给谁？可以给我姓名、备注昵称或手机号。",
    "amount": "请问金额是多少？例如「500」或「2000」元。",
    "channel": "这次用哪种方式收款？可以说：手机号（数字人民币）、微信、支付宝 或 银行卡。",
    "when": "请问什么时候执行？例如「明天」「下个月5号」「2026-12-01」。",
    "total": "请问这次一共消费了多少钱？",
    "participants": "请问都有谁一起参与？用顿号隔开就行，例如「我、王强、李思琪」。",
    "keyword": "请问要查哪家商户或哪类消费？例如「星巴克」「咖啡」。",
    "image": "请把账单截图或账单文件的路径发给我，例如 samples/bill_sample.png。",
    "product": "请问要操作哪只产品？可以说产品名，例如「稳利债券A」。",
    "card": "请问是哪张卡？可以说「信用卡」「虚拟卡」或卡号后四位。",
    "card_no": "请把要绑定的银行卡号发给我（16-19 位）。",
    "password": "请输入该卡的取款密码（演示环境固定 123456）。",
    "phone": "请提供这张卡在银行预留的 11 位手机号。",
    "kind": "您要申请储蓄卡、信用卡、虚拟卡还是附属卡？",
    "bank": "请选择发卡行，例如工商银行、农业银行、中国银行、建设银行、招商银行或交通银行。",
    "account_class": "请选择储蓄卡账户类型：一类户（I 类）或二类户（II 类）。",
    "branch": "请提供开户城市或网点，例如「深圳」。",
    "employment": "请说明您的就业情况，例如「互联网公司产品经理」「个体经营」「学生」。",
    "monthly_income": "请提供税后月收入，例如「25000」或「2.5万」。",
    "existing_debt": "请提供每月现有负债还款额；没有负债可回复「0」。",
    "limit_request": "请填写期望信用额度，例如「30000」元。",
    "credit_purpose": "请说明信用卡主要用途，例如「日常消费」「旅行」「装修」。",
    "statement_mode": (
        "请先在回形针上传消费流水并确认导入，完成后回复「已上传」；"
        "也可以回复「使用演示流水」进行模拟核验。"
    ),
    "card_password": (
        "请设置 6 位数字取款密码（仅用于原型开户演示，请勿填写真实密码）。"
        "不要使用 123456、生日或连续数字。"
    ),
    "card_password_confirm": "请再次输入同一密码进行确认。",
    "consent": "是否同意本次演示进行模拟信用评估？请回复「同意」或「不同意」。",
    "new_limit": "请问要调整到多少额度？",
    "merchant": "请问要取消哪个订阅？例如「腾讯视频VIP」。",
    "person": "请问是要为谁准备？例如「老婆」「妈妈」。",
    "budget": "请问预算多少？例如「1000」元。",
    "name": "请问这个目标叫什么名字？例如「旅行基金」。",
    "target_amount": "请问目标金额是多少？",
    "birthday": "请问 ta 的生日是哪一天？例如「11月20日」。",
    "date": "请问希望哪一天送达（日期）？例如「11月20日」或「下周六」。",
    "item": "请问想送什么礼物？例如「鲜花」「蛋糕」或「伴手礼」。",
}

NUMERIC_SLOTS = {
    "amount", "total", "target_amount", "new_limit", "budget", "limit_request",
    "days", "risk_level", "monthly_income", "existing_debt",
}
DATE_SLOTS = {"when", "deadline", "date", "birthday"}
CHANNEL_LABELS = {
    "phone": "手机号（数字人民币）",
    "wechat": "微信",
    "alipay": "支付宝",
    "bank": "银行卡",
}
CARD_KIND_LABELS = {
    "credit": "信用卡",
    "debit": "借记卡",
    "virtual": "虚拟卡",
    "supplementary": "附属卡",
}

CARD_APPLY_REQUIREMENTS = {
    "debit": (
        "bank", "phone", "account_class", "branch",
        "card_password", "card_password_confirm",
    ),
    "credit": (
        "bank", "phone", "employment", "monthly_income", "existing_debt",
        "limit_request", "credit_purpose", "statement_mode", "consent",
    ),
    "virtual": ("bank",),
    "supplementary": ("bank", "person", "limit_request"),
}

CARD_APPLY_FIELD_LABELS = {
    "bank": "发卡行",
    "phone": "预留手机号",
    "account_class": "账户类型",
    "branch": "开户城市/网点",
    "employment": "就业情况",
    "monthly_income": "税后月收入",
    "existing_debt": "每月负债",
    "limit_request": "期望额度",
    "credit_purpose": "主要用途",
    "statement_mode": "消费流水核验",
    "card_password": "取款密码",
    "card_password_confirm": "密码二次确认",
    "consent": "模拟信用评估授权",
    "person": "附属卡使用人",
}

# Writes that depend on inferred, resolved, or defaulted business data must be
# reviewed as a whole before any tool is allowed to mutate state.
DATA_CONFIRM_INTENTS = {
    "transfer.send",
    "transfer.schedule",
    "transfer.split_aa",
    "wealth.subscribe",
    "wealth.redeem",
    "card.apply",
    "card.bind",
    "card.limit_adjust",
    "card.control",
    "card.freeze",
    "card.unlock",
    "card.report_lost",
    "subscription.cancel",
    "subscription.resume",
    "subscription.change_cycle",
    "membership.purchase",
    "goal.create",
    "crossscene.birthday_plan",
    "crossscene.gift_plan",
}


class Planner:
    """把意图翻译成可执行的工具调用计划。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def plan(self, intent: Intent, ctx: ToolContext) -> Plan:
        if intent.name == "crossscene.birthday_plan":
            return self._birthday_plan(intent, ctx)
        if intent.name == "crossscene.gift_plan":
            return self._gift_plan(intent, ctx)
        if intent.name == "card.apply":
            return self._card_apply_plan(intent, ctx)
        if intent.name not in SIMPLE_PLANS:
            return Plan(intent=intent.name, slots=dict(intent.slots))
        template = SIMPLE_PLANS[intent.name]
        slots = dict(intent.slots)
        plan = Plan(intent=intent.name, slots=slots)
        missing = self._missing_slots(template, slots)
        if missing:
            plan.missing = missing
            plan.question = self._question(missing[0])
            return plan
        plan.steps = self._build_steps(template, slots)
        plan.description = self._describe(plan.steps, ctx)
        return plan

    def _build_steps(self, template: List[Tuple[str, Dict[str, str]]], slots: Dict[str, Any]) -> List[PlanStep]:
        steps: List[PlanStep] = []
        for tool_name, mapping in template:
            args: Dict[str, Any] = {}
            for param, slot in mapping.items():
                if slots.get(slot) not in (None, ""):
                    args[param] = slots[slot]
            steps.append(PlanStep(tool=tool_name, args=args))
        return steps

    def _missing_slots(self, template: List[Tuple[str, Dict[str, str]]], slots: Dict[str, Any]) -> List[str]:
        missing: List[str] = []
        for tool_name, mapping in template:
            tool = self.registry.get(tool_name)
            if tool is None:
                continue
            required = set(tool.required_params())
            for param, slot in mapping.items():
                if param in required and slot not in slots and slot not in missing:
                    missing.append(slot)
            # 二选一型参数：一个都没有时才追问
            if getattr(tool, "required_any", None):
                candidates = [mapping.get(param, param) for param in tool.required_any]
                if not any(candidate in slots for candidate in candidates):
                    slot = candidates[0]
                    if slot not in missing:
                        missing.append(slot)
        return missing

    @staticmethod
    def _question(slot: str) -> str:
        return DEFAULT_QUESTIONS.get(slot, f"请补充一下「{slot}」。")

    def _card_apply_plan(self, intent: Intent, ctx: ToolContext) -> Plan:
        """Build distinct intake flows for debit, credit, and other card types."""
        slots = dict(intent.slots)
        kind = str(slots.get("kind") or "")
        if kind not in CARD_APPLY_REQUIREMENTS:
            plan = Plan(intent=intent.name, slots=slots, missing=["kind"])
            plan.question = "【开卡申请】请先选择卡种：储蓄卡、信用卡、虚拟卡或附属卡。"
            return plan

        missing = [
            slot
            for slot in CARD_APPLY_REQUIREMENTS[kind]
            if slots.get(slot) in (None, "")
        ]
        if missing:
            plan = Plan(intent=intent.name, slots=slots, missing=missing)
            label = "储蓄卡" if kind == "debit" else CARD_KIND_LABELS.get(kind, kind)
            mode = "开户" if kind == "debit" else "申请"
            remaining = "、".join(
                CARD_APPLY_FIELD_LABELS.get(slot, slot)
                for slot in missing
            )
            first_question = (
                "请提供申请该卡使用的 11 位本人手机号。"
                if missing[0] == "phone"
                else self._question(missing[0])
            )
            if missing[0] == "card_password_confirm" and slots.get("card_password"):
                password_check = evaluate_card_password(slots["card_password"])
                first_question = (
                    f"首次输入完成，{password_check['message']}\n"
                    "请再次输入同一密码进行确认。"
                )
            plan.question = (
                f"【{label}{mode}】请继续补充：{remaining}。\n"
                f"{first_question}"
            )
            return plan

        args = {key: value for key, value in slots.items() if key != "note"}
        if slots.get("note"):
            args["note"] = slots["note"]
        step = PlanStep(tool="card.apply", args=args)
        plan = Plan(intent=intent.name, slots=slots, steps=[step])
        plan.description = self._describe_step(step, ctx)
        return plan

    def _birthday_plan(self, intent: Intent, ctx: ToolContext) -> Plan:
        """跨场景示例：生日惊喜（锁定资金 + 生日前 2 天订花和蛋糕）。"""
        slots = dict(intent.slots)
        person = str(slots.get("person") or "").strip()
        if not person:
            return Plan(
                intent=intent.name,
                slots=slots,
                missing=["person"],
                question="这份生日惊喜要送给谁？可以填写姓名或关系，例如「老师」「妈妈」。",
            )
        target_person, birthday = lookup_person(ctx, person)
        display_name = target_person.name if target_person else person
        given = slots.get("birthday")
        if not given:
            plan = Plan(intent=intent.name, slots=slots, missing=["birthday"])
            hint = (f"系统记录为 {birthday.isoformat()}，请重新输入确认。"
                    if birthday else "系统目前没有记录。")
            plan.question = (f"请先输入「{display_name}」的生日，格式例如「11月20日」或「2026-11-20」。"
                             f"\n{hint}")
            return plan
        parsed = parse_date(str(given), ctx.clock.today())
        if parsed is None:
            plan = Plan(intent=intent.name, slots=slots, missing=["birthday"])
            plan.question = "这个生日日期无法识别，请重新输入，例如「11月20日」。"
            return plan
        birthday = parsed
        budget = float(slots.get("budget") or 1000.0)
        lock_amount = float(slots["budget"]) if slots.get("budget") else 1000.0
        gift_day = birthday - timedelta(days=2)
        steps = [
            PlanStep(
                tool="goal.create",
                args={"name": f"{display_name}生日惊喜金", "target_amount": lock_amount,
                      "deadline": birthday.isoformat(), "scenario": "birthday"},
                note="锁定当月活期资金",
            ),
            PlanStep(
                tool="crossscene.schedule_gift",
                args={"person": display_name, "item": "鲜花", "date": gift_day.isoformat(),
                      "budget": round(budget * 0.35, 2)},
                note="生日前 2 天送花",
            ),
            PlanStep(
                tool="crossscene.schedule_gift",
                args={"person": display_name, "item": "蛋糕", "date": gift_day.isoformat(),
                      "budget": round(budget * 0.45, 2)},
                note="生日前 2 天订蛋糕",
            ),
        ]
        plan = Plan(intent=intent.name, steps=steps, slots=slots)
        plan.description = (
            f"为「{display_name}」准备生日惊喜（生日 {birthday.isoformat()}）：\n"
            f"  1. 现在锁定 {lock_amount:,.2f} 元活期资金作为生日专款\n"
            f"  2. {gift_day.isoformat()}（生日前 2 天）订购鲜花，预算 {budget * 0.35:,.2f} 元\n"
            f"  3. {gift_day.isoformat()}（生日前 2 天）预订蛋糕，预算 {budget * 0.45:,.2f} 元"
        )
        return plan

    def _gift_plan(self, intent: Intent, ctx: ToolContext) -> Plan:
        """Plan a flexible gift order without assuming a fixed product."""
        slots = dict(intent.slots)
        person = str(slots.get("person") or "").strip()
        if not person:
            return Plan(
                intent=intent.name,
                slots=slots,
                missing=["person"],
                question="请问这份礼物要送给谁？",
            )
        item = str(slots.get("item") or "").strip()
        if not item:
            return Plan(
                intent=intent.name,
                slots=slots,
                missing=["item"],
                question="请问想送什么礼物？请直接说具体物品，例如「鲜花」「蛋糕」或「苹果手机」。",
            )
        date_text = str(slots.get("date") or "").strip()
        if not date_text:
            return Plan(
                intent=intent.name,
                slots=slots,
                missing=["date"],
                question="请问希望哪一天送达（日期）？例如「11月20日」或「下周六」。",
            )
        run_day = parse_date(date_text, ctx.clock.today())
        if run_day is None:
            return Plan(
                intent=intent.name,
                slots=slots,
                missing=["date"],
                question="这个送达日期无法识别，请重新输入，例如「明天」「11月20日」。",
            )
        slots["date"] = run_day.isoformat()

        try:
            budget = float(slots.get("budget") or 0)
        except (TypeError, ValueError):
            budget = 0.0
        provider = (
            ctx.providers.merchants
            if getattr(ctx, "providers", None) is not None
            else MerchantOfferProvider(ctx.store)
        )
        offer = provider.find_offer(
            item,
            budget or None,
            run_day.isoformat(),
            ctx.user.city,
        )
        base_offer = provider.find_offer(
            item,
            None,
            run_day.isoformat(),
            ctx.user.city,
        )
        if base_offer is not None and budget and budget < base_offer.price:
            slots.pop("budget", None)
            return Plan(
                intent=intent.name,
                slots=slots,
                missing=["budget"],
                question=(
                    f"「{item}」在演示商户中的最低价格为 {base_offer.price:,.2f} 元，"
                    "请提高预算或更换礼物。"
                ),
            )
        if base_offer is None and budget <= 0:
            return Plan(
                intent=intent.name,
                slots=slots,
                missing=["budget"],
                question=(
                    f"演示商户目录中没有「{item}」。请提供本次礼物价格，"
                    "我会按“用户报价、模拟代购”处理。"
                ),
            )

        target_person, _ = lookup_person(ctx, person)
        display_name = target_person.name if target_person else person
        step = PlanStep(
            tool="crossscene.schedule_gift",
            args={
                "person": display_name,
                "item": item,
                "date": run_day.isoformat(),
                **({"budget": budget} if budget > 0 else {}),
            },
            note="确认后创建礼物预约",
        )
        if offer is not None:
            price_text = f"商户报价 {offer.price:,.2f} 元（{offer.merchant}）"
        else:
            price_text = f"用户报价 {budget:,.2f} 元，模拟代购（未联网核验）"
        plan = Plan(intent=intent.name, steps=[step], slots=slots)
        plan.description = "\n".join([
            f"  · 预约 {run_day.isoformat()} 为「{display_name}」下单「{item}」",
            f"  · {price_text}",
        ])
        return plan

    def _describe(self, steps: List[PlanStep], ctx: ToolContext) -> str:
        return "\n".join("  · " + self._describe_step(step, ctx) for step in steps)

    def _describe_step(self, step: PlanStep, ctx: ToolContext) -> str:
        args = step.args
        tool = step.tool
        try:
            if tool == "transfer.send":
                channel = str(args.get("channel") or "phone")
                payee = ctx.providers.payees.find(ctx.user_id, str(args.get("payee") or ""))
                target = channel_account(payee, channel) if payee else ""
                payer = resolve_account(ctx, str(args.get("account") or ""))
                parts = [
                    f"向「{self._payee_label(ctx, args.get('payee'))}」"
                    f"转账 {float(args.get('amount', 0)):,.2f} 元",
                    f"收款方式：{CHANNEL_LABELS.get(channel, channel)}",
                ]
                if target:
                    parts.append(f"收款账号：{target}")
                elif payee is not None or channel != "phone":
                    parts.append(
                        f"未配置{CHANNEL_LABELS.get(channel, channel)}收款账号，"
                        "请到「资料与卡片设置」补充"
                    )
                if payer:
                    parts.append(f"付款账户：{payer.name}（尾号 {payer.tail}）")
                if args.get("note"):
                    parts.append(f"备注：{args['note']}")
                return "；".join(parts)
            if tool == "transfer.schedule":
                repeat = {"once": "一次性", "monthly": "每月", "weekly": "每周"}.get(
                    str(args.get("repeat", "once")), "一次性"
                )
                channel = str(args.get("channel") or "phone")
                payee = ctx.providers.payees.find(ctx.user_id, str(args.get("payee") or ""))
                target = channel_account(payee, channel) if payee else ""
                when = str(args.get("when", ""))
                parsed = parse_date(when, ctx.clock.today())
                when_text = parsed.isoformat() if parsed else when
                parts = [
                    f"{repeat}向「{self._payee_label(ctx, args.get('payee'))}」"
                    f"转账 {float(args.get('amount', 0)):,.2f} 元",
                    f"收款方式：{CHANNEL_LABELS.get(channel, channel)}",
                    f"首次执行：{when_text}",
                ]
                if target:
                    parts.append(f"收款账号：{target}")
                elif payee is not None or channel != "phone":
                    parts.append(
                        f"未配置{CHANNEL_LABELS.get(channel, channel)}收款账号，"
                        "请到「资料与卡片设置」补充"
                    )
                return "；".join(parts)
            if tool == "transfer.split_aa":
                people = args.get("participants") or []
                total = float(args.get("total", 0))
                share = total / len(people) if people else 0.0
                channel = str(args.get("channel") or "wechat")
                return (f"AA 收款 {total:,.2f} 元；{len(people)} 人平摊，"
                        f"每人 {share:,.2f} 元；收款方式：{CHANNEL_LABELS.get(channel, channel)}；"
                        "发起人：本人")
            if tool == "bill.analyze":
                return f"统计 {args.get('period', 'this_month')} 的消费分类与收支情况"
            if tool == "bill.anomaly":
                return f"扫描 {args.get('period', 'last_30_days')} 的异常交易"
            if tool == "bill.report":
                return f"生成 {args.get('period', 'this_month')} 账单报告"
            if tool == "bill.search":
                return f"检索与「{args.get('keyword')}」相关的消费"
            if tool == "wealth.subscribe":
                product = ctx.providers.wealth.find(str(args.get("product") or ""))
                payer = resolve_account(ctx, str(args.get("account") or ""))
                product_text = (f"「{product.name}」（{product.product_id}，R{product.risk_level}）"
                                if product else f"「{args.get('product')}」")
                parts = [f"申购{product_text} {float(args.get('amount', 0)):,.2f} 元"]
                if product:
                    parts.append(f"费率：{product.fee_rate * 100:.2f}%")
                    parts.append(f"赎回：{product.redemption_rule}")
                    parts.append(f"数据：{product.source or '未知来源'}")
                if payer:
                    parts.append(f"扣款账户：{payer.name}（尾号 {payer.tail}）")
                return "；".join(parts)
            if tool == "wealth.redeem":
                holdings = ctx.providers.wealth.holdings(ctx.user_id, "holding")
                product_key = str(args.get("product") or "")
                product = ctx.providers.wealth.find(product_key) if product_key else None
                if product:
                    holdings = [h for h in holdings if h.product_id == product.product_id]
                names = []
                for holding in holdings:
                    item = ctx.providers.wealth.get(holding.product_id)
                    names.append(item.name if item else holding.product_id)
                scope = "全部持仓" if not product_key else f"「{product.name if product else product_key}」"
                amount_text = (f" {float(args['amount']):,.2f} 元"
                               if args.get("amount") else "（全部可赎回份额）")
                return f"赎回{scope}{amount_text}；涉及：{'、'.join(names) or '无持仓'}"
            if tool == "card.apply":
                kind = str(args.get("kind") or "")
                label = "储蓄卡" if kind == "debit" else CARD_KIND_LABELS.get(kind, kind)
                bank = str(args.get("bank") or "未提供")
                phone = "".join(ch for ch in str(args.get("phone") or "") if ch.isdigit())
                masked_phone = (f"{phone[:3]}****{phone[-4:]}"
                                if len(phone) == 11 else "未提供")
                if kind == "debit":
                    password_check = evaluate_card_password(args.get("card_password"))
                    return (
                        f"开立{label}；发卡行：{bank}；"
                        f"账户类型：{args.get('account_class')}；"
                        f"开户城市/网点：{args.get('branch')}；"
                        f"预留手机号：{masked_phone}；"
                        f"取款密码：已二次确认（强度 {password_check['strength']}，"
                        f"{password_check['score']}/100）"
                    )
                if kind == "credit":
                    statement_label = {
                        "uploaded": "已上传流水",
                        "demo": "演示流水",
                    }.get(str(args.get("statement_mode")), "未选择")
                    return (
                        f"申请{label}；发卡行：{bank}；"
                        f"预留手机号：{masked_phone}；"
                        f"就业情况：{args.get('employment')}；"
                        f"月收入：{float(args.get('monthly_income', 0)):,.0f} 元；"
                        f"每月负债：{float(args.get('existing_debt', 0)):,.0f} 元；"
                        f"期望额度：{float(args.get('limit_request', 0)):,.0f} 元；"
                        f"用途：{args.get('credit_purpose')}；"
                        f"流水核验：{statement_label}；"
                        "模拟信用评估授权：已同意"
                    )
                return f"申请{label}；发卡行：{bank}"
            if tool == "card.bind":
                number = "".join(ch for ch in str(args.get("card_no") or "") if ch.isdigit())
                bank = str(args.get("bank") or BANK_BINS.get(number[:6], "未识别银行"))
                phone_digits = "".join(ch for ch in str(args.get("phone") or "") if ch.isdigit())
                phone = (f"{phone_digits[:3]}****{phone_digits[-4:]}"
                         if len(phone_digits) == 11 else "未提供")
                return (f"绑定{bank}储蓄卡（尾号{number[-4:]}）；"
                        f"预留手机号：{phone}")
            if tool in {"card.freeze", "card.unlock", "card.report_lost"}:
                card = ctx.providers.cards.find(ctx.user_id, str(args.get("card", "")))
                label = (f"{CARD_KIND_LABELS.get(card.kind, card.kind)}（尾号{card.tail}）"
                         if card else "未识别的卡片")
                if tool == "card.freeze":
                    return f"冻结{label}，暂停全部交易"
                if tool == "card.unlock":
                    return f"解冻{label}"
                return f"为{label}办理挂失并补卡（原卡将永久失效）"
            if tool == "card.limit_adjust":
                card = ctx.providers.cards.find(ctx.user_id, str(args.get("card", "")))
                label = (f"{CARD_KIND_LABELS.get(card.kind, card.kind)}（尾号{card.tail}）"
                         if card else "未识别的卡片")
                return f"把{label}的额度调整为 {float(args.get('new_limit', 0)):,.0f} 元"
            if tool == "card.control":
                card = ctx.providers.cards.find(ctx.user_id, str(args.get("card", "")))
                label = (f"{CARD_KIND_LABELS.get(card.kind, card.kind)}（尾号{card.tail}）"
                         if card else "未识别的卡片")
                scope = {"online": "线上交易", "overseas": "境外交易",
                         "contactless": "小额免密"}.get(str(args.get("scope")), str(args.get("scope")))
                return f"{'开启' if args.get('enable') else '关闭'}{label}的{scope}"
            if tool == "subscription.cancel":
                keyword = str(args.get("merchant") or "")
                target = next(
                    (sub for sub in ctx.providers.subscriptions.of(ctx.user_id, "active")
                     if keyword and (keyword in sub.merchant or sub.merchant in keyword)),
                    None,
                )
                if target is None:
                    return f"取消「{keyword}」的自动续费（未找到明确匹配）"
                return (f"取消「{target.merchant}」的自动续费；"
                        f"扣费金额：{target.amount:,.2f} 元；"
                        f"下次扣费：{target.next_charge_date}")
            if tool == "subscription.resume":
                keyword = str(args.get("merchant") or "")
                target = next(
                    (sub for sub in ctx.providers.subscriptions.of(ctx.user_id, "cancelled")
                     if keyword and (keyword in sub.merchant or sub.merchant in keyword)),
                    None,
                )
                if target is None:
                    return f"恢复「{keyword}」的自动续费（未找到已取消订阅）"
                return (f"恢复「{target.merchant}」的自动续费；"
                        f"每期金额：{target.amount:,.2f} 元；"
                        f"当前周期到期：{target.expires_at or target.next_charge_date}")
            if tool == "subscription.change_cycle":
                keyword = str(args.get("merchant") or "")
                target = next(
                    (
                        sub
                        for sub in ctx.providers.subscriptions.of(ctx.user_id, "active")
                        if keyword and (
                            keyword in sub.merchant or sub.merchant in keyword
                        )
                    ),
                    None,
                )
                if target is None:
                    return f"调整「{keyword}」续费周期（未找到生效中的订阅）"
                return "\n".join([
                    f"  · 当前服务：{target.merchant}，{target.term_months} 个月，"
                    f"续费价 {target.amount:,.2f} 元",
                    f"  · 下期服务：{int(args.get('term_months', 0))} 个月，"
                    f"续费价 {float(args.get('renewal_price', 0)):,.2f} 元",
                    "  · 本周期不扣款，当前到期后生效",
                ])
            if tool == "membership.purchase":
                items = list(args.get("items") or [])
                lines = ["会员充值订单（价格由用户提供，未联网核验）："]
                total = 0.0
                for index, item in enumerate(items, start=1):
                    price = float(item.get("purchase_price") or 0)
                    total += price
                    auto_label = (
                        f"自动续费，续费价 {float(item.get('renewal_price') or 0):,.2f} 元"
                        if item.get("auto_renew")
                        else "不自动续费"
                    )
                    lines.append(
                        f"  {index}. {item.get('platform')} "
                        f"{int(item.get('term_months') or 0)} 个月，"
                        f"本次 {price:,.2f} 元，{auto_label}"
                    )
                account = ctx.store.primary_account(ctx.user_id)
                lines.append(f"  · 合计扣款：{total:,.2f} 元")
                if account is not None:
                    lines.append(
                        f"  · 扣款账户：{account.name}（尾号 {account.tail}），"
                        f"可用 {account.available:,.2f} 元"
                    )
                return "\n".join(lines)
            if tool == "goal.create":
                account = resolve_account(ctx)
                parts = [f"锁定 {float(args.get('target_amount', 0)):,.2f} 元到目标「{args.get('name')}」"]
                if account:
                    parts.append(f"锁定账户：{account.name}（尾号 {account.tail}）")
                if args.get("deadline"):
                    parts.append(f"目标日期：{args['deadline']}")
                return "；".join(parts)
            if tool == "crossscene.schedule_gift":
                provider = (
                    ctx.providers.merchants
                    if getattr(ctx, "providers", None) is not None
                    else MerchantOfferProvider(ctx.store)
                )
                offer = provider.find_offer(
                    str(args.get("item") or ""),
                    float(args.get("budget") or 0) or None,
                    str(args.get("date") or ""),
                    ctx.user.city,
                )
                if offer is None:
                    return f"预约 {args.get('date')} 为{args.get('person')}下单{args.get('item')}（当前无可用商户）"
                return (f"预约 {args.get('date')} 由「{offer.merchant}」为{args.get('person')}下单"
                        f"{args.get('item')}，价格 {offer.price:,.2f} 元")
        except (TypeError, ValueError):
            pass
        tool_obj = self.registry.get(tool)
        return tool_obj.description if tool_obj else tool

    @staticmethod
    def _payee_label(ctx: ToolContext, raw) -> str:
        """把"房东"这类备注解析成真实姓名，让二次确认界面更明确。"""
        keyword = str(raw or "")
        payee = ctx.providers.payees.find(ctx.user_id, keyword)
        if payee and payee.name:
            return payee.name if payee.name == keyword else f"{payee.name}（{keyword}）"
        return keyword

