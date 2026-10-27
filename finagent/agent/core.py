"""智能体核心：把 NLU、规划器、风控、工具集串成一条完整链路。

一次用户输入的完整处理流程：
    自然语言
      -> 意图识别（NLU）          大模型优先，规则兜底
      -> 槽位补全（多轮对话）      缺参数就追问，直到齐了
      -> 任务规划（Planner）       单步或跨工具的多步计划
      -> 风控校验（RiskEngine）    限额/二次确认/身份验证/拦截
      -> 工具执行（Dispatcher）    逐个调用银行业务工具
      -> 自然语言应答 + 调用轨迹
"""
from __future__ import annotations

import re
from dataclasses import asdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from config import LLMConfig, RiskPolicy
from finagent.agent.dialogue import DialogueSession, Pending, classify_control
from finagent.agent.planner import (
    DATA_CONFIRM_INTENTS,
    NUMERIC_SLOTS,
    DATE_SLOTS,
    Plan,
    Planner,
    PlanStep,
)
from finagent.agent.registry import ToolRegistry, build_default_registry
from finagent.agent.scheduler import Scheduler
from finagent.agent.subscription_lifecycle import process_subscription_lifecycle
from finagent.data.store import Clock, DataStore
from finagent.data.market import MarketDataProvider
from finagent.data.providers import build_data_providers
from finagent.nlu.intent_parser import IntentParser
from finagent.nlu.intent_parser import normalize_synonyms
from finagent.nlu.intent_schema import Intent
from finagent.nlu.membership_parser import (
    apply_membership_options,
    apply_membership_purchase_prices,
    apply_membership_renewal_prices,
    validate_membership_items,
)
from finagent.nlu.llm_client import LLMClient
from finagent.risk.engine import RiskDecision, RiskEngine
from finagent.risk.questionnaire import (
    BASIC_MAP,
    BASIC_QUESTIONS,
    Inference,
    evaluate_basic,
    infer_basic_answers_detailed,
)
from finagent.tools.base import ToolContext, ToolResult
from finagent.tools.card_tools import (
    evaluate_card_password,
    validate_card_number,
    validate_phone_number,
)
from finagent.tools.transfer_tools import resolve_account
from finagent.utils import parse_amount, parse_date
from finagent.vision.receipt_parser import looks_like_pasted_bill

# 明显不是"人名/商户名"的输入，不能拿来当收款人（防止把时间戳填成收款人）
NOISE_VALUE_RE = re.compile(
    r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
    r"|\d{1,2}\s*[:：]\s*\d{2}"
    r"|^\s*[-+]?\s*[¥￥]?\s*\d[\d,]*(?:\.\d{1,2})?\s*(?:元|块)?\s*$"
)


@dataclass
class AgentReply:
    """一次 Agent 应答。"""

    text: str
    intent: str = ""
    ok: bool = True
    kind: str = "reply"                      # reply / question / confirm / verify / executed / error
    trace: List[Dict[str, Any]] = field(default_factory=list)
    plan: List[str] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)
    needs_input: bool = False


@dataclass
class ReplyMemory:
    """A business reply and the user request that produced it."""

    user_text: str
    reply: AgentReply


