"""投资者风险承受能力评估问卷（监管口径评分细则）。

说明：这里实现的是**评分框架与细则**，不是监管发布的官方问卷原文。
框架对齐《证券期货投资者适当性管理办法》的常见做法：
    5 个维度 -> 14 道题 -> 加权打分（满分 100）-> 客户等级 C1-C5

客户等级 C1-C5 与产品 R1-R5 是两套东西：
    C = 客户能承受多大风险；R = 产品有多大风险。
匹配规则：
    C1 只能买 R1；C2 可买 R1-R2；C3 可买 R1-R3；C4 可买 R1-R4；C5 可买 R1-R5。
超出但只高一级 -> 允许购买，但必须签《风险不匹配警示书》并二次确认；
高两级及以上 -> 直接拒绝。

每道题都带分值和评分依据，结果可解释、可复核，便于合规审查。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple

from finagent.data.models import User
from finagent.data.store import DataStore

CUSTOMER_LABELS = {
    1: "C1 保守型",
    2: "C2 稳健型",
    3: "C3 平衡型",
    4: "C4 成长型",
    5: "C5 进取型",
}
LEVEL_THRESHOLDS: List[Tuple[float, int]] = [(87, 5), (73, 4), (55, 3), (40, 2), (0, 1)]


@dataclass
class Option:
    key: str
    label: str
    score: float


@dataclass
class Question:
    qid: str
    dimension: str
    text: str
    max_score: float
    options: List[Option]

    def option(self, key: str) -> Optional[Option]:
        for option in self.options:
            if option.key == key:
                return option
        return None


@dataclass
class Detail:
    qid: str
    dimension: str
    question: str
    answer: str
    score: float
    max_score: float
    inferred: bool = False


@dataclass
class QuestionnaireResult:
    total: float
    level: int
    label: str
    details: List[Detail] = field(default_factory=list)
    dimension_totals: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    answered: int = 0
    inferred: int = 0

    @property
    def max_total(self) -> float:
        return 100.0

    def blocking_reason(self) -> str:
        return (f"客户等级 {self.label}（{self.total:.0f} 分），"
                f"可匹配 R1-R{min(self.level, 5)}")


QUESTIONS: List[Question] = [
    # ---------- 财务状况 25 ----------
    Question("q1", "财务状况", "您的年收入大约是多少？", 8, [
        Option("a", "10 万以下", 2), Option("b", "10-30 万", 5),
        Option("c", "30-100 万", 7), Option("d", "100 万以上", 8)]),
    Question("q2", "财务状况", "可用于投资的金融资产大约多少？", 8, [
        Option("a", "10 万以下", 2), Option("b", "10-50 万", 5),
        Option("c", "50-300 万", 7), Option("d", "300 万以上", 8)]),
    Question("q3", "财务状况", "负债占家庭资产的比例？", 5, [
        Option("a", "50% 以上", 1), Option("b", "30%-50%", 3),
        Option("c", "10%-30%", 4), Option("d", "10% 以下", 5)]),
    Question("q4", "财务状况", "家庭负担情况？", 4, [
        Option("a", "较重（需赡养老人/抚养子女）", 1),
        Option("b", "一般", 2), Option("c", "较轻", 4)]),
    # ---------- 投资经验 30（含投资知识） ----------
    Question("q5", "投资经验", "您的最高学历？", 5, [
        Option("a", "高中及以下", 2), Option("b", "大专", 3),
        Option("c", "本科", 4), Option("d", "硕士及以上", 5)]),
    Question("q6", "投资经验", "是否有金融从业经历或专业证书？", 5, [
        Option("a", "没有", 1), Option("b", "学过相关课程", 3),
        Option("c", "金融从业或持证", 5)]),
    Question("q7", "投资经验", "对以下产品的了解程度？", 10, [
        Option("a", "只了解存款和货币基金", 2),
        Option("b", "了解债券和基金", 5),
        Option("c", "了解股票和 ETF", 8),
        Option("d", "了解期权、期货等衍生品", 10)]),
    Question("q8", "投资经验", "您的投资年限？", 5, [
        Option("a", "没有经验", 0), Option("b", "1 年以内", 2),
        Option("c", "1-3 年", 3), Option("d", "3-5 年", 4), Option("e", "5 年以上", 5)]),
    Question("q9", "投资经验", "买过风险最高的产品是？", 5, [
        Option("a", "只有存款/货币基金", 1), Option("b", "债券基金", 2),
        Option("c", "股票或股票基金", 3), Option("d", "可转债/期权/期货", 5)]),
    # ---------- 风险态度 20 ----------
    Question("q10", "风险态度", "能接受的最大本金亏损？", 10, [
        Option("a", "不能接受亏损", 2), Option("b", "10% 以内", 5),
        Option("c", "20% 左右", 7), Option("d", "30% 左右", 9),
        Option("e", "50% 以上", 10)]),
    Question("q11", "风险态度", "您的投资目标更接近？", 10, [
        Option("a", "保本，跑赢通胀即可", 2), Option("b", "稳健增值", 5),
        Option("c", "追求较高收益，能接受波动", 7), Option("d", "追求高收益", 10)]),
    # ---------- 投资期限 15 ----------
    Question("q12", "投资期限", "这笔钱大概多久不用？", 8, [
        Option("a", "6 个月以内", 2), Option("b", "6-12 个月", 4),
        Option("c", "1-3 年", 6), Option("d", "3 年以上", 8)]),
    Question("q13", "投资期限", "这笔钱的用途？", 7, [
        Option("a", "随时可能要用", 1), Option("b", "1 年内有明确用途", 3),
        Option("c", "长期不用的闲钱", 7)]),
    # ---------- 流动性需求 10 ----------
    Question("q14", "流动性需求", "现有活期资金能覆盖几个月支出？", 10, [
        Option("a", "不足 1 个月", 0), Option("b", "1-3 个月", 3),
        Option("c", "3-6 个月", 7), Option("d", "6 个月以上", 10)]),
]

QUESTION_MAP = {q.qid: q for q in QUESTIONS}

# ---------------------------------------------------------------------------
# 基础版问卷（6 题）：理财操作前的强制准入，约 1 分钟填完
# ---------------------------------------------------------------------------
BASIC_QUESTIONS: List[Question] = [
    Question("b1", "财务状况", "您的年收入大约是多少？", 20, [
        Option("a", "10 万以下", 5), Option("b", "10-30 万", 12),
        Option("c", "30-100 万", 17), Option("d", "100 万以上", 20)]),
    Question("b2", "财务状况", "可用于投资的金融资产大约多少？", 20, [
        Option("a", "10 万以下", 5), Option("b", "10-50 万", 12),
        Option("c", "50-300 万", 17), Option("d", "300 万以上", 20)]),
    Question("b3", "投资经验", "您的投资年限？", 15, [
        Option("a", "没有经验", 0), Option("b", "1 年以内", 5),
        Option("c", "1-3 年", 9), Option("d", "3-5 年", 12), Option("e", "5 年以上", 15)]),
    Question("b4", "风险态度", "能接受的最大本金亏损？", 20, [
        Option("a", "不能接受亏损", 4), Option("b", "10% 以内", 10),
        Option("c", "20% 左右", 14), Option("d", "30% 左右", 18),
        Option("e", "50% 以上", 20)]),
    Question("b5", "投资期限", "这笔钱大概多久不用？", 15, [
        Option("a", "6 个月以内", 3), Option("b", "6-12 个月", 7),
        Option("c", "1-3 年", 11), Option("d", "3 年以上", 15)]),
    Question("b6", "流动性需求", "现有活期资金能覆盖几个月支出？", 10, [
        Option("a", "不足 1 个月", 0), Option("b", "1-3 个月", 3),
        Option("c", "3-6 个月", 7), Option("d", "6 个月以上", 10)]),
]
BASIC_MAP = {q.qid: q for q in BASIC_QUESTIONS}
# 基础 6 题与完整 14 题的对应关系（用于把快评答案带进完整明细）
BASIC_TO_FULL = {"b1": "q1", "b2": "q2", "b3": "q8", "b4": "q10", "b5": "q12", "b6": "q14"}


def expand_basic_answers(answers: Optional[Dict[str, str]]) -> Dict[str, str]:
    return {BASIC_TO_FULL[key]: value for key, value in (answers or {}).items()
            if key in BASIC_TO_FULL}


def _monthly_expense(store: DataStore, user_id: str, today: date) -> float:
    from datetime import timedelta
    start = (today - timedelta(days=90)).isoformat()
    txns = store.transactions_of(user_id, start, today.isoformat())
    expense = sum(-t.amount for t in txns if t.amount < 0 and t.category not in {"理财", "转账"})
    return round(expense / 3, 2) if expense else 0.0


def _pick(value: float, cuts: List[float], keys: List[str]) -> str:
    """按阈值把数值映射到选项 key。"""
    for cut, key in zip(cuts, keys):
        if value < cut:
            return key
    return keys[-1]


def infer_basic_answers(user: User, store: DataStore, today: date) -> Dict[str, str]:
    """从账户数据推断基础 6 题的答案（用于"跳过问卷"）。"""
    full = infer_answers(user, store, today)
    return {
        "b1": full["q1"],
        "b2": full["q2"],
        "b3": full["q8"],
        "b4": full["q10"],
        "b5": full["q12"],
        "b6": full["q14"],
    }


def infer_answers(user: User, store: DataStore, today: date) -> Dict[str, str]:
    """从客户已有数据推断问卷答案，让测评可以一句话跑完。"""
    accounts = store.accounts_of(user.user_id)
    total_assets = store.total_assets(accounts)
    liquid = sum(a.balance for a in accounts if a.kind in {"current", "wallet"})
    monthly = _monthly_expense(store, user.user_id, today) or max(user.monthly_income * 0.6, 1.0)
    holdings = store.holdings_of(user.user_id, status="holding")
    products = [store.products[h.product_id] for h in holdings if h.product_id in store.products]

    annual_income = user.monthly_income * 12
    answers = {
        "q1": _pick(annual_income, [100_000, 300_000, 1_000_000], ["a", "b", "c", "d"]),
        "q2": _pick(total_assets, [100_000, 500_000, 3_000_000], ["a", "b", "c", "d"]),
        "q4": "b",
        "q5": "c",
        "q6": "a",
    }

    credit = [a for a in accounts if a.kind == "credit" and a.credit_limit > 0]
    if credit:
        ratio = sum(a.balance for a in credit) / (sum(a.credit_limit for a in credit) or 1)
        answers["q3"] = "a" if ratio >= 0.5 else "b" if ratio >= 0.3 else "c" if ratio >= 0.1 else "d"
    else:
        answers["q3"] = "d"

    max_risk = max((p.risk_level for p in products), default=0)
    if max_risk >= 5:
        answers["q7"], answers["q9"] = "d", "d"
    elif max_risk >= 4:
        answers["q7"], answers["q9"] = "c", "c"
    elif max_risk >= 2:
        answers["q7"], answers["q9"] = "b", "b"
    else:
        answers["q7"], answers["q9"] = "a", "a"

    years = 0.0
    if holdings:
        earliest = min(date.fromisoformat(h.purchase_date[:10]) for h in holdings)
        years = max((today - earliest).days, 0) / 365.0
    answers["q8"] = ("a" if years <= 0 else "b" if years < 1 else "c" if years < 3
                     else "d" if years < 5 else "e")

    attitude = {"保守型": ("a", "a"), "稳健型": ("b", "b"), "平衡型": ("c", "b"),
                "成长型": ("d", "c"), "进取型": ("e", "d")}.get(user.risk_profile, ("c", "b"))
    answers["q10"], answers["q11"] = attitude

    age = user.age or 35
    answers["q12"] = "d" if age < 35 else "c" if age < 50 else "b"
    answers["q13"] = "c" if liquid / max(total_assets, 1) < 0.35 else "b"
    months = liquid / monthly if monthly else 0
    answers["q14"] = "d" if months >= 6 else "c" if months >= 3 else "b" if months >= 1 else "a"
    return answers


def evaluate(
    answers: Optional[Dict[str, str]] = None,
    user: Optional[User] = None,
    store: Optional[DataStore] = None,
    today: Optional[date] = None,
) -> QuestionnaireResult:
    """按答案打分；未提供的题目用客户数据推断。"""
    today = today or date.today()
    inferred = infer_answers(user, store, today) if (user is not None and store is not None) else {}
    given = {k: v for k, v in (answers or {}).items() if k in QUESTION_MAP}
    details: List[Detail] = []
    total = 0.0
    dimension_totals: Dict[str, Tuple[float, float]] = {}
    for question in QUESTIONS:
        key = given.get(question.qid) or inferred.get(question.qid)
        option = question.option(key) if key else None
        if option is None:
            option = question.options[0]
            key = question.options[0].key
        is_inferred = question.qid not in given
        details.append(Detail(question.qid, question.dimension, question.text,
                              option.label, option.score, question.max_score, is_inferred))
        total += option.score
        earned, maximum = dimension_totals.get(question.dimension, (0.0, 0.0))
        dimension_totals[question.dimension] = (earned + option.score, maximum + question.max_score)
    total = round(total, 1)
    level = 3
    for threshold, mapped in LEVEL_THRESHOLDS:
        if total >= threshold:
            level = mapped
            break
    return QuestionnaireResult(
        total=total,
        level=level,
        label=CUSTOMER_LABELS[level],
        details=details,
        dimension_totals=dimension_totals,
        answered=len(given),
        inferred=len(QUESTIONS) - len(given),
    )


def evaluate_basic(
    answers: Optional[Dict[str, str]] = None,
    user: Optional[User] = None,
    store: Optional[DataStore] = None,
    today: Optional[date] = None,
) -> QuestionnaireResult:
    """基础 6 题问卷评分（理财操作前的准入评估）。"""
    today = today or date.today()
    inferred = infer_basic_answers(user, store, today) if (user is not None and store is not None) else {}
    given = {k: v for k, v in (answers or {}).items() if k in BASIC_MAP}
    details: List[Detail] = []
    total = 0.0
    dimension_totals: Dict[str, Tuple[float, float]] = {}
    for question in BASIC_QUESTIONS:
        key = given.get(question.qid) or inferred.get(question.qid)
        option = question.option(key) if key else None
        if option is None:
            option = question.options[0]
        details.append(Detail(question.qid, question.dimension, question.text,
                              option.label, option.score, question.max_score,
                              question.qid not in given))
        total += option.score
        earned, maximum = dimension_totals.get(question.dimension, (0.0, 0.0))
        dimension_totals[question.dimension] = (earned + option.score, maximum + question.max_score)
    total = round(total, 1)
    level = 3
    for threshold, mapped in LEVEL_THRESHOLDS:
        if total >= threshold:
            level = mapped
            break
    return QuestionnaireResult(
        total=total, level=level, label=CUSTOMER_LABELS[level],
        details=details, dimension_totals=dimension_totals,
        answered=len(given), inferred=len(BASIC_QUESTIONS) - len(given),
    )


def match_product(customer_level: int, product_risk: int) -> Tuple[bool, bool, str]:
    """返回 (是否允许, 是否需要警示确认, 说明)。"""
    if product_risk <= customer_level:
        return True, False, "风险等级在承受范围内"
    if product_risk == customer_level + 1:
        return True, True, "超出承受能力一级，需签署风险不匹配警示书并二次确认"
    return False, False, f"超出承受能力 {product_risk - customer_level} 级，禁止销售"
