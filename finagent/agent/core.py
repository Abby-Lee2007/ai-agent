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
from finagent.data.store import Clock, DataStore
from finagent.data.market import MarketDataProvider
from finagent.data.providers import build_data_providers
from finagent.nlu.intent_parser import IntentParser
from finagent.nlu.intent_parser import normalize_synonyms
from finagent.nlu.intent_schema import Intent
from finagent.nlu.llm_client import LLMClient
from finagent.risk.engine import RiskDecision, RiskEngine
from finagent.risk.questionnaire import (
    BASIC_MAP,
    BASIC_QUESTIONS,
    evaluate_basic,
    infer_basic_answers_detailed,
)
from finagent.tools.base import ToolContext, ToolResult
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


class BankingAgent:
    """银行智能代理。"""

    MAX_VERIFY_ATTEMPTS = 3

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
        """处理一句用户输入。"""
        raw = (text or "").strip()
        if not raw:
            return AgentReply("请说点什么吧，例如「给老婆转2000块」。", kind="question", needs_input=True)

        self.session.turn += 1
        self.session.remember_user(raw)
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
            {"turn": self.session.turn, "user": raw, "agent": reply.text,
             "intent": reply.intent, "tools": [t.get("tool") for t in reply.trace]}
        )
        return reply

    # ==================================================================
    # 主路由
    # ==================================================================
    def _route(self, text: str) -> AgentReply:
        pending = self.session.pending

        if pending is not None and pending.kind == "risk_cooling":
            return self._handle_risk_cooling(pending, text)

        if pending is not None and pending.kind == "data_confirm":
            control = classify_control(text)
            if control == "confirm":
                return self._after_data_confirm(pending)
            if control == "deny":
                self.session.clear_pending()
                return AgentReply("好的，本次操作已取消，没有执行任何业务动作。")

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
                self.session.clear_pending()
                return AgentReply("好的，系统推断结果未采用，本次不做任何理财推荐。")
            return AgentReply(
                "请回复「确认」采用这组系统推断答案，或回复「取消」放弃。",
                kind="question",
                needs_input=True,
            )

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

    @classmethod
    def _needs_risk_profile(cls, intent_name: str) -> bool:
        return intent_name in cls.RISK_GATED_INTENTS

    def _start_questionnaire(self, intent: Intent) -> AgentReply:
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
    def _resume_from_slots(self, pending: Pending, text: str) -> AgentReply:
        ctx = self.context()
        self._fill_missing(pending, text, ctx)
        if pending.missing:
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

    def _fill_missing(self, pending: Pending, text: str, ctx: ToolContext) -> None:
        missing = list(pending.missing)
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
            digits = re.findall(r"\d{16,19}", text)
            return digits[0] if digits else None
        if slot == "password":
            match = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
            return match.group(1) if match else None
        if slot == "phone":
            match = re.search(r"1[3-9]\d{9}", text)
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
            return AgentReply(
                "风控校验未通过，本次操作没有执行：\n" + "\n".join("  · " + b for b in blockers),
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
                "args": step.args,
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

