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
from finagent.tools.crossscene_tools import lookup_person
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
    "subscription.remind": [("subscription.remind", {"days": "days"})],
    "goal.create": [("goal.create", {"name": "name", "target_amount": "target_amount",
                                     "deadline": "deadline", "scenario": "scenario"})],
    "goal.list": [("goal.list", {})],
    "account.balance": [("account.balance", {})],
    "profile.show": [("profile.show", {})],
    "help": [("help", {})],
    "agent.about": [("agent.about", {})],
    "crossscene.daily_brief": [("crossscene.daily_brief", {})],
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
    "new_limit": "请问要调整到多少额度？",
    "merchant": "请问要取消哪个订阅？例如「腾讯视频VIP」。",
    "person": "请问是要为谁准备？例如「老婆」「妈妈」。",
    "budget": "请问预算多少？例如「1000」元。",
    "name": "请问这个目标叫什么名字？例如「旅行基金」。",
    "target_amount": "请问目标金额是多少？",
    "birthday": "请问 ta 的生日是哪一天？例如「11月20日」。",
}

NUMERIC_SLOTS = {"amount", "total", "target_amount", "new_limit", "budget", "limit_request", "days", "risk_level"}
DATE_SLOTS = {"when", "deadline", "date", "birthday"}


class Planner:
    """把意图翻译成可执行的工具调用计划。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def plan(self, intent: Intent, ctx: ToolContext) -> Plan:
        if intent.name == "crossscene.birthday_plan":
            return self._birthday_plan(intent, ctx)
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

    def _birthday_plan(self, intent: Intent, ctx: ToolContext) -> Plan:
        """跨场景示例：生日惊喜（锁定资金 + 生日前 2 天订花和蛋糕）。"""
        slots = dict(intent.slots)
        person = slots.get("person") or "爱人"
        target_person, birthday = lookup_person(ctx, person)
        display_name = target_person.name if target_person else person
        # 用户在多轮对话里直接告知了生日时，以用户提供的日期为准
        given = slots.get("birthday")
        if given:
            parsed = parse_date(str(given), ctx.clock.today())
            if parsed is not None:
                birthday = parsed
        if birthday is None:
            plan = Plan(intent=intent.name, slots=slots, missing=["birthday"])
            plan.question = (f"我还不知道「{display_name}」的生日，可以告诉我具体日期吗？"
                             f"例如「11月20日」。")
            return plan
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

    def _describe(self, steps: List[PlanStep], ctx: ToolContext) -> str:
        return "\n".join("  · " + self._describe_step(step, ctx) for step in steps)

    def _describe_step(self, step: PlanStep, ctx: ToolContext) -> str:
        args = step.args
        tool = step.tool
        try:
            if tool == "transfer.send":
                text = (f"向「{self._payee_label(ctx, args.get('payee'))}」"
                        f"转账 {float(args.get('amount', 0)):,.2f} 元")
                return text + (f"，备注：{args['note']}" if args.get("note") else "")
            if tool == "transfer.schedule":
                repeat = {"once": "一次性", "monthly": "每月", "weekly": "每周"}.get(
                    str(args.get("repeat", "once")), "一次性"
                )
                when = str(args.get("when", ""))
                parsed = parse_date(when, ctx.clock.today())
                when_text = parsed.isoformat() if parsed else when
                return (f"{repeat}向「{self._payee_label(ctx, args.get('payee'))}」"
                        f"转账 {float(args.get('amount', 0)):,.2f} 元，首次执行 {when_text}")
            if tool == "transfer.split_aa":
                people = args.get("participants") or []
                return f"按 {len(people)} 人平摊 {float(args.get('total', 0)):,.2f} 元并发起 AA 收款"
            if tool == "bill.analyze":
                return f"统计 {args.get('period', 'this_month')} 的消费分类与收支情况"
            if tool == "bill.anomaly":
                return f"扫描 {args.get('period', 'last_30_days')} 的异常交易"
            if tool == "bill.report":
                return f"生成 {args.get('period', 'this_month')} 账单报告"
            if tool == "bill.search":
                return f"检索与「{args.get('keyword')}」相关的消费"
            if tool == "wealth.subscribe":
                return f"申购「{args.get('product')}」{float(args.get('amount', 0)):,.2f} 元"
            if tool == "wealth.redeem":
                text = f"赎回「{args.get('product')}」" if args.get("product") else "赎回理财持仓"
                if args.get("amount"):
                    text += f" {float(args['amount']):,.2f} 元"
                return text
            if tool in {"card.freeze", "card.unlock", "card.report_lost"}:
                card = ctx.store.find_card(ctx.user_id, str(args.get("card", "")))
                label = f"尾号{card.tail}的卡片" if card else "默认卡片"
                if tool == "card.freeze":
                    return f"冻结{label}，暂停全部交易"
                if tool == "card.unlock":
                    return f"解冻{label}"
                return f"为{label}办理挂失并补卡（原卡将永久失效）"
            if tool == "card.limit_adjust":
                return f"把额度调整为 {float(args.get('new_limit', 0)):,.0f} 元"
            if tool == "card.control":
                scope = {"online": "线上交易", "overseas": "境外交易",
                         "contactless": "小额免密"}.get(str(args.get("scope")), str(args.get("scope")))
                return f"{'开启' if args.get('enable') else '关闭'}{scope}"
            if tool == "subscription.cancel":
                return f"取消「{args.get('merchant')}」的自动续费"
            if tool == "goal.create":
                return f"锁定 {float(args.get('target_amount', 0)):,.2f} 元到目标「{args.get('name')}」"
            if tool == "crossscene.schedule_gift":
                return (f"预约 {args.get('date')} 为{args.get('person')}下单{args.get('item')}，"
                        f"预算 {float(args.get('budget', 0)):,.2f} 元")
        except (TypeError, ValueError):
            pass
        tool_obj = self.registry.get(tool)
        return tool_obj.description if tool_obj else tool

    @staticmethod
    def _payee_label(ctx: ToolContext, raw) -> str:
        """把"房东"这类备注解析成真实姓名，让二次确认界面更明确。"""
        keyword = str(raw or "")
        payee = ctx.store.find_payee(ctx.user_id, keyword)
        if payee and payee.name:
            return payee.name if payee.name == keyword else f"{payee.name}（{keyword}）"
        return keyword