class BankingAgent:
    """银行智能代理。"""

    MAX_VERIFY_ATTEMPTS = 3
    MAX_REPLY_HISTORY = 20
    REVISABLE_AMOUNT_SLOTS = {
        "transfer.send": "amount",
        "transfer.schedule": "amount",
        "transfer.split_aa": "total",
    }

    def __init__(
        self,
        store: DataStore,
        user_id: str,
        registry: Optional[ToolRegistry] = None,
        llm: Optional[LLMClient] = None,
        llm_mode: Optional[str] = None,
        session: Optional[DialogueSession] = None,
    ) -> None:
        self.store = store
        self.clock: Clock = store.clock
        self.user_id = user_id
        self.registry = registry or build_default_registry()
        self.planner = Planner(self.registry)
        self.market = MarketDataProvider()
        self.providers = build_data_providers(store, self.market)
        upload_ledger = self.providers.accounts.upload_balance(user_id)
        store.migrate_upload_ledger(user_id, upload_ledger)
        self.risk = RiskEngine(store, self.clock, providers=self.providers)
        self.scheduler = Scheduler(
            store,
            self.clock,
            self.risk,
            self.registry,
            providers=self.providers,
        )
        self.llm = llm if llm is not None else LLMClient()
        self.parser = IntentParser(
            store=store,
            user_id=user_id,
            llm=self.llm,
            mode=llm_mode or LLMConfig.MODE,
        )
        self.session = session or DialogueSession(user_id)
        # 会话级共享缓存：工具之间用它传递中间结果（例如"已识别、待导入"的账单）
        self.scratch: Dict[str, Any] = {}
        self.risk_answers: Dict[str, str] = {}
        self.risk_level: Optional[int] = None
        self.risk_assessed_at = ""
        self.risk_inference_confirmed_at = ""
        self.verify_attempts = 0
        self.last_intent_name = ""
        self.paused_questionnaire: Optional[Pending] = None
        self.last_reply: Optional[AgentReply] = None
        self.reply_history: List[ReplyMemory] = []

    # ==================================================================
    # 对外入口
    # ==================================================================
    def reset(self) -> None:
        self.session.reset()
        self.scratch.clear()
        self.risk_answers.clear()
        self.risk_level = None
        self.risk_assessed_at = ""
        self.risk_inference_confirmed_at = ""
        self.verify_attempts = 0
        self.paused_questionnaire = None
        self.last_reply = None
        self.reply_history.clear()

    def export_reply_history(self) -> List[Dict[str, Any]]:
        return [
            {"user_text": item.user_text, "reply": asdict(item.reply)}
            for item in self.reply_history
        ]

    def restore_reply_history(self, data: Optional[List[Dict[str, Any]]]) -> None:
        self.reply_history.clear()
        for item in data or []:
            if not isinstance(item, dict) or not isinstance(item.get("reply"), dict):
                continue
            try:
                reply = AgentReply(**item["reply"])
            except (TypeError, ValueError):
                continue
            self.reply_history.append(ReplyMemory(
                user_text=str(item.get("user_text") or ""),
                reply=reply,
            ))
        self._trim_reply_history()

    def remember_reply(self, user_text: str, reply: AgentReply) -> None:
        self.reply_history.append(ReplyMemory(user_text=user_text, reply=reply))
        self._trim_reply_history()

    def _trim_reply_history(self) -> None:
        if len(self.reply_history) > self.MAX_REPLY_HISTORY:
            self.reply_history = self.reply_history[-self.MAX_REPLY_HISTORY:]

    def export_paused_questionnaire(self) -> Optional[Dict[str, Any]]:
        if self.paused_questionnaire is None:
            return None
        return asdict(self.paused_questionnaire)

    def restore_paused_questionnaire(self, data: Optional[Dict[str, Any]]) -> None:
        if not data:
            self.paused_questionnaire = None
            return
        origin_data = data.get("origin")
        origin = Intent(**origin_data) if isinstance(origin_data, dict) else None
        inferences = {
            qid: Inference(**value) if isinstance(value, dict) else value
            for qid, value in (data.get("inferences") or {}).items()
        }
        self.paused_questionnaire = Pending(
            kind=str(data.get("kind") or "questionnaire"),
            intent=str(data.get("intent") or ""),
            slots=dict(data.get("slots") or {}),
            missing=list(data.get("missing") or []),
            plan=None,
            question=str(data.get("question") or ""),
            answers=dict(data.get("answers") or {}),
            queue=list(data.get("queue") or []),
            origin=origin,
            inferences=inferences,
            inference_confirmed_at=str(data.get("inference_confirmed_at") or ""),
        )

    def context(self) -> ToolContext:
        ctx = ToolContext(store=self.store, clock=self.clock, risk=self.risk, user_id=self.user_id)
        ctx.session = self.scratch
        ctx.session["registry"] = self.registry
        ctx.session["llm"] = self.llm
        ctx.session["market"] = self.market
        ctx.session["llm_mode"] = self.parser.mode
        ctx.session["parser_source"] = self.parser.last_source
        ctx.session["risk_answers"] = self.risk_answers
        ctx.session["risk_level"] = self.risk_level
        ctx.providers = self.providers
        return ctx

    def run_due_tasks(self) -> List[str]:
        """执行到期的定时任务（定时转账、预约下单、提醒）。"""
        return self.scheduler.run_due(self.user_id)

    def handle(self, text: str) -> AgentReply:
        lifecycle_messages = process_subscription_lifecycle(
            self.store,
            self.clock,
            self.user_id,
        )
        reply = self._handle_input(text)
        if lifecycle_messages:
            notice = "\n".join(f"  · {message}" for message in lifecycle_messages)
            reply.text = (
                f"订阅状态更新：\n{notice}\n\n{reply.text}"
                if reply.text else f"订阅状态更新：\n{notice}"
            )
            reply.data["lifecycle_messages"] = lifecycle_messages
        return reply

    def _handle_input(self, text: str) -> AgentReply:
        """处理一句用户输入。"""
        raw = (text or "").strip()
        if not raw:
            return AgentReply("请说点什么吧，例如「给老婆转2000块」。", kind="question", needs_input=True)

        self.session.turn += 1
        remembered_text = "******" if self._input_is_secret(raw) else raw
        self.session.remember_user(remembered_text)
        try:
            reply = self._route(raw)
        except Exception as exc:  # 原型阶段：任何异常都转成可读提示，避免中断对话
            reply = AgentReply(
                f"抱歉，处理这句话时出了点问题：{exc}",
                ok=False,
                kind="error",
            )
        self.session.remember_assistant(reply.text)
        self.session.transcript.append(
            {"turn": self.session.turn, "user": remembered_text, "agent": reply.text,
             "intent": reply.intent, "tools": [t.get("tool") for t in reply.trace]}
        )
        self.last_reply = reply
        if not self._is_why_question(raw):
            self.remember_reply(raw, reply)
        return reply

    def _input_is_secret(self, text: str) -> bool:
        pending = self.session.pending
        if pending is None or not pending.missing:
            return False
        current = pending.missing[0]
        if pending.intent == "card.apply":
            return current in {"card_password", "card_password_confirm"}
        if pending.intent == "card.bind":
            return current == "password"
        return False

    # ==================================================================
    # 主路由
    # ==================================================================
    def _route(self, text: str) -> AgentReply:
        question_answer = self._answer_informational_question(text)
        if question_answer is not None:
            return question_answer
        if self._is_why_question(text):
            return self._explain_last_reply(text)

        pending = self.session.pending

        if pending is not None and pending.kind == "blocked_draft":
            return self._resume_blocked_draft(pending, text)

        if pending is not None and pending.kind == "risk_cooling":
            return self._handle_risk_cooling(pending, text)

        if pending is not None and pending.kind == "data_confirm":
            control = classify_control(text)
            if control == "confirm":
                return self._after_data_confirm(pending)
            if control == "deny":
                self.session.clear_pending()
                return AgentReply("好的，本次操作已取消，没有执行任何业务动作。")

        if pending is not None and pending.kind == "membership_options":
            if classify_control(text) == "deny":
                self.session.clear_pending()
                return AgentReply("好的，本次会员充值已取消，没有扣款。")
            return self._handle_membership_options(pending, text)

        if pending is not None and pending.kind == "membership_purchase_prices":
            if classify_control(text) == "deny":
                self.session.clear_pending()
                return AgentReply("好的，本次会员充值已取消，没有扣款。")
            return self._handle_membership_purchase_prices(pending, text)

        if pending is not None and pending.kind == "membership_renewal_prices":
            if classify_control(text) == "deny":
                self.session.clear_pending()
                return AgentReply("好的，本次会员充值已取消，没有扣款。")
            return self._handle_membership_renewal_prices(pending, text)

        if pending is not None and pending.kind == "confirm":
            control = classify_control(text)
            if control == "confirm":
                return self._after_confirm(pending)
            if control == "deny":
                self.session.clear_pending()
                return AgentReply("好的，这次操作已经取消，没有执行任何资金变动。", ok=True)

        if pending is not None and pending.kind == "verify":
            return self._handle_verify(pending, text)

        if pending is not None and pending.kind == "slot":
            return self._resume_from_slots(pending, text)

        if pending is not None and pending.kind == "questionnaire_confirm":
            control = classify_control(text)
            if control == "confirm":
                return self._finish_questionnaire(pending)
            if control == "deny":
                self.paused_questionnaire = None
                self.session.clear_pending()
                return AgentReply("好的，系统推断结果未采用，本次不做任何理财推荐。")
            intent = self.parser.parse(text, history=self.session.history[:-1])
            if not intent.is_unknown():
                self.paused_questionnaire = pending
                self.session.clear_pending()
                return self._continue_with_intent(intent)
            return AgentReply(
                "请回复「确认」采用这组系统推断答案，或回复「取消」放弃。",
                kind="question",
                needs_input=True,
            )

        if pending is not None and pending.kind == "questionnaire_resume":
            return self._handle_questionnaire_resume(pending, text)

        if pending is not None and pending.kind == "questionnaire":
            return self._handle_questionnaire(pending, text)

        # 用户直接把一整段账单文本粘进来时，整体交给账单解析，
        # 绝不能逐行当成指令（"转账-退款 +6.50" 会被误判成转账）
        if looks_like_pasted_bill(text):
            return self._handle_pasted_bill(text)

        # 新的一轮：先做意图识别
        # 纯控制词但没有待确认操作时的兜底提示
        if classify_control(text) and self.session.pending is None:
            return AgentReply(
                "目前没有等待确认的操作。您可以告诉我要办什么业务，例如「分析这个月账单」。",
                kind="reply",
            )

        intent = self.parser.parse(text, history=self.session.history[:-1])
        self.last_intent_name = intent.name
        if intent.is_unknown():
            return AgentReply(self._unknown_reply(text), kind="question", needs_input=True)
        return self._continue_with_intent(intent)

    def _continue_with_intent(self, intent: Intent) -> AgentReply:
        """Route a recognized new business intent after any prior state is handled."""
        if intent.name == "membership.purchase":
            self.session.clear_pending()
            return self._advance_membership_order(
                list(intent.slots.get("items") or [])
            )
        # 理财相关操作必须先完成风险问卷（投资者适当性管理的准入要求）
        if self._needs_risk_profile(intent.name) and self.risk_level is None:
            return self._start_questionnaire(intent)

        # 用户发起了一个新的业务请求，之前挂起的确认/追问作废
        self.session.clear_pending()
        ctx = self.context()
        plan = self.planner.plan(intent, ctx)
        if plan.missing:
            self.session.pending = Pending(
                kind="slot", intent=intent.name, slots=dict(intent.slots),
                missing=list(plan.missing), question=plan.question, plan=None,
            )
            return AgentReply(plan.question, intent=intent.name, kind="question", needs_input=True)
        if not plan.steps:
            return AgentReply(f"我理解到您的意图是「{intent.name}」，但没能生成可执行的动作，请再说具体一点。",
                              intent=intent.name, kind="question", needs_input=True)
        return self._gate_and_execute(plan)

    @staticmethod
    def _membership_item_lines(items: List[Dict[str, Any]]) -> str:
        return "\n".join(
            f"  {index}. {item.get('platform')} "
            f"{int(item.get('term_months') or 0)} 个月，"
            + (
                f"本次 {float(item.get('purchase_price') or 0):,.2f} 元"
                if float(item.get("purchase_price") or 0) > 0
                else "本次价格待补充"
            )
            for index, item in enumerate(items, start=1)
        )

    def _advance_membership_order(
        self,
        items: List[Dict[str, Any]],
    ) -> AgentReply:
        normalized = [dict(item) for item in items]
        if not normalized:
            return AgentReply(
                "请告诉我要充值的平台、月数和本次价格。例如："
                "「腾讯视频3个月60元、Keep12个月199元」。",
                intent="membership.purchase",
                kind="question",
                needs_input=True,
            )
        invalid_core = [item for item in normalized if (
            not str(item.get("platform") or "").strip()
            or not 1 <= int(item.get("term_months") or 0) <= 36
        )]
        if invalid_core:
            return AgentReply(
                "部分项目缺少平台或 1–36 个月周期，请重新发送完整订单。",
                intent="membership.purchase",
                kind="question",
                needs_input=True,
            )

        missing_prices = [
            item for item in normalized
            if float(item.get("purchase_price") or 0) <= 0
        ]
        if missing_prices:
            self.session.pending = Pending(
                kind="membership_purchase_prices",
                intent="membership.purchase",
                slots={"items": normalized},
            )
            return AgentReply(
                "已识别以下会员项目，请补充本次购买价格：\n"
                f"{self._membership_item_lines(normalized)}\n\n"
                "可直接回复「148元」；多个项目可回复"
                "「1本次价格148元，2本次价格299元」。",
                intent="membership.purchase",
                kind="question",
                needs_input=True,
            )

        if any(item.get("auto_renew") is None for item in normalized):
            self.session.pending = Pending(
                kind="membership_options",
                intent="membership.purchase",
                slots={"items": normalized},
            )
            return AgentReply(
                "请逐项确认是否默认开启自动续费（价格由你提供）：\n"
                f"{self._membership_item_lines(normalized)}\n\n"
                "可回复：「1自动续费，续费价25元；2不自动续费」，"
                "或「都自动续费，续费价同本次价格」。",
                intent="membership.purchase",
                kind="question",
                needs_input=True,
            )

        missing_renewal = [
            item for item in normalized
            if item.get("auto_renew")
            and float(item.get("renewal_price") or 0) <= 0
        ]
        if missing_renewal:
            self.session.pending = Pending(
                kind="membership_renewal_prices",
                intent="membership.purchase",
                slots={"items": normalized},
            )
            return AgentReply(
                "以下项目开启了自动续费，请补充每期续费价格：\n"
                f"{self._membership_item_lines(normalized)}\n\n"
                "可回复：「1续费价25元，2续费价199元」，"
                "或「都按本次价格续费」。",
                intent="membership.purchase",
                kind="question",
                needs_input=True,
            )

        errors = validate_membership_items(normalized)
        if errors:
            self.session.pending = Pending(
                kind="membership_options",
                intent="membership.purchase",
                slots={"items": normalized},
            )
            return AgentReply(
                "会员订单还需要调整：\n"
                + "\n".join(f"  · {error}" for error in errors),
                intent="membership.purchase",
                kind="question",
                needs_input=True,
            )

        self.session.clear_pending()
        intent = Intent(
            "membership.purchase",
            {"items": normalized},
            confidence=0.9,
            source="rule",
        )
        plan = self.planner.plan(intent, self.context())
        return self._gate_and_execute(plan)

    def _handle_membership_options(self, pending: Pending, text: str) -> AgentReply:
        items = apply_membership_options(
            list(pending.slots.get("items") or []),
            text,
        )
        pending.slots["items"] = items
        return self._advance_membership_order(items)

    def _handle_membership_purchase_prices(
        self,
        pending: Pending,
        text: str,
    ) -> AgentReply:
        items = apply_membership_purchase_prices(
            list(pending.slots.get("items") or []),
            text,
        )
        pending.slots["items"] = items
        return self._advance_membership_order(items)

    def _handle_membership_renewal_prices(
        self,
        pending: Pending,
        text: str,
    ) -> AgentReply:
        items = apply_membership_renewal_prices(
            list(pending.slots.get("items") or []),
            text,
        )
        pending.slots["items"] = items
        return self._advance_membership_order(items)

    def _interrupt_intent(self, text: str) -> Optional[Intent]:
        """Return a valid new business intent that should break a pending prompt."""
        if classify_control(text):
            return None
        intent = self.parser.parse(text, history=self.session.history[:-1])
        return None if intent.is_unknown() else intent

    @staticmethod
    def _is_why_question(text: str) -> bool:
        cleaned = re.sub(r"\s+", "", text or "")
        return bool(re.search(
            r"^(为什么|为啥|为何|凭什么|依据是什么|怎么判断|为什么这样|为什么给我|"
            r"这是什么意思|怎么得出|为什么这么判|为什么建议)",
            cleaned,
        ))

    @staticmethod
    def _is_upload_balance_question(text: str) -> bool:
        """Treat balance reconciliation wording as a question, not bill analysis."""
        cleaned = re.sub(r"\s+", "", text or "")
        ledger_terms = ("上传账单", "导入账单", "上传流水", "导入流水")
        question_terms = (
            "对不上", "不一致", "不一样", "不符", "差多少", "差很多",
            "为什么", "怎么", "怎么回事", "是不是", "感觉", "核对",
        )
        return (
            "余额" in cleaned
            and any(term in cleaned for term in ledger_terms)
            and any(term in cleaned for term in question_terms)
        )

    @staticmethod
    def _is_upload_balance_scope_question(text: str) -> bool:
        """Detect questions about which account uploaded spending belongs to."""
        cleaned = re.sub(r"\s+", "", text or "")
        ledger_terms = (
            "上传账单余额", "导入账单余额", "上传余额",
            "上传账单", "导入账单", "上传流水", "导入流水",
        )
        scope_terms = (
            "只计入", "只记入", "只扣", "只影响", "只算", "只归",
            "计入", "记入", "算在", "扣在", "归到", "归属",
        )
        question_terms = ("吗", "是不是", "是否", "会不会", "哪个", "哪里", "怎么", "为什么")
        context_terms = ("消费", "消耗", "支出", "花费", "前面", "虚拟", "账户", "银行卡")
        return (
            any(term in cleaned for term in ledger_terms)
            and any(term in cleaned for term in scope_terms)
            and any(term in cleaned for term in question_terms)
            and any(term in cleaned for term in context_terms)
        )

    def _answer_informational_question(self, text: str) -> Optional[AgentReply]:
        """Answer recognizable data questions before business keyword routing."""
        if self._is_upload_balance_scope_question(text):
            return self._explain_upload_balance_scope()
        if self._is_upload_balance_question(text):
            return self._explain_upload_balance()
        return None

    def _explain_upload_balance_scope(self) -> AgentReply:
        """Explain whether uploaded spending affects demo accounts or the ledger."""
        ledger = self.providers.accounts.upload_balance(self.user_id)
        imports = [
            txn for txn in self.providers.transactions.of(self.user_id)
            if "receipt_import" in txn.tags or txn.source.startswith("upload:")
        ]
        income = round(sum(txn.amount for txn in imports if txn.amount > 0), 2)
        expense = round(abs(sum(txn.amount for txn in imports if txn.amount < 0)), 2)
        net = round(sum(txn.amount for txn in imports), 2)
        lines = [
            "是的。上传账单导入的消费只记入「上传账单余额」这个虚拟账户，"
            "不会自动修改前面演示账户里的活期、定期、信用卡或电子钱包余额。",
            (
                f"该虚拟账户当前为 {ledger.balance:,.2f} 元：已导入 {len(imports)} 笔，"
                f"收入 {income:,.2f} 元、支出 {expense:,.2f} 元、净额 {net:,.2f} 元。"
            ),
        ]
        if imports:
            lines.append(
                "只要存在已导入流水，后续账单分析会优先只使用你上传的流水，"
                "不会把演示消费混进来。"
            )
        else:
            lines.append(
                "目前还没有成功导入的逐笔流水；完成账单确认导入后，消费才会记入这里，"
                "后续账单分析也会优先只使用上传流水。"
            )
        lines.extend([
            "总资产页面仍会把演示账户和这个虚拟上传账户一起展示，所以看起来会混在一起，"
            "但两者的余额归属和扣减是分开的。",
            "主动转账、申购等操作仍按你指定的付款账户扣款，不会默认使用上传账单余额。",
        ])
        return AgentReply(
            "\n".join(lines),
            intent="account.upload_balance_scope",
            kind="reply",
            data={
                "balance": ledger.balance,
                "imported_count": len(imports),
                "income": income,
                "expense": expense,
                "net": net,
            },
        )

    def _explain_upload_balance(self) -> AgentReply:
        """Explain the virtual upload ledger using the current session's imports."""
        ledger = self.providers.accounts.upload_balance(self.user_id)
        imports = [
            txn for txn in self.providers.transactions.of(self.user_id)
            if "receipt_import" in txn.tags or txn.source.startswith("upload:")
        ]
        batches = self.store.upload_batches_of(self.user_id)
        income = round(sum(txn.amount for txn in imports if txn.amount > 0), 2)
        expense = round(abs(sum(txn.amount for txn in imports if txn.amount < 0)), 2)
        net = round(sum(txn.amount for txn in imports), 2)
        lines = [
            "「上传账单余额」不是银行卡里的真实余额，而是一个虚拟补录账户；"
            "它从 0 元开始，只记录已经确认导入的账单流水。",
            (
                f"当前显示 {ledger.balance:,.2f} 元，由 {len(imports)} 笔已导入流水汇总："
                f"收入 {income:,.2f} 元、支出 {expense:,.2f} 元、净额 {net:,.2f} 元；"
                f"当前共 {len(batches)} 个批次。"
            ),
        ]
        if not imports:
            lines.append("目前还没有成功导入的逐笔流水，所以这个账户显示 0.00 元。")
        if abs(ledger.balance - net) > 0.005:
            lines.append(
                f"注意：账户余额与已导入流水净额相差 {ledger.balance - net:,.2f} 元，"
                "可能是旧数据迁移或批次冲回造成的，需要进一步核对。"
            )
        lines.extend([
            "和实际余额对不上，通常有这些原因：",
            "1. 真实银行卡包含未上传的工资、转账、理财、手续费等交易，虚拟账户看不到。",
            "2. 账单汇总页只做统计、不逐笔入账；只有确认导入的明细才会改变这个余额。",
            "3. 重复流水会被跳过，删除上传批次时对应金额也会冲回。",
            "4. 多张卡或多个账户的真实余额，不能用一份上传账单完整还原。",
            "因此这个数字适合核对“已上传流水的净影响”，不能直接当作银行卡实际余额。"
            "如果你提供实际余额和对应日期，我可以继续帮你计算差额并核对相关批次。",
        ])
        return AgentReply(
            "\n".join(lines),
            intent="account.upload_balance_explain",
            kind="reply",
            data={
                "balance": ledger.balance,
                "imported_count": len(imports),
                "income": income,
                "expense": expense,
                "net": net,
                "batch_count": len(batches),
            },
        )

    def _previous_user_text(self) -> str:
        for item in reversed(self.session.history[:-1]):
            if item.get("role") == "user":
                return str(item.get("content") or "")
        return ""

    @staticmethod
    def _why_subject(text: str) -> tuple[tuple[str, ...], str]:
        """Return the business topics explicitly named in a why-question."""
        cleaned = re.sub(r"\s+", "", text or "")
        # "为什么建议我..." refers to the recommendation in the latest result,
        # even when the suggested action mentions a card or a transfer.
        if re.search(r"(这个|这条|该|刚才的|上面的)?建议|为什么建议", cleaned):
            return (), ""
        groups = [
            (
                ("异常", "盗刷", "可疑交易", "高风险交易", "重复扣款", "乱扣"),
                ("bill.anomaly",),
                "异常交易",
            ),
            (
                ("风险测评", "风险评级", "风险等级", "投资者适当性", "c1", "c2", "c3", "c4", "c5"),
                ("wealth.risk_assess",),
                "风险测评",
            ),
            (
                ("理财推荐", "产品推荐", "推荐产品", "推荐基金", "理财产品", "申购"),
                ("wealth.recommend",),
                "理财产品推荐",
            ),
            (
                ("账单分析", "分析账单", "消费分类", "消费构成", "收支分析", "月度账单", "年度账单"),
                ("bill.analyze", "bill.report", "bill.search"),
                "账单分析",
            ),
            (
                ("银行卡", "卡片", "额度", "挂失", "冻结", "解锁"),
                (
                    "card.list", "card.apply", "card.bind", "card.limit_adjust",
                    "card.control", "card.freeze", "card.unlock", "card.report_lost",
                ),
                "银行卡操作",
            ),
            (
                ("转账", "到账", "收款", "aa"),
                ("transfer.send", "transfer.schedule", "transfer.split_aa", "transfer.list_scheduled"),
                "转账",
            ),
            (
                ("订阅", "自动续费", "续费"),
                (
                    "subscription.detect",
                    "subscription.list",
                    "subscription.cancel",
                    "subscription.resume",
                    "subscription.change_cycle",
                    "membership.purchase",
                    "subscription.remind",
                ),
                "订阅管理",
            ),
            (("余额",), ("account.balance",), "余额查询"),
            (("生日", "惊喜"), ("crossscene.birthday_plan",), "生日惊喜"),
            (("账单", "消费", "支出"), ("bill.analyze", "bill.report", "bill.search"), "账单分析"),
        ]
        lowered = cleaned.lower()
        for keywords, intents, label in groups:
            if any(keyword in lowered for keyword in keywords):
                return intents, label
        return (), ""

    def _latest_reply_memory(self, intents: tuple[str, ...] = ()) -> Optional[ReplyMemory]:
        for memory in reversed(self.reply_history):
            if memory.reply.intent == "explain.why":
                continue
            if not intents or memory.reply.intent in intents:
                return memory
        if self.last_reply is not None and self.last_reply.intent != "explain.why":
            if not intents or self.last_reply.intent in intents:
                return ReplyMemory(
                    user_text=self._previous_user_text(),
                    reply=self.last_reply,
                )
        return None

    def _explain_last_reply(self, text: str) -> AgentReply:
        intents, label = self._why_subject(text)
        memory = self._latest_reply_memory(intents)
        if memory is None and intents:
            return AgentReply(
                f"当前会话里没有找到与「{label}」匹配的结果。"
                f"请先执行一次{label}，我再解释具体依据。",
                intent="explain.why",
                kind="reply",
            )
        if memory is None:
            return AgentReply(
                "目前还没有可以解释的业务结果。您可以先发起一个查询或业务操作。",
                intent="explain.why",
                kind="reply",
            )
        last = memory.reply
        previous = memory.user_text or self._previous_user_text()
        if last.intent == "bill.anomaly":
            body = self._explain_anomaly(last, previous, text)
        elif last.intent in {"bill.analyze", "bill.report", "bill.search"}:
            body = self._explain_bill_analysis(last, previous)
        elif last.intent == "wealth.recommend":
            body = self._explain_wealth_recommendation(last, previous)
        elif last.intent == "wealth.risk_assess":
            body = self._explain_risk_assessment(last, previous)
        else:
            body = self._explain_generic(last, previous)
        return AgentReply(body, intent="explain.why", kind="reply")

    @staticmethod
    def _matching_anomaly_items(items: List[Dict[str, Any]], question: str) -> List[Dict[str, Any]]:
        cleaned = re.sub(r"\s+", "", question or "")
        kind_aliases = {
            "深夜": "night_large",
            "重复": "duplicate_charge",
            "金额离群": "amount_outlier",
            "偏离": "amount_outlier",
            "订阅涨价": "subscription_hike",
            "涨价": "subscription_hike",
            "境外": "overseas",
            "小额": "micro_burst",
            "试探": "micro_burst",
            "陌生商户": "new_merchant_large",
            "首次": "new_merchant_large",
        }
        requested_kinds = {
            kind for keyword, kind in kind_aliases.items() if keyword in cleaned
        }
        merchant_matches = [
            item for item in items
            if str(item.get("merchant") or "")
            and str(item.get("merchant") or "") in cleaned
        ]
        kind_matches = [
            item for item in items
            if str(item.get("kind") or "") in requested_kinds
        ]
        if merchant_matches and requested_kinds:
            narrowed = [
                item for item in merchant_matches
                if str(item.get("kind") or "") in requested_kinds
            ]
            if narrowed:
                return narrowed
        if merchant_matches:
            return merchant_matches
        if kind_matches:
            return kind_matches
        return items

    def _explain_anomaly(self, last: AgentReply, previous: str, question: str = "") -> str:
        items = list(last.data.get("items") or [])
        scope = last.data.get("data_scope", "")
        labels = {
            "night_large": "深夜大额",
            "duplicate_charge": "重复扣款",
            "amount_outlier": "金额离群",
            "subscription_hike": "订阅涨价",
            "overseas": "境外消费",
            "micro_burst": "小额试探",
            "new_merchant_large": "陌生商户大额",
        }
        rules = {
            "night_large": "在 00:00-05:00 发生且金额不低于 1,000 元",
            "duplicate_charge": "同一商户、同一金额在 10 分钟内重复扣款",
            "amount_outlier": "同类消费金额超过均值加 3 倍标准差，且达到 500 元",
            "subscription_hike": "订阅续费金额较历史价格明显上涨",
            "overseas": "商户或备注出现境外消费特征",
            "micro_burst": "1 小时内出现 4 笔以上 20 元以下小额扣款",
            "new_merchant_large": "当前分析范围内首次出现该商户且金额不低于 5,000 元",
        }
        selected = self._matching_anomaly_items(items, question)
        lines = ["异常交易的判定由规则命中和数据偏离共同得出。"]
        if selected:
            if len(selected) < len(items):
                lines.append(f"这里只解释问题中明确指定的 {len(selected)} 笔命中记录。")
            else:
                lines.append(f"本次共有 {len(items)} 笔记录命中异常规则。")
            lines.append("")
            for item in selected[:8]:
                kind = str(item.get("kind") or "")
                label = labels.get(kind, kind or "风险规则")
                rule = rules.get(kind, "命中当前风控规则")
                reason = str(item.get("reason") or "命中异常交易规则")
                suggestion = str(item.get("suggestion") or "")
                metrics = item.get("metrics") or {}
                amount = abs(float(item.get("amount") or 0))
                lines.append(
                    f"- {item.get('date', '日期未知')} {item.get('merchant', '未知商户')}，"
                    f"支出 {amount:,.2f} 元"
                )
                lines.append(f"  命中规则：{label}。{rule}。")
                lines.append(f"  具体依据：{reason}")
                if metrics:
                    metric_labels = {
                        "category_mean": "同类均值",
                        "category_stdev": "标准差",
                        "threshold": "告警阈值",
                        "zscore": "偏离倍数",
                        "hour": "发生时段",
                        "window_minutes": "时间窗口",
                        "count": "命中笔数",
                        "unit_amount": "单笔金额",
                        "total": "合计金额",
                        "previous_amount": "调整前金额",
                        "current_amount": "当前金额",
                        "increase_pct": "涨幅",
                        "amount": "涉及金额",
                        "first_seen": "首次出现",
                    }
                    metric_text = "，".join(
                        f"{metric_labels.get(key, key)} {value}"
                        for key, value in list(metrics.items())[:4]
                    )
                    lines.append(f"  量化指标：{metric_text}")
                if suggestion:
                    lines.append(f"  处理建议：{suggestion}")
                lines.append("")
        else:
            lines.append("这次没有交易同时命中异常规则，所以没有给出异常处理建议。")
        if scope == "uploaded":
            lines.append("数据范围：仅使用你上传的真实流水，未混入模拟数据。")
        elif scope == "demo":
            lines.append("数据范围：当前使用演示种子数据。")
        return "\n".join(lines)

    @staticmethod
    def _explain_bill_analysis(last: AgentReply, previous: str) -> str:
        data = last.data or {}
        stats = data.get("stats") or {}
        scope = data.get("data_scope", "")
        lines = [
            "账单分析的统计口径是：先按期间筛选交易，再汇总收入、支出和净结余，"
            "并按消费类别和商户排序。账单分析本身不等于异常交易检查。",
        ]
        if stats:
            lines.append(
                f"本次数据包含 {stats.get('count', 0)} 笔交易，"
                f"收入 {stats.get('income', 0):,.2f} 元，支出 {stats.get('expense', 0):,.2f} 元，"
                f"结余 {stats.get('net', 0):,.2f} 元。"
            )
        if scope:
            lines.append(f"数据范围：{'仅用户上传流水' if scope == 'uploaded' else '演示种子数据'}。")
        return "\n".join(lines)

    @staticmethod
    def _explain_wealth_recommendation(last: AgentReply, previous: str) -> str:
        data = last.data or {}
        return "\n".join([
            "推荐依据包括：您的风险承受等级、产品 R 风险等级、计划金额、投资期限、"
            "资金用途，以及产品是否来自公开行情或演示数据。",
            f"本次最高风险上限为 R{data.get('max_risk', '未记录')}，"
            f"筛出 {len(data.get('products') or [])} 个候选产品。"
            "演示产品的收益率不是实时收益，公开行情也只作为辅助参考。",
        ])

    @staticmethod
    def _explain_risk_assessment(last: AgentReply, previous: str) -> str:
        data = last.data or {}
        return "\n".join([
            "评分依据是问卷答案、账户资产、收入、应急金、投资经验和风险态度等维度，"
            "按规则加权后映射到客户等级。",
            f"本次得分 {data.get('total', '未记录')}，等级 {data.get('c_level', data.get('label', '未记录'))}。",
        ])

    @staticmethod
    def _explain_generic(last: AgentReply, previous: str) -> str:
        lines = ["这项结果依据刚才工具返回的当前会话数据。"]
        first_line = last.text.strip().splitlines()[0] if last.text.strip() else ""
        if first_line:
            lines.append(f"结果摘要：{first_line}")
        if last.trace:
            lines.append("我依据工具执行结果、账户数据和风控规则生成了这个结果。")
        else:
            lines.append("刚才的回复主要是在等待您补充信息或确认，因此没有执行资金动作。")
        return "\n".join(lines)

    def _handle_pasted_bill(self, text: str) -> AgentReply:
        """把粘贴进来的账单文本交给 bill.scan_receipt 整体解析。"""
        self.session.clear_pending()
        plan = Plan(intent="bill.scan_receipt",
                    steps=[PlanStep(tool="bill.scan_receipt", args={"text": text})])
        return self._gate_and_execute(plan)

    # ==================================================================
    # 风险问卷闸门（理财操作前必须先完成）
    # ==================================================================
    RISK_GATED_INTENTS = {"wealth.recommend", "wealth.subscribe", "wealth.risk_assess"}
    SKIP_WORDS = {"跳过", "跳过问卷", "默认", "用我的资料", "按我的资料", "skip"}
    ABORT_WORDS = {"取消", "算了", "不做了", "先不做了", "退出问卷"}
    RESUME_WORDS = {"1", "继续", "继续上次", "上次", "恢复", "恢复上次"}
    RESTART_WORDS = {"2", "重新开始", "重来", "重新", "从头开始"}

    @classmethod
    def _needs_risk_profile(cls, intent_name: str) -> bool:
        return intent_name in cls.RISK_GATED_INTENTS

    def _start_questionnaire(self, intent: Intent) -> AgentReply:
        if self.paused_questionnaire is not None:
            self.session.pending = Pending(
                kind="questionnaire_resume",
                intent=intent.name,
                origin=intent,
            )
            return AgentReply(
                self._questionnaire_resume_text(self.paused_questionnaire),
                intent=intent.name,
                kind="question",
                needs_input=True,
            )
        pending = Pending(
            kind="questionnaire",
            intent=intent.name,
            origin=intent,
            queue=[question.qid for question in BASIC_QUESTIONS],
            answers={},
        )
        self.session.pending = pending
        head = (
            "开始推荐前，先完成 6 道计分题和 1 道一致性校验题。\n"
            "  这是投资者适当性管理的要求：没有评估风险承受能力，不能推荐或销售理财产品。\n"
            "  可以直接回复选项字母，也可以回复文字；回复「跳过」时，系统会先展示推断答案，"
            "只有你确认后才会采用。\n"
        )
        return AgentReply(head + "\n" + self._question_text(pending),
                          intent=intent.name, kind="question", needs_input=True)

    def _questionnaire_resume_text(self, paused: Pending) -> str:
        lines = [
            "检测到上次未完成的理财风险问卷。",
            f"上次进度：已答 {len(paused.answers)}/{len(BASIC_QUESTIONS)} 题。",
        ]
        if paused.answers:
            lines.append("上次已选：")
            for index, question in enumerate(BASIC_QUESTIONS, start=1):
                key = paused.answers.get(question.qid, "")
                if not key:
                    continue
                option = question.option(key)
                label = option.label if option else key.upper()
                lines.append(f"  {index}. {question.text} -> {key.upper()}. {label}")
        lines.extend([
            "",
            "请选择：",
            "  1. 继续上次记录",
            "  2. 重新开始",
            "  3. 取消本次理财流程",
        ])
        return "\n".join(lines)

    def _handle_questionnaire_resume(self, pending: Pending, text: str) -> AgentReply:
        cleaned = (text or "").strip().lower()
        if cleaned in self.ABORT_WORDS or cleaned in {"3", "取消本次"}:
            self.paused_questionnaire = None
            self.session.clear_pending()
            return AgentReply("好的，本次理财流程已取消，未记录任何问卷结果。")
        if cleaned in self.RESTART_WORDS:
            self.paused_questionnaire = None
            self.session.clear_pending()
            origin = pending.origin or Intent(pending.intent or "wealth.recommend", {})
            return self._start_questionnaire(origin)
        if cleaned in self.RESUME_WORDS:
            paused = self.paused_questionnaire
            if paused is None:
                self.session.clear_pending()
                return AgentReply("上次问卷状态已失效，请重新开始理财流程。", ok=False, kind="error")
            paused.intent = pending.intent or paused.intent
            paused.origin = pending.origin or paused.origin
            self.session.pending = paused
            if paused.kind == "questionnaire_confirm":
                return AgentReply(
                    self._questionnaire_review_text(paused),
                    intent=paused.intent,
                    kind="confirm",
                    needs_input=True,
                )
            if paused.queue:
                return AgentReply(
                    self._question_text(paused),
                    intent=paused.intent,
                    kind="question",
                    needs_input=True,
                )
            return self._finish_questionnaire(paused)
        intent = self.parser.parse(text, history=self.session.history[:-1])
        if not intent.is_unknown():
            self.paused_questionnaire = None
            self.session.clear_pending()
            return self._continue_with_intent(intent)
        return AgentReply(
            "请回复 1 继续上次记录、2 重新开始，或 3 取消。",
            intent=pending.intent,
            kind="question",
            needs_input=True,
        )

    def _question_text(self, pending: Pending) -> str:
        qid = pending.queue[0]
        question = BASIC_MAP[qid]
        total = len(BASIC_QUESTIONS)
        index = total - len(pending.queue) + 1
        lines = [f"第 {index}/{total} 题：{question.text}"]
        if question.why:
            lines.append(f"  为什么问：{question.why}")
        lines.extend(f"  {option.key.upper()}. {option.label}" for option in question.options)
        lines.append(f"请回复 A-{question.options[-1].key.upper()} 之一。")
        return "\n".join(lines)

    @staticmethod
    def _parse_option(qid: str, text: str) -> Optional[str]:
        """把用户的回答映射到选项 key，支持字母、序号、选项原文。"""
        question = BASIC_MAP.get(qid)
        if question is None:
            return None
        cleaned = (text or "").strip()
        keys = [option.key.upper() for option in question.options]
        if cleaned.upper() in keys:
            return cleaned.lower()
        if cleaned.isdigit() and 1 <= int(cleaned) <= len(question.options):
            return question.options[int(cleaned) - 1].key
        for option in question.options:
            label = option.label.replace(" ", "")
            if label and (label in cleaned.replace(" ", "") or cleaned.replace(" ", "") in label):
                return option.key
        return None

    def _handle_questionnaire(self, pending: Pending, text: str) -> AgentReply:
        cleaned = (text or "").strip()
        if cleaned in self.ABORT_WORDS:
            self.paused_questionnaire = None
            self.session.clear_pending()
            return AgentReply("好的，风险评估已取消，本次不做任何理财推荐。")
        if cleaned in self.SKIP_WORDS:
            inferred = infer_basic_answers_detailed(
                self.store.get_user(self.user_id),
                self.store,
                self.clock.today(),
            )
            pending.inferences = dict(inferred)
            for qid in list(pending.queue):
                pending.answers[qid] = inferred.get(qid).option if qid in inferred else pending.answers.get(qid, "a")
            pending.queue = []
            pending.kind = "questionnaire_confirm"
            return AgentReply(
                self._questionnaire_review_text(pending),
                intent=pending.intent,
                kind="confirm",
                needs_input=True,
            )
        else:
            qid = pending.queue[0]
            key = self._parse_option(qid, cleaned)
            if key is None:
                intent = self.parser.parse(text, history=self.session.history[:-1])
                if not intent.is_unknown():
                    self.paused_questionnaire = pending
                    self.session.clear_pending()
                    return self._continue_with_intent(intent)
                return AgentReply(
                    "这个回答我没看懂。\n" + self._question_text(pending),
                    kind="question", needs_input=True,
                )
            pending.answers[qid] = key
            pending.queue.pop(0)

        if pending.queue:
            return AgentReply(self._question_text(pending), kind="question", needs_input=True)

        return self._finish_questionnaire(pending)

    def _questionnaire_review_text(self, pending: Pending) -> str:
        lines = ["系统推断结果如下，尚未用于理财评估："]
        for question in BASIC_QUESTIONS:
            key = pending.answers.get(question.qid, "")
            option = question.option(key)
            label = option.label if option else key.upper()
            inference = pending.inferences.get(question.qid)
            lines.append(f"  {question.text}  {key.upper()}. {label}")
            if inference is not None:
                lines.append(
                    f"    依据：{inference.reason}；置信度：{inference.confidence:.0%}"
                )
        lines.append("")
        lines.append("请回复「确认」采用这组推断答案，或回复「取消」放弃。")
        return "\n".join(lines)

    def _finish_questionnaire(self, pending: Pending) -> AgentReply:
        user = self.store.get_user(self.user_id)
        result = evaluate_basic(pending.answers, user, self.store, self.clock.today())
        self.risk_answers = dict(pending.answers)
        self.paused_questionnaire = None
        self.risk_level = result.level
        self.risk_assessed_at = self.clock.iso()
        if pending.inferences:
            self.risk_inference_confirmed_at = self.clock.iso()
            pending.inference_confirmed_at = self.risk_inference_confirmed_at
            self.store.log(
                "risk.inference_confirmed",
                answers=dict(pending.answers),
                confirmed_at=self.risk_inference_confirmed_at,
            )
        self.store.log(
            "risk.assessed",
            level=result.level,
            total=result.total,
            emergency_months=result.emergency_months,
            caps=list(result.caps),
        )
        origin = pending.origin
        self.session.clear_pending()
        return self._after_questionnaire(result, origin)

    def _after_questionnaire(self, result, origin: Optional[Intent]) -> AgentReply:
        weakest = result.weakest_details(3)
        weakest_text = "、".join(
            f"{item.question} {item.answer}（{item.score:g}/{item.max_score:g}）"
            for item in weakest
        )
        summary = (
            f"评估完成：{result.label}（{result.total:.0f}/100）。\n"
            f"  可买产品：R1-R{result.level}；R{min(result.level + 1, 5)} 需签风险不匹配警示书并二次确认；"
            f"R{min(result.level + 2, 5)} 及以上禁止销售。\n"
            f"  主要拉低项：{weakest_text or '无'}\n"
            f"  建议 12 个月后或资产发生重大变化时复测。"
        )
        for note in [*result.caps, *result.consistency_notes]:
            summary += f"\n  提示：{note}"
        if result.straight_line:
            summary += "\n  请确认是否认真阅读题目；系统已记录本次一致性异常。"
        target = origin if origin is not None else Intent("wealth.risk_assess", {})
        ctx = self.context()
        plan = self.planner.plan(target, ctx)
        if plan.missing:
            self.session.pending = Pending(
                kind="slot", intent=target.name, slots=dict(target.slots),
                missing=list(plan.missing), question=plan.question,
            )
            return AgentReply(summary + "\n\n" + plan.question, intent=target.name,
                              kind="question", needs_input=True)
        reply = self._gate_and_execute(plan)
        prefix = summary
        if target.name == "wealth.risk_assess":
            prefix += ("\n  下面是根据问卷答案 + 账户数据生成的完整评分明细"
                       "（14 题口径，已作答项优先采用你的回答）。")
        reply.text = prefix + "\n\n" + reply.text
        return reply

    # ------------------------------------------------------------------
    def _unknown_reply(self, text: str) -> str:
        templates = self._suggest_templates(text)
        lines = ["这句我还没完全理解。可以照着下面的句式说："]
        lines.extend(f"  · {template}" for template in templates)
        lines.append("也可以直接说「你能做什么」查看全部功能与句式模版。")
        return "\n".join(lines)

    def _suggest_templates(self, text: str, limit: int = 4) -> List[str]:
        """按字面重合度为用户找最接近的句式模版。"""
        def bigrams(value: str) -> set:
            value = (value or "").replace(" ", "")
            return {value[i:i + 2] for i in range(len(value) - 1)} if len(value) > 1 else {value}

        target = bigrams(normalize_synonyms(text))
        scored = []
        for tool in self.registry.all():
            examples = getattr(tool, "examples", []) or []
            if not examples:
                continue
            best = 0
            for example in examples:
                best = max(best, len(target & bigrams(example)))
            best += len(target & bigrams(tool.description)) // 2
            if best:
                scored.append((best, examples[0]))
        scored.sort(key=lambda item: item[0], reverse=True)
        chosen: List[str] = []
        for _, template in scored:
            if template not in chosen:
                chosen.append(template)
            if len(chosen) >= limit:
                break
        if not chosen:
            chosen = ["给老婆转500块，用微信", "分析我这个月的账单",
                      "推荐几个基金", "我有哪些卡"]
        return chosen

    # ==================================================================
    # 槽位追问（多轮对话）
    # ==================================================================
    @classmethod
    def _revisable_amount_slot(cls, intent: str) -> str:
        return cls.REVISABLE_AMOUNT_SLOTS.get(intent, "")

    @staticmethod
    def _looks_like_amount_revision(text: str) -> bool:
        cleaned = re.sub(r"\s+", "", text or "")
        if re.fullmatch(r"[¥￥]?\d[\d,]*(?:\.\d{1,2})?(?:元|块)?", cleaned):
            return True
        return any(
            term in cleaned
            for term in (
                "改成", "改为", "调到", "调整为", "降到", "减到", "换成",
                "少转", "只转", "金额改", "金额调", "改小",
            )
        )

    def _resume_blocked_draft(self, pending: Pending, text: str) -> AgentReply:
        """Revise or replace a transfer draft rejected by the risk gate."""
        control = classify_control(text)
        if control == "deny":
            self.session.clear_pending()
            return AgentReply("好的，这笔未通过的转账草稿已取消，没有执行任何资金变动。")

        amount_slot = self._revisable_amount_slot(pending.intent)
        amount = parse_amount(text)
        if (
            amount_slot
            and amount is not None
            and float(amount) > 0
            and self._looks_like_amount_revision(text)
        ):
            slots = dict(pending.slots)
            slots[amount_slot] = float(amount)
            self.session.clear_pending()
            return self._continue_with_intent(
                Intent(pending.intent, slots, 1.0, "system")
            )

        if control == "confirm":
            current = pending.slots.get(amount_slot)
            return AgentReply(
                f"当前金额 {float(current or 0):,.2f} 元仍未通过风控，不能直接确认执行。"
                "请回复「改成3000」这样的指令调整金额，或回复「取消」放弃。",
                intent=pending.intent,
                kind="question",
                needs_input=True,
            )

        interrupt = self._interrupt_intent(text)
        if interrupt is not None:
            self.session.clear_pending()
            return self._continue_with_intent(interrupt)

        current = pending.slots.get(amount_slot)
        return AgentReply(
            f"刚才的转账草稿仍保留，当前金额为 {float(current or 0):,.2f} 元。"
            "可以直接回复「改成3000」或只回复一个新金额，我会沿用原来的收款人、"
            "渠道和付款账户重新校验；回复「取消」可放弃。",
            intent=pending.intent,
            kind="question",
            needs_input=True,
        )

    def _resume_from_slots(self, pending: Pending, text: str) -> AgentReply:
        control = classify_control(text)
        if control == "deny":
            self.session.clear_pending()
            return AgentReply("好的，本次操作已取消，没有执行任何业务动作。")
        ctx = self.context()
        current_slot_answer = None
        if pending.intent == "card.apply" and pending.missing:
            current_slot = pending.missing[0]
            current_slot_answer = self._extract_slot(current_slot, text, ctx)
        interrupt = (
            None
            if current_slot_answer is not None
            else self._interrupt_intent(text)
        )
        if interrupt is not None:
            self.session.clear_pending()
            return self._continue_with_intent(interrupt)
        self._fill_missing(pending, text, ctx)
        validation_error = self._validate_pending_slots(pending)
        if validation_error is not None:
            return validation_error
        if pending.missing:
            if pending.intent == "card.apply":
                intent = Intent(pending.intent, pending.slots, 0.9, "system")
                plan = self.planner.plan(intent, ctx)
                if plan.missing:
                    pending.missing = list(plan.missing)
                    return AgentReply(
                        plan.question,
                        intent=pending.intent,
                        kind="question",
                        needs_input=True,
                    )
            next_slot = pending.missing[0]
            return AgentReply(self.planner._question(next_slot), intent=pending.intent,
                              kind="question", needs_input=True)
        intent = Intent(pending.intent, pending.slots, 0.9, "system")
        plan = self.planner.plan(intent, ctx)
        if plan.missing:
            pending.missing = list(plan.missing)
            return AgentReply(plan.question, intent=pending.intent, kind="question", needs_input=True)
        self.session.clear_pending()
        return self._gate_and_execute(plan)

    def _validate_pending_slots(self, pending: Pending) -> Optional[AgentReply]:
        """Run deterministic checks before asking for more sensitive card data."""
        if pending.intent not in {"card.bind", "card.apply"}:
            return None
        if pending.intent == "card.apply":
            account_class = pending.slots.get("account_class")
            if account_class and account_class not in {"I类户", "II类户"}:
                pending.slots.pop("account_class", None)
                pending.missing = ["account_class"]
                return AgentReply(
                    "账户类型不符合要求，请选择一类户（I 类）或二类户（II 类）。",
                    intent=pending.intent,
                    kind="question",
                    needs_input=True,
                )
            password = pending.slots.get("card_password")
            password_confirm = pending.slots.get("card_password_confirm")
            if password:
                password_check = evaluate_card_password(password)
                if not password_check["ok"] or not password_check["acceptable"]:
                    pending.slots.pop("card_password", None)
                    pending.slots.pop("card_password_confirm", None)
                    pending.missing = ["card_password", "card_password_confirm"]
                    return AgentReply(
                        password_check["message"]
                        + "\n密码强度不足，请重新设置一个 6 位数字密码；"
                          "不要使用连续数字、重复数字或常见弱密码。",
                        intent=pending.intent,
                        kind="question",
                        needs_input=True,
                    )
            if password_confirm:
                if str(password) != str(password_confirm):
                    pending.slots.pop("card_password", None)
                    pending.slots.pop("card_password_confirm", None)
                    pending.missing = ["card_password", "card_password_confirm"]
                    return AgentReply(
                        "两次输入的密码不一致，密码未保存。请重新设置 6 位数字密码。",
                        intent=pending.intent,
                        kind="question",
                        needs_input=True,
                    )
            statement_mode = pending.slots.get("statement_mode")
            if statement_mode and statement_mode not in {"uploaded", "demo"}:
                pending.slots.pop("statement_mode", None)
                pending.missing = ["statement_mode"]
                return AgentReply(
                    "请选择流水核验方式：回复「已上传」使用当前会话的上传流水，"
                    "或回复「使用演示流水」进行模拟核验。",
                    intent=pending.intent,
                    kind="question",
                    needs_input=True,
                )
            if statement_mode == "uploaded":
                uploaded = [
                    txn for txn in self.store.transactions_of(self.user_id)
                    if "receipt_import" in txn.tags or txn.source.startswith("upload:")
                ]
                if not uploaded:
                    pending.slots.pop("statement_mode", None)
                    pending.missing = ["statement_mode"]
                    return AgentReply(
                        "当前会话没有已确认导入的消费流水。请先用回形针上传账单并完成导入，"
                        "再回复「已上传」；也可以回复「使用演示流水」。",
                        intent=pending.intent,
                        kind="question",
                        needs_input=True,
                    )
            if pending.slots.get("consent") is False:
                self.session.clear_pending()
                return AgentReply(
                    "您未同意模拟信用评估授权，本次信用卡申请未提交，也没有创建卡片。",
                    intent=pending.intent,
                    ok=False,
                    kind="reply",
                )
        if pending.intent == "card.bind" and pending.slots.get("card_no"):
            card_check = validate_card_number(
                pending.slots["card_no"],
                str(pending.slots.get("bank") or ""),
            )
            if not card_check["ok"]:
                for slot in ("card_no", "password", "phone"):
                    pending.slots.pop(slot, None)
                pending.missing = ["card_no", "password", "phone"]
                return AgentReply(
                    card_check["message"] + "\n请重新输入卡号，随后再填写密码和预留手机号。",
                    intent=pending.intent,
                    kind="question",
                    needs_input=True,
                )
        if pending.slots.get("phone"):
            phone_check = validate_phone_number(pending.slots["phone"])
            if not phone_check["ok"]:
                pending.slots.pop("phone", None)
                pending.missing = ["phone"]
                return AgentReply(
                    phone_check["message"] + "\n请重新输入银行预留的 11 位手机号。",
                    intent=pending.intent,
                    kind="question",
                    needs_input=True,
                )
        return None

    def _fill_missing(self, pending: Pending, text: str, ctx: ToolContext) -> None:
        missing = list(pending.missing)
        if pending.intent == "card.apply":
            if not missing:
                return
            slot = missing.pop(0)
            value = self._extract_slot(slot, text, ctx)
            if value is None or not self._is_valid_slot_value(slot, value):
                # Keep the answer so the card-specific validator can explain
                # whether the phone, account type, or another field is invalid.
                value = self._coerce_text(text)
            if self._is_valid_slot_value(slot, value):
                pending.slots[slot] = value
            else:
                missing.insert(0, slot)
            pending.missing = missing
            return
        remaining: List[str] = []
        filled_any = False
        for slot in missing:
            value = self._extract_slot(slot, text, ctx)
            if value is None or not self._is_valid_slot_value(slot, value):
                remaining.append(slot)
            else:
                pending.slots[slot] = value
                filled_any = True
        # 兜底：一个都没抽出来时，把整句话当作第一个缺失槽位的答案
        if not filled_any and remaining:
            slot = remaining.pop(0)
            value = self._coerce_text(text)
            if self._is_valid_slot_value(slot, value):
                pending.slots[slot] = value
            else:
                remaining.insert(0, slot)
        pending.missing = remaining

    TEXT_SLOTS = {"payee", "person", "merchant", "card", "product", "name", "keyword"}

    @classmethod
    def _is_valid_slot_value(cls, slot: str, value: Any) -> bool:
        """拦掉"把时间戳/金额当成收款人"这类脏填充。"""
        if value is None:
            return False
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return False
            if slot in cls.TEXT_SLOTS:
                if len(text) > 20:
                    return False
                if NOISE_VALUE_RE.search(text):
                    return False
        return True

    def _extract_slot(self, slot: str, text: str, ctx: ToolContext) -> Any:
        if slot == "participants":
            parts = [p.strip() for p in text.replace("，", "、").replace(",", "、").replace("和", "、").split("、")]
            names = [p for p in parts if p]
            return names if len(names) >= 2 else None
        if slot in NUMERIC_SLOTS:
            if slot == "existing_debt" and any(
                    word in text for word in ("没有", "无负债", "没负债", "零", "0元")):
                return 0.0
            amount = parse_amount(text)
            if amount is None:
                return None
            return int(amount) if slot in {"days", "risk_level"} else amount
        if slot in DATE_SLOTS:
            parsed = parse_date(text, ctx.clock.today())
            return parsed.isoformat() if parsed else None
        if slot == "enable":
            if any(w in text for w in ("开", "启用", "允许", "恢复")):
                return True
            if any(w in text for w in ("关", "禁", "停")):
                return False
            return None
        if slot == "scope":
            if "境外" in text or "海外" in text:
                return "overseas"
            if "免密" in text or "闪付" in text:
                return "contactless"
            if "线上" in text or "网络" in text:
                return "online"
            return None
        if slot == "repeat":
            if "每月" in text:
                return "monthly"
            if "每周" in text:
                return "weekly"
            return "once"
        if slot == "kind":
            if any(word in text for word in ("储蓄卡", "借记卡", "储蓄账户", "借记账户")):
                return "debit"
            if any(word in text for word in ("虚拟卡", "虚拟")):
                return "virtual"
            if "附属" in text:
                return "supplementary"
            if any(word in text for word in ("信用卡", "贷记卡")):
                return "credit"
            return None
        if slot == "account_class":
            normalized = text.upper().replace(" ", "")
            if any(word in normalized for word in ("一类", "I类", "Ⅰ类")):
                return "I类户"
            if any(word in normalized for word in ("二类", "II类", "Ⅱ类")):
                return "II类户"
            return None
        if slot == "consent":
            if any(word in text for word in ("不同意", "拒绝", "否", "不授权")):
                return False
            if any(word in text for word in ("同意", "授权", "可以", "是")):
                return True
            return None
        if slot == "statement_mode":
            if any(word in text for word in ("已上传", "上传", "导入", "已有流水", "使用已上传")):
                return "uploaded"
            if any(word in text for word in ("演示", "模拟", "示例", "没有流水", "无流水")):
                return "demo"
            return None
        if slot in {"employment", "credit_purpose", "branch"}:
            return self._coerce_text(text) or None
        if slot == "channel":
            if "微信" in text:
                return "wechat"
            if "支付宝" in text:
                return "alipay"
            if "银行卡" in text or "卡" in text:
                return "bank"
            if any(word in text for word in ("手机", "数字人民币", "钱包", "电话")):
                return "phone"
            return None
        if slot == "card_no":
            compact = re.sub(r"[\s-]", "", text)
            digits = re.findall(r"\d{16,19}", compact)
            return digits[0] if digits else None
        if slot in {"password", "card_password", "card_password_confirm"}:
            match = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
            return match.group(1) if match else None
        if slot == "phone":
            # 前后都不能再是数字，避免从 16-19 位卡号里截出"手机号"
            match = re.search(r"(?<!\d)(1[3-9]\d{9})(?!\d)", text)
            return match.group(0) if match else None
        if slot in {"payee", "person"}:
            person = self.parser.rule._person(text)
            return person or None
        if slot == "card":
            card = self.parser.rule._card(text)
            return card or None
        return None

    @staticmethod
    def _coerce_text(text: str) -> str:
        cleaned = text.strip().strip("。！!，,、 ")
        for prefix in ("是", "叫", "给", "转到", "转给"):
            if cleaned.startswith(prefix) and len(cleaned) > len(prefix):
                cleaned = cleaned[len(prefix):]
        return cleaned[:30]

    # ==================================================================
    # 风控闸门
    # ==================================================================
    def _gate_and_execute(self, plan: Plan, data_confirmed: bool = False) -> AgentReply:
        ctx = self.context()
        decisions: List[RiskDecision] = []
        blockers: List[str] = []
        for step in plan.steps:
            decision = self._assess_step(step.tool, step.args, ctx)
            decisions.append(decision)
            if decision.blocked:
                blockers.append(f"{self._tool_label(step.tool)}：{decision.summary()}")
        if blockers:
            amount_slot = self._revisable_amount_slot(plan.intent)
            revision_hint = ""
            if amount_slot:
                self.session.pending = Pending(
                    kind="blocked_draft",
                    intent=plan.intent,
                    slots=dict(plan.slots),
                    plan=plan,
                )
                current = plan.slots.get(amount_slot)
                revision_hint = (
                    f"\n\n转账草稿已保留，当前金额 {float(current or 0):,.2f} 元。"
                    "可直接回复「改成3000」修改金额，或只回复一个新金额重新校验；"
                    "回复「取消」可放弃。"
                )
            return AgentReply(
                "风控校验未通过，本次操作没有执行：\n"
                + "\n".join("  · " + b for b in blockers)
                + revision_hint,
                intent=plan.intent, ok=False, kind="error",
            )

        requires_verification = any(d.requires_verification for d in decisions)
        requires_confirmation = any(d.requires_confirmation for d in decisions)
        risk_notes = [d.summary() for d in decisions if d.reasons]
        cooling_product = self._cooling_product(plan)
        if cooling_product is not None:
            risk_notes.append(
                f"客户评级 C1，目标产品「{cooling_product.name}」为 R2；"
                "已签署风险不匹配警示提示，进入 24 小时冷静期"
            )

        if plan.intent in DATA_CONFIRM_INTENTS and not data_confirmed:
            self.session.pending = Pending(
                kind="data_confirm",
                intent=plan.intent,
                plan=plan,
                slots=dict(plan.slots),
            )
            tail = "\n\n请回复「确认」执行，或「取消」放弃。"
            if requires_verification:
                tail = ("\n\n请回复「确认」继续。确认后还需要输入 6 位短信验证码"
                        f"（演示环境固定为 {RiskPolicy.DEMO_SMS_CODE}）。")
            return AgentReply(
                self._confirm_text(plan, risk_notes, header="请核对关键信息，尚未执行：") + tail,
                intent=plan.intent,
                kind="confirm",
                needs_input=True,
                plan=plan.tools(),
            )

        if requires_confirmation and not data_confirmed:
            self.session.pending = Pending(kind="confirm", intent=plan.intent, plan=plan,
                                           slots=dict(plan.slots))
            tail = "\n\n请回复「确认」执行，或「取消」放弃。"
            if requires_verification:
                tail = ("\n\n该操作还需要验证身份：回复「确认」后请输入手机收到的 6 位短信验证码"
                        f"（演示环境固定为 {RiskPolicy.DEMO_SMS_CODE}）。")
            return AgentReply(
                self._confirm_text(plan, risk_notes) + tail,
                intent=plan.intent, kind="confirm", needs_input=True,
                plan=plan.tools(),
            )
        if requires_verification:
            self.verify_attempts = 0
            self.session.pending = Pending(kind="verify", intent=plan.intent, plan=plan,
                                           slots=dict(plan.slots))
            return AgentReply(
                self._confirm_text(plan, risk_notes)
                + f"\n\n该操作金额较大，需要验证身份。请输入手机收到的 6 位短信验证码"
                  f"（演示环境固定为 {RiskPolicy.DEMO_SMS_CODE}）：",
                intent=plan.intent, kind="verify", needs_input=True,
                plan=plan.tools(),
            )
        return self._execute(plan, ctx)

    def _after_data_confirm(self, pending: Pending) -> AgentReply:
        if pending.plan is None:
            self.session.clear_pending()
            return AgentReply("计划已失效，请重新说一次您的需求。", ok=False, kind="error")
        plan = pending.plan
        product = self._cooling_product(plan)
        if product is not None:
            available_at = (self.clock.now() + timedelta(hours=24)).isoformat(timespec="seconds")
            self.session.pending = Pending(
                kind="risk_cooling",
                intent=plan.intent,
                plan=plan,
                slots={
                    "product_id": product.product_id,
                    "available_at": available_at,
                },
            )
            self.store.log(
                "risk.cooling_started",
                product=product.name,
                risk_level=product.risk_level,
                available_at=available_at,
            )
            return AgentReply(
                f"风险不匹配警示已记录。目标产品「{product.name}」为 R2，"
                "高于您的 C1 承受能力。\n"
                f"冷静期将持续到 {available_at}。冷静期结束前不会执行申购。\n"
                "冷静期结束后回复「确认」继续，或回复「取消」放弃。",
                intent=plan.intent,
                kind="question",
                needs_input=True,
            )
        self.session.clear_pending()
        return self._gate_and_execute(plan, data_confirmed=True)

    def _handle_risk_cooling(self, pending: Pending, text: str) -> AgentReply:
        control = classify_control(text)
        if control == "deny":
            self.session.clear_pending()
            return AgentReply("好的，风险不匹配申购已取消，本次不会扣款。")
        if control is None:
            interrupt = self._interrupt_intent(text)
            if interrupt is not None:
                self.session.clear_pending()
                return self._continue_with_intent(interrupt)
        if control != "confirm":
            return AgentReply(
                "请回复「确认」检查冷静期状态，或回复「取消」放弃本次申购。",
                kind="question",
                needs_input=True,
            )
        available_at = datetime.fromisoformat(str(pending.slots.get("available_at") or ""))
        now = self.clock.now()
        if now < available_at:
            remaining = available_at - now
            hours = max(1, int(remaining.total_seconds() // 3600) + 1)
            return AgentReply(
                f"冷静期尚未结束，还需约 {hours} 小时。"
                f"可在 {available_at.isoformat(timespec='seconds')} 后再次确认。",
                kind="question",
                needs_input=True,
            )
        if pending.plan is None:
            self.session.clear_pending()
            return AgentReply("计划已失效，请重新发起申购。", ok=False, kind="error")
        plan = pending.plan
        self.store.log(
            "risk.cooling_completed",
            intent=plan.intent,
            completed_at=self.clock.iso(),
        )
        self.session.clear_pending()
        return self._gate_and_execute(plan, data_confirmed=True)

    def _cooling_product(self, plan: Plan):
        if plan.intent != "wealth.subscribe" or self.risk_level != 1:
            return None
        step = next((item for item in plan.steps if item.tool == "wealth.subscribe"), None)
        if step is None:
            return None
        product = self.providers.wealth.find(str(step.args.get("product") or ""))
        return product if product is not None and product.risk_level == 2 else None

    def _assess_step(self, tool_name: str, args: Dict[str, Any], ctx: ToolContext) -> RiskDecision:
        if tool_name == "transfer.send":
            user = ctx.user
            payer = resolve_account(ctx, str(args.get("account", "")))
            payee = ctx.providers.payees.find(user.user_id, str(args.get("payee", "")))
            return self.risk.check_transfer(user, payer, payee, str(args.get("payee", "")),
                                            float(args.get("amount", 0) or 0))
        tool = self.registry.get(tool_name)
        account = None
        if tool_name in {"wealth.subscribe"}:
            account = resolve_account(ctx, str(args.get("account", "")))
        decision = self.risk.check_operation(tool_name, args, ctx.user, account)
        if tool is not None and tool.requires_confirmation and not decision.requires_confirmation:
            decision.requires_confirmation = True
            decision.level = "high" if tool.risk_level == "high" else decision.level
            if not decision.reasons:
                decision.reasons.append(f"{tool.description}（需要二次确认）")
        return decision

    @staticmethod
    def _tool_label(tool_name: str) -> str:
        return {
            "transfer.send": "转账",
            "goal.create": "锁定资金",
            "wealth.subscribe": "理财申购",
        }.get(tool_name, tool_name)

    def _confirm_text(
        self,
        plan: Plan,
        risk_notes: List[str],
        header: str = "",
    ) -> str:
        header = header or ("以下操作需要您确认：" if plan.description else "请确认本次操作：")
        lines = [header]
        if plan.description:
            lines.append(plan.description)
        if risk_notes:
            lines.append("")
            lines.append("风控提示：")
            lines.extend("  · " + note for note in risk_notes)
        return "\n".join(lines)

    def _after_confirm(self, pending: Pending) -> AgentReply:
        ctx = self.context()
        decisions = [self._assess_step(s.tool, s.args, ctx) for s in (pending.plan.steps if pending.plan else [])]
        if any(d.requires_verification for d in decisions):
            pending.kind = "verify"
            self.verify_attempts = 0
            return AgentReply(
                f"请输入手机收到的 6 位短信验证码（演示环境固定为 {RiskPolicy.DEMO_SMS_CODE}）：",
                intent=pending.intent, kind="verify", needs_input=True,
            )
        if pending.plan is None:
            self.session.clear_pending()
            return AgentReply("计划已失效，请重新说一次您的需求。", ok=False, kind="error")
        self.session.clear_pending()
        return self._execute(pending.plan, ctx)

    def _handle_verify(self, pending: Pending, text: str) -> AgentReply:
        if classify_control(text) == "deny":
            self.session.clear_pending()
            return AgentReply("好的，已验证操作已取消，资金没有变动。")
        interrupt = self._interrupt_intent(text)
        if interrupt is not None:
            self.session.clear_pending()
            return self._continue_with_intent(interrupt)
        code = "".join(ch for ch in text if ch.isdigit())
        valid = code == RiskPolicy.DEMO_SMS_CODE
        if not valid:
            self.verify_attempts += 1
            if self.verify_attempts >= self.MAX_VERIFY_ATTEMPTS:
                self.session.clear_pending()
                return AgentReply("验证码连续错误，操作已终止。如非本人操作，建议立即修改密码并冻结卡片。",
                                  ok=False, kind="error")
            return AgentReply(
                f"验证码不正确，请重新输入 6 位数字（演示环境固定为 {RiskPolicy.DEMO_SMS_CODE}）。",
                kind="verify", needs_input=True,
            )
        if pending.plan is None:
            self.session.clear_pending()
            return AgentReply("计划已失效，请重新说一次您的需求。", ok=False, kind="error")
        self.session.clear_pending()
        return self._execute(pending.plan, self.context())

    # ==================================================================
    # 工具执行
    # ==================================================================
    def _execute(self, plan: Plan, ctx: ToolContext) -> AgentReply:
        trace: List[Dict[str, Any]] = []
        messages: List[str] = []
        summaries: List[str] = []
        payload: Dict[str, Any] = {}
        ok = True
        for index, step in enumerate(plan.steps, start=1):
            tool = self.registry.get(step.tool)
            if tool is None:
                messages.append(f"未找到工具 {step.tool}")
                ok = False
                break
            try:
                result: ToolResult = tool.run(ctx, **step.args)
            except TypeError as exc:
                result = ToolResult(False, f"参数不匹配：{exc}")
            except Exception as exc:
                result = ToolResult(False, f"执行出错：{exc}")
            trace.append({
                "step": index,
                "tool": step.tool,
                "args": {
                    key: ("******" if key in {
                        "password", "card_password", "card_password_confirm",
                    } else value)
                    for key, value in step.args.items()
                },
                "ok": result.ok,
                "summary": result.summary,
            })
            if len(plan.steps) > 1:
                messages.append(f"[{index}/{len(plan.steps)}] {result.message}")
            else:
                messages.append(result.message)
            if result.ok:
                summaries.append(result.summary)
                payload.update(result.data or {})
            else:
                ok = False
                break
        self.store.log("agent.execute", intent=plan.intent, tools=plan.tools(), ok=ok)
        text = "\n\n".join(messages) if len(plan.steps) > 1 else (messages[0] if messages else "操作已完成。")
        follow_up = payload.get("follow_up") if ok else None
        if follow_up:
            # 工具执行到一半需要用户确认下一步（例如"识别完账单，是否导入流水"）
            follow_plan = Plan(
                intent=str(follow_up.get("intent") or plan.intent),
                steps=[PlanStep(tool=follow_up["tool"], args=dict(follow_up.get("args") or {}))],
                description="  · " + str(follow_up.get("description", "")),
            )
            self.session.pending = Pending(kind="confirm", intent=follow_plan.intent,
                                           plan=follow_plan, slots={})
            text += "\n\n请回复「确认」继续执行，或「取消」放弃。"
            return AgentReply(text, intent=plan.intent, ok=ok, kind="confirm",
                              trace=trace, plan=plan.tools(), data=payload, needs_input=True)
        return AgentReply(text, intent=plan.intent, ok=ok, kind="executed",
                          trace=trace, plan=plan.tools(), data=payload)

