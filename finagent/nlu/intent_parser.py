"""意图识别与槽位抽取。

采用"大模型优先 + 规则兜底"的双通道设计：
- 配置了 API Key 时，由大模型做语义理解，覆盖口语化、长句、隐含表达；
- 没有 Key 或调用失败时，自动回退到内置规则解析器，保证离线可演示。

两个通道的结果会合并：大模型给意图，规则补全它漏掉的槽位（例如金额、日期）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from finagent.nlu.intent_schema import INTENTS, Intent, catalog_text, slot_dictionary_text
from finagent.nlu.llm_client import LLMClient
from finagent.nlu.capability_catalog import (
    resolve_capability_intent,
    resolve_capability_label,
)
from finagent.nlu.membership_parser import (
    PRICE_RE,
    RENEWAL_RE,
    TERM_RE,
    parse_membership_items,
)
from finagent.utils import parse_amount, parse_date, parse_period

NAME_CORE = r"[\u4e00-\u9fa5]{2,4}"
RE_PHONE = re.compile(r"1[3-9]\d{9}")
RE_NAME_AFTER_GIVE = re.compile(
    rf"(?:给|向|帮|转给|付给|打给|发给)({NAME_CORE}?)(?=转|打|汇|付|发|了|一|笔|$|[，,、。\s]|\d)"
)
RE_NAME_BEFORE_VERB = re.compile(rf"({NAME_CORE}?)(?=转|汇款|付钱|付款|收款)")
RE_WHEN_PATTERNS = [
    re.compile(r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日号]?)"),
    re.compile(r"(下个?月\d{1,2}[日号])"),
    re.compile(r"(本月\d{1,2}[日号])"),
    re.compile(r"(下+周[一二三四五六日天])"),
    re.compile(r"(周[一二三四五六日天])"),
    re.compile(r"(今天|明天|后天|大后天)"),
    re.compile(r"(\d{1,2}月\d{1,2}[日号])"),
    re.compile(r"(\d{1,2}[日号])"),
    re.compile(r"(月底|月初)"),
]
RE_NOTE = re.compile(r"备注[：:是为]?\s*([^\s，。；;]{1,20})")
# 账单图/小票文件的路径：Windows 绝对路径、带扩展名的 Unix 路径、或带后缀的文件名
# 注意：Unix 分支必须要求扩展名，否则"信用卡/虚拟卡/附属卡"这种列表会被误判成路径
RE_RECEIPT_PATH = re.compile(
    r"([A-Za-z]:[\\/][^\s，。；;\"'）)]+"
    r"|/[^\s，。；;\"'）)]*\.(?:png|jpe?g|bmp|webp|tiff?|gif|txt|csv|md|tsv)"
    r"|[\w\u4e00-\u9fa5./\\-]+\.(?:png|jpe?g|bmp|webp|tiff?|gif|txt|csv|md|tsv))",
    re.IGNORECASE,
)

RELATION_WORDS = [
    "老婆", "老公", "爱人", "妻子", "丈夫", "配偶", "太太", "伴侣", "媳妇",
    "妈妈", "爸爸", "母亲", "父亲", "房东", "老师", "老板",
    "女朋友", "男朋友", "未婚妻", "未婚夫",
]
GENERIC_PERSON_VALUES = {"他人", "别人", "对方", "某人", "有人", "某个人", "谁"}

# 同义说法归一：用户不会总用我们预设的动词
SYNONYMS = [
    ("申领", "申请"), ("申办", "申请"), ("领取", "申请"), ("办理", "办"),
    ("开通一张", "开一张"), ("开通个", "开个"), ("弄一张", "办一张"),
    ("查看", "查"), ("查询", "查"), ("看一下", "看看"), ("瞧瞧", "看看"),
]


def normalize_synonyms(text: str) -> str:
    result = text or ""
    for old, new in SYNONYMS:
        result = result.replace(old, new)
    return result


# 通用兜底关键词：精确规则都没命中时，用这些词判断用户大概想办什么
INTENT_KEYWORDS = {
    "transfer.send": ["转账", "转给", "打钱", "汇款", "发红包"],
    "transfer.internal": ["本人转账", "内部转账", "卡片互转", "银行卡互转"],
    "transfer.schedule": ["定时转", "自动转", "每月转", "每周转", "预约转"],
    "transfer.split_aa": ["aa", "分摊", "平摊", "均摊", "各付"],
    "transfer.aa_status": ["aa收款进度", "aa状态", "谁还没付款"],
    "transfer.list_scheduled": ["定时转账", "待执行", "预约任务"],
    "bill.analyze": ["账单", "消费", "支出", "开销", "花销"],
    "bill.anomaly": ["异常", "可疑", "盗刷", "重复扣", "乱扣"],
    "bill.report": ["账单报告", "月报", "年报", "年度账单"],
    "bill.search": ["花了多少", "消费了多少", "花在"],
    "bill.scan_receipt": ["截图", "小票", "收据", "识别账"],
    "bill.supported_formats": ["上传哪些格式", "支持哪些格式", "账单格式", "文件格式", "能识别哪些文件"],
    "forecast.cashflow": ["现金流", "数字孪生", "未来90天", "资金缺口", "财务预测"],
    "advisor.proactive": ["主动财务", "财务体检", "个人cfo", "主动提醒"],
    "advisor.health_score": ["财务健康分", "财务健康评分", "财务评分"],
    "macro.official": [
        "官方宏观数据", "国债收益率", "央行逆回购", "公开市场操作",
        "中国cpi", "人民银行数据",
    ],
    "market.overview": [
        "a股行情", "大盘怎么样", "上证指数", "深证成指", "创业板指",
        "科创50", "北证50", "两市成交额",
    ],
    "advisor.events": ["主动事件", "财务通知", "事件中心", "稍后提醒"],
    "risk.dispute": ["发起争议交易", "提交争议交易", "申请交易争议"],
    "risk.playbook": ["风险处置", "交易处置", "盗刷怎么办", "争议交易"],
    "account.reconcile_adjust": ["修复账务差异", "调整对账差异", "手工对账修正"],
    "account.reconcile": ["账务对账", "账实一致", "核对余额"],
    "wealth.risk_assess": ["风险测评", "风险承受", "测评"],
    "wealth.analytics": ["最大回撤", "组合风险", "产品风险分析", "波动率"],
    "wealth.compare": ["产品对比", "比较产品", "对比理财", "有什么区别", "怎么选"],
    "wealth.recommend": ["推荐", "理财产品", "配置", "买什么"],
    "wealth.subscribe": ["申购", "买入", "认购", "购买"],
    "wealth.redeem": ["赎回", "卖出"],
    "wealth.portfolio": ["持仓", "赚了多少", "收益"],
    "card.list": ["有哪些卡", "卡列表", "我的卡"],
    "card.statement": ["信用卡账单", "本期应还", "最低还款", "还款日", "账单日"],
    "card.timeline": ["申请进度", "卡片进度", "审批时间线", "虚拟卡卡号", "虚拟卡安全码"],
    "card.repay": ["信用卡还款", "还信用卡", "偿还信用卡"],
    "card.apply": ["申领", "办卡", "开卡", "申请卡", "领卡"],
    "card.bind": ["绑定", "绑卡", "添加卡"],
    "card.limit_adjust": ["额度", "限额", "提额", "降额"],
    "card.control": ["境外交易", "线上交易", "免密", "闪付"],
    "card.freeze": ["冻结", "锁卡"],
    "card.unlock": ["解冻", "解锁"],
    "card.report_lost": ["挂失", "丢了", "补卡"],
    "subscription.detect": ["自动续费", "自动扣", "偷偷扣", "订阅扣", "扣钱"],
    "subscription.list": ["订阅", "会员"],
    "subscription.cancel": ["取消订阅", "退订", "取消会员"],
    "subscription.resume": ["恢复订阅", "恢复自动续费", "重新开通", "续回去"],
    "subscription.change_cycle": ["改周期", "改成续费", "调整续费", "更换套餐"],
    "membership.purchase": ["充会员", "购买会员", "开通会员", "会员充值"],
    "subscription.remind": ["续费", "快到期", "扣费提醒"],
    "goal.create": ["攒钱", "锁定资金", "资金目标"],
    "goal.list": ["我的目标", "目标进度"],
    "crossscene.birthday_plan": ["生日", "惊喜", "礼物"],
    "crossscene.daily_brief": ["简报", "每日", "要注意"],
    "account.balance": ["余额", "多少钱", "总资产"],
    "profile.show": ["我的资料", "客户画像"],
    "help": ["能做什么", "有什么功能"],
    "agent.about": ["什么模型", "你是谁", "怎么实现"],
}


class RuleIntentParser:
    """基于关键词与正则的离线意图解析器。

    规则表按优先级排列：越靠前的规则越具体，避免被宽泛规则抢走。
    想新增场景时，在 `RULES` 里加一行、再补一个 `_match_xxx` 方法即可。
    """

    RULES: List[str] = [
        "bill.supported_formats",
        "help",
        "agent.about",
        "crossscene.daily_brief",
        "crossscene.gift_plan",
        "bill.scan_receipt",
        "advisor.proactive",
        "advisor.health_score",
        "macro.official",
        "market.overview",
        "advisor.events",
        "forecast.cashflow",
        "risk.dispute",
        "risk.playbook",
        "account.reconcile_adjust",
        "account.reconcile",
        "transfer.aa_status",
        "wealth.compare",
        "wealth.analytics",
        "card.repay",
        "card.timeline",
        "card.statement",
        "transfer.split_aa",
        "transfer.list_scheduled",
        "transfer.internal",
        "transfer.schedule",
        "transfer.send",
        "bill.anomaly",
        "bill.report",
        "bill.search",
        "account.balance",
        "bill.analyze",
        "wealth.risk_assess",
        "wealth.redeem",
        "wealth.subscribe",
        "wealth.portfolio",
        "wealth.recommend",
        "card.report_lost",
        "card.unlock",
        "card.freeze",
        "card.bind",
        "card.control",
        "card.limit_adjust",
        "card.apply",
        "card.list",
        "subscription.change_cycle",
        "membership.purchase",
        "subscription.resume",
        "subscription.cancel",
        "subscription.remind",
        "subscription.detect",
        "subscription.list",
        "crossscene.birthday_plan",
        "crossscene.gift_plan",
        "goal.list",
        "goal.create",
        "profile.show",
    ]

    def __init__(self, store=None, user_id: str = "") -> None:
        self.store = store
        self.user_id = user_id

    def parse(self, text: str) -> Intent:
        raw = (text or "").strip()
        label_intent = resolve_capability_label(raw)
        cleaned = normalize_synonyms(raw)
        if not cleaned:
            return Intent("unknown", {}, 0.0, "rule", cleaned)
        if label_intent:
            return Intent(
                label_intent,
                self._slots(label_intent, cleaned),
                0.95,
                "rule-label",
                cleaned,
            )
        for name in self.RULES:
            matcher = getattr(self, "_match_" + name.replace(".", "_"))
            if matcher(cleaned):
                return Intent(name, self._slots(name, cleaned), 0.85, "rule", cleaned)
        # 通用兜底：按业务关键词打分，避免"必须用同一个句式"
        guess = resolve_capability_intent(cleaned) or self._keyword_guess(cleaned)
        if guess:
            return Intent(guess, self._slots(guess, cleaned), 0.6, "rule-keyword", cleaned,
                          reason="精确规则未命中，按关键词兜底识别")
        return Intent("unknown", {}, 0.0, "rule", cleaned, reason="未匹配到任何意图")

    @staticmethod
    def _keyword_guess(text: str) -> str:
        """返回命中最多的意图；只在没有任何精确规则命中时才调用。"""
        lowered = (text or "").lower()
        best_name, best_score = "", 0
        for name, words in INTENT_KEYWORDS.items():
            score = sum(1 for word in words if word in lowered)
            if score > best_score:
                best_name, best_score = name, score
        return best_name

    @staticmethod
    def _has(text: str, *words: str) -> bool:
        return any(w in text for w in words)

    def _match_help(self, t: str) -> bool:
        if self._match_bill_supported_formats(t):
            return False
        return self._has(t, "你能做什么", "你能干嘛", "有什么功能", "帮助", "help", "怎么用", "支持哪些")

    def _match_bill_supported_formats(self, t: str) -> bool:
        return self._has(
            t,
            "上传哪些格式", "支持哪些格式", "可以上传什么格式", "可以上传哪些文件",
            "账单格式", "文件格式", "上传格式", "支持什么格式", "能识别哪些文件", "支持什么文件",
            "excel支持什么", "xlsx支持",
        )

    def _match_agent_about(self, t: str) -> bool:
        """关于 Agent 自身的问题（用什么模型、怎么实现的）。"""
        if self._has(t, "什么大模型", "什么模型", "哪个模型", "用的什么大模型", "大模型是什么",
                     "模型是哪个", "底层模型", "什么语言模型", "LLM"):
            return True
        return self._has(t, "你是谁", "你是什么", "你叫什么", "介绍一下你", "自我介绍",
                         "你的架构", "你怎么实现", "怎么实现的", "什么技术栈", "用的什么技术",
                         "你的原理", "你是怎么做")

    def _match_crossscene_daily_brief(self, t: str) -> bool:
        return self._has(t, "简报", "今天有什么要注意", "最近要注意什么", "有什么要提醒我", "今天有什么要")

    def _match_forecast_cashflow(self, t: str) -> bool:
        return self._has(
            t,
            "现金流", "财务数字孪生", "数字孪生", "未来90天", "未来三个月",
            "未来6个月", "资金缺口", "现金够不够", "收入下降", "如果买",
            "如果购买", "财务预测",
        ) or bool(re.search(r"如果.{0,16}(?:买|购买|花|支出)", t))

    def _match_advisor_proactive(self, t: str) -> bool:
        return self._has(
            t,
            "主动财务管家", "个人cfo", "个人 CFO", "财务体检",
            "主动提醒", "帮我盯着账户", "今天财务上有什么",
        )

    def _match_advisor_health_score(self, t: str) -> bool:
        return self._has(t, "财务健康分", "财务健康评分", "财务评分", "健康评分")

    def _match_macro_official(self, t: str) -> bool:
        return self._has(
            t,
            "官方宏观数据", "国债收益率", "央行逆回购", "人民银行公开市场",
            "公开市场操作", "中国CPI", "中国cpi", "宏观利率", "刷新宏观",
        )

    def _match_market_overview(self, t: str) -> bool:
        return self._has(
            t, "A股行情", "a股行情", "大盘怎么样", "大盘行情", "上证指数",
            "深证成指", "创业板指", "科创50", "北证50", "两市成交额",
            "市场行情怎么样",
        )

    def _match_advisor_events(self, t: str) -> bool:
        return self._has(
            t,
            "主动事件", "财务通知", "事件中心", "查看财务提醒",
            "稍后提醒", "忽略事件",
        )

    def _match_risk_playbook(self, t: str) -> bool:
        return self._has(
            t,
            "风险处置", "交易处置", "处置方案", "盗刷怎么办",
            "争议交易", "盗刷后", "风险事件处置",
        )

    def _match_risk_dispute(self, t: str) -> bool:
        return self._has(
            t,
            "发起争议交易", "提交争议交易", "申请交易争议",
            "发起交易争议", "提交交易争议",
        )

    def _match_account_reconcile(self, t: str) -> bool:
        return self._has(t, "账务对账", "账实一致", "账实核对", "核对余额", "核对银行卡")

    def _match_account_reconcile_adjust(self, t: str) -> bool:
        return self._has(
            t,
            "修复账务差异", "调整对账差异", "手工对账修正",
            "修正账实差异", "修复对账差异",
        )

    def _match_wealth_analytics(self, t: str) -> bool:
        if self._has(
            t,
            "最大回撤", "波动率", "组合风险", "组合相关性",
            "产品风险分析", "理财风险分析", "费率拖累",
        ):
            return True
        return bool(re.search(r"(?:产品|理财|组合).{0,8}(?:风险|回撤|波动)", t))

    def _match_wealth_compare(self, t: str) -> bool:
        wants_compare = self._has(
            t,
            "对比", "比较", "有什么区别", "怎么选", "哪个好", "哪个更",
        )
        return wants_compare and self._has(
            t,
            "产品", "理财", "基金", "债券", "存款", "股票", "ETF",
        )

    def _match_transfer_aa_status(self, t: str) -> bool:
        return self._has(t, "aa收款进度", "AA收款进度", "aa状态", "AA状态", "谁还没付款")

    def _match_transfer_split_aa(self, t: str) -> bool:
        # Keep "aa" as a standalone token so random UUID/path fragments do not
        # hijack a bill-image upload.
        if re.search(r"(?<![a-z0-9])aa(?![a-z0-9])", t.lower()):
            return True
        return self._has(t, "分摊", "平摊", "均摊", "各付", "拆分", "每人", "一起付")

    def _match_transfer_list_scheduled(self, t: str) -> bool:
        return self._has(t, "定时转账", "预约任务", "待执行任务", "有哪些定时", "我的计划")

    def _match_transfer_internal(self, t: str) -> bool:
        if not self._has(t, "转", "划转", "调拨"):
            return False
        if not self._has(t, "卡", "尾号", "账户"):
            return False
        separator = re.search(r"[向给]", t)
        if separator and re.search(r"[向给].{0,80}(?:转|划转|调拨)", t):
            left = t[:separator.start()]
            right = t[separator.end():]
            verb = re.search(r"转|划转|调拨", right)
            target_text = right[:verb.start()] if verb else right
            if (
                self._looks_like_card_reference(left)
                and self._looks_like_card_reference(target_text)
            ):
                return True
            return False
        if "到" not in t:
            return False
        _, right = t.split("到", 1)
        target_text = re.split(
            r"[，。,；;\s]|(?:\d+(?:\.\d+)?元)",
            right,
            maxsplit=1,
        )[0]
        if not self._looks_like_card_reference(target_text):
            return False
        return self._has(t, "从", "本人", "自己", "内部") or bool(
            re.search(r"(?:尾号)?\d{4}.{0,12}(?:转|划转).{0,12}(?:尾号)?\d{4}", t)
        )

    def _match_transfer_schedule(self, t: str) -> bool:
        if not self._has(t, "转", "打钱", "汇款", "付", "交"):
            return False
        if self._has(t, "每月", "每个月", "每周", "每星期", "定时", "自动转"):
            return True
        return self._has(t, "明天", "后天", "下个月", "下周一", "月底", "月初") and self._has(t, "转", "汇款")

    def _match_transfer_send(self, t: str) -> bool:
        if self._has(t, "转账", "转钱", "打钱", "汇款", "付给", "发红包", "还钱", "转给"):
            return True
        if re.search(r"(给|向|帮)[\u4e00-\u9fa5]{2,4}(转|打|汇|付|发)", t):
            return True
        return bool(re.search(r"转\s*\d", t))

    def _match_bill_anomaly(self, t: str) -> bool:
        return self._has(t, "异常", "可疑", "盗刷", "不对劲", "重复扣款", "重复扣", "乱扣", "风控",
                         "被扣了", "安全吗", "有没有问题", "账户安全")

    def _match_bill_report(self, t: str) -> bool:
        if self._has(t, "账单报告", "月报", "年报", "年度账单", "月度账单", "年度报告", "消费报告"):
            return True
        return self._has(t, "报告") and self._has(t, "账单", "消费", "支出", "年度", "月")

    def _match_bill_scan_receipt(self, t: str) -> bool:
        """识别账单截图 / 小票照片。"""
        if RE_RECEIPT_PATH.search(t):
            return True
        image_words = self._has(t, "截图", "图片", "照片", "小票", "收据", "账单图", "拍照", "扫描件")
        action = self._has(t, "识别", "扫描", "解析", "上传", "拍", "读", "看", "录")
        if image_words and action:
            return True
        # "帮我识别一下这张账单" 这类没有出现"图片/截图"字样的说法
        return self._has(t, "这张", "这几张", "这份") and self._has(t, "账单", "小票", "收据")

    def _match_bill_search(self, t: str) -> bool:
        asks_spend = re.search(
            r"(花了多少|花多少|消费了多少|一共花|总共花|花了多少钱)",
            t,
        )
        explicit_lookup = re.search(
            r"(查|找|搜|检索|筛选|列出|调出|看看|看一下).{0,20}"
            r"(消费|交易|流水)",
            t,
        )
        if not asks_spend and not explicit_lookup:
            return False
        keyword = self._search_keyword(t)
        # "最近7天花了多少" 没有具体商户，应该走账单统计而不是关键词检索
        if not keyword or re.fullmatch(r"[\d一二三四五六七八九十]+(天|周|个?月|年)", keyword):
            return False
        return True

    def _match_bill_analyze(self, t: str) -> bool:
        return self._has(t, "账单", "消费", "支出", "开销", "花销", "统计", "花了", "用了多少")

    def _match_wealth_risk_assess(self, t: str) -> bool:
        return self._has(t, "风险测评", "风险承受", "风险等级", "测一下风险", "评估风险", "做个测评", "我的风险")

    def _match_wealth_redeem(self, t: str) -> bool:
        return self._has(t, "赎回", "卖出", "取回", "全部卖掉", "把钱拿出来", "提出来")

    def _match_wealth_subscribe(self, t: str) -> bool:
        # "想买点股票/基金" 是在问"买什么"，不是下单；有具体产品名才算申购
        if self._has(t, "股票", "个股", "基金", "理财产品", "理财", "债券",
                     "期权", "期货", "衍生品") and not self._product_name(t):
            return False
        if self._has(t, "申购", "买入", "认购", "购买", "买点", "投点", "帮我买", "帮我投"):
            return True
        return bool(re.search(r"买\s*\d", t))

    def _match_wealth_portfolio(self, t: str) -> bool:
        return self._has(t, "持仓", "我的理财", "收益怎么样", "赚了多少", "亏了多少", "我的基金")

    def _match_wealth_recommend(self, t: str) -> bool:
        # "我想买点股票/基金" 没有具体标的，本质是让 Agent 推荐
        if self._has(t, "买点", "买些", "想买", "买什么") and self._has(
                t, "股票", "基金", "理财", "债券", "期权", "期货", "衍生品"):
            return True
        return self._has(
            t, "推荐", "理财产品", "买什么好", "怎么配置", "资产配置",
            "有哪些产品", "基金推荐", "理财", "换一批", "更多产品",
        )

    def _match_card_report_lost(self, t: str) -> bool:
        return self._has(t, "挂失", "丢了", "不见了", "找不到卡", "补卡", "被偷", "被偷了")

    def _match_card_unlock(self, t: str) -> bool:
        return self._has(t, "解冻", "解锁", "恢复交易", "恢复使用", "恢复正常", "解除冻结")

    def _match_card_freeze(self, t: str) -> bool:
        return self._has(t, "冻结", "锁卡", "停止交易", "禁止交易", "先停卡")

    def _match_card_control(self, t: str) -> bool:
        scope = self._has(t, "境外", "海外", "线上", "网络支付", "免密", "闪付", "云闪付")
        action = self._has(t, "开", "关", "启用", "禁用", "允许", "禁止", "限制")
        return scope and action

    def _match_card_limit_adjust(self, t: str) -> bool:
        if self._has(t, "额度", "限额", "上限") and self._has(
            t, "提", "调", "改", "降", "升", "调整", "设成", "设为"
        ):
            return True
        card_context = self._has(t, "卡", "尾号", "银行")
        return card_context and bool(
            re.search(r"(?:调整到|调到|改成|设为|设成)\s*\d", t)
        )

    def _match_card_apply(self, t: str) -> bool:
        actions = ("申请", "申领", "领取", "办理", "办一张", "办张", "办个",
                   "开一张", "开张", "开个", "开通", "想要一张", "要一张", "来一张")
        return self._has(t, *actions) and self._has(t, "卡")

    def _match_card_repay(self, t: str) -> bool:
        if "信用卡" not in t:
            return False
        if self._has(t, "还款", "还信用卡", "偿还", "归还信用卡"):
            return True
        has_transfer = self._has(t, "转", "划转", "支付")
        targets_credit = bool(
            re.search(r"(?:给|向|到|转入|转入到).{0,30}信用卡", t)
        )
        has_source_card = self._has(
            t,
            "从",
            "用",
            "虚拟卡",
            "借记卡",
            "储蓄卡",
            "尾号",
        )
        return has_transfer and targets_credit and has_source_card

    def _match_card_bind(self, t: str) -> bool:
        return self._has(t, "绑定", "添加", "绑一张", "绑定一张") and self._has(
            t, "银行卡", "储蓄卡", "借记卡", "他行卡")

    def _match_card_list(self, t: str) -> bool:
        return self._has(t, "我的卡", "有哪些卡", "卡片列表", "卡列表", "我有什么卡", "卡种", "卡的情况")

    def _match_card_statement(self, t: str) -> bool:
        return self._has(
            t,
            "信用卡账单", "本期账单", "本期应还", "最低还款",
            "信用卡还款日", "账单日",
        )

    def _match_card_timeline(self, t: str) -> bool:
        if self._has(
            t,
            "申请进度", "卡片进度", "审批时间线", "卡片生命周期",
            "虚拟卡卡号", "虚拟卡安全码", "显示虚拟卡",
        ):
            return True
        return bool(re.search(r"虚拟卡.{0,12}(?:卡号|安全码|CVV)", t))

    def _match_subscription_cancel(self, t: str) -> bool:
        return self._has(
            t, "取消", "停止", "关掉", "关闭", "不想再", "不用再",
        ) and self._has(
            t, "订阅", "会员", "续费", "自动续费", "自动扣", "扣费", "扣款",
        )

    def _match_subscription_resume(self, t: str) -> bool:
        restore_action = self._has(t, "恢复", "重新开通", "续回去", "重新开启")
        open_action = self._has(t, "打开", "开启") and self._has(t, "自动续费", "续费")
        return (restore_action or open_action) and self._has(
            t, "订阅", "会员", "续费", "自动续费",
        )

    def _match_subscription_change_cycle(self, t: str) -> bool:
        return self._has(t, "改", "调整", "变更", "更换") and self._has(
            t, "续费", "周期", "套餐", "月会", "年会",
        )

    def _match_membership_purchase(self, t: str) -> bool:
        return self._has(t, "充", "购买", "开通", "充值") and bool(
            parse_membership_items(t)
        )

    def _match_subscription_remind(self, t: str) -> bool:
        return self._has(t, "续费提醒", "要扣费", "快到期", "哪个要扣", "什么时候扣", "即将扣费", "最近有哪些订阅")

    def _match_subscription_detect(self, t: str) -> bool:
        return self._has(t, "自动续费", "偷偷扣", "识别订阅", "帮我找找订阅", "有哪些订阅扣费", "哪些在扣钱")

    def _match_subscription_list(self, t: str) -> bool:
        return self._has(t, "订阅", "会员", "自动扣费", "月费", "自动续费")

    def _match_crossscene_birthday_plan(self, t: str) -> bool:
        return self._has(t, "生日", "纪念日", "惊喜", "鲜花", "蛋糕", "礼物")

    def _match_crossscene_gift_plan(self, t: str) -> bool:
        if "生日" in t:
            return False
        explicit_gift = self._has(
            t, "伴手礼", "礼物预约", "准备礼物", "纪念日礼物", "送礼", "礼物",
        )
        contextual_gift = (
            self._has(t, "下单", "送", "订", "买", "准备")
            and bool(self._person(t))
            and not self._has(
                t, "转账", "转给", "汇款", "红包", "话费", "账单",
                "理财", "基金", "申购", "赎回", "会员", "订阅",
            )
        )
        return explicit_gift or contextual_gift

    def _match_goal_list(self, t: str) -> bool:
        return self._has(t, "我的目标", "攒钱目标", "心愿", "锁了多少钱", "目标进度", "有哪些目标", "资金目标")

    def _match_goal_create(self, t: str) -> bool:
        return self._has(t, "锁定", "冻结一笔", "存一笔", "攒", "基金计划") and self._has(
            t, "元", "块", "万", "钱", "资金"
        )

    def _match_account_balance(self, t: str) -> bool:
        return self._has(t, "余额", "多少钱", "总资产", "账户有多少", "还有多少", "查钱", "资产")

    def _match_profile_show(self, t: str) -> bool:
        return self._has(t, "我的资料", "我的信息", "我的档案", "客户画像", "我是谁")

    # ------------------------------------------------------------------
    # 槽位抽取
    # ------------------------------------------------------------------
    def _slots(self, intent: str, text: str) -> Dict[str, Any]:
        today = self.store.clock.today() if self.store is not None else None
        slots: Dict[str, Any] = {}

        if intent == "transfer.internal":
            separator = re.search(r"[向给]", text)
            if (
                separator
                and re.search(r"[向给].+?(?:转|划转|调拨)", text)
            ):
                left = text[:separator.start()]
                right = text[separator.end():]
                verb = re.search(r"转|划转|调拨", right)
                source_text = left.strip("从用 ，,")
                target_text = right[:verb.start()] if verb else right
                source = self._card_reference(source_text)
                target = self._card_reference(target_text)
                if source:
                    slots["from_card"] = source
                if target:
                    slots["to_card"] = target
            elif "到" in text:
                left, right = text.split("到", 1)
                match = re.search(r"(?:从)?(.+?)(?:转|划转|调拨)", left)
                if match:
                    slots["from_card"] = match.group(1).strip("从 ，,")
                target = re.split(r"[，。,；;\s]|(?:\d+(?:\.\d+)?元)", right, maxsplit=1)[0].strip()
                target = re.sub(r"(?:卡中|卡里|卡上|里面|中|里|上)$", "卡", target)
                if target:
                    slots["to_card"] = target
            amount = parse_amount(text)
            if amount is not None:
                slots["amount"] = amount
            note = self._note(text)
            if note:
                slots["note"] = note

        elif intent == "macro.official":
            if self._has(text, "刷新", "联网更新", "重新获取"):
                slots["refresh"] = True

        elif intent == "forecast.cashflow":
            month_match = re.search(
                r"(?:未来|接下来)?\s*(\d{1,2})\s*个?月|未来\s*(\d{2,3})\s*天",
                text,
            )
            if month_match:
                if month_match.group(1):
                    slots["months"] = int(month_match.group(1))
                elif month_match.group(2):
                    slots["months"] = max(1, round(int(month_match.group(2)) / 30))
            elif self._has(text, "半年", "6个月"):
                slots["months"] = 6
            elif self._has(text, "一年", "12个月"):
                slots["months"] = 12
            else:
                slots["months"] = 3
            amount = parse_amount(text)
            if amount is not None:
                if self._has(text, "投资", "申购", "定投"):
                    slots["investment_amount"] = amount
                elif self._has(text, "买", "消费", "支出", "花"):
                    slots["purchase_amount"] = amount
            income_match = re.search(
                r"收入(?:下降|减少|上升|增加)\s*(\d+(?:\.\d+)?)\s*%",
                text,
            )
            if income_match:
                value = float(income_match.group(1))
                slots["income_change_pct"] = (
                    -value if self._has(text, "下降", "减少") else value
                )
            extra = re.search(r"每月(?:多花|新增支出|增加支出)\s*(\d+(?:\.\d+)?)", text)
            if extra:
                slots["extra_monthly_expense"] = float(extra.group(1))
            installment = re.search(r"分期(?:每月|月供)\s*(\d+(?:\.\d+)?)", text)
            if installment:
                slots["extra_monthly_expense"] = float(installment.group(1))
            expected_return = re.search(
                r"(?:预期年化|年化收益|收益率)\s*(\d+(?:\.\d+)?)\s*%",
                text,
            )
            if expected_return:
                slots["investment_return_pct"] = float(expected_return.group(1))
            date_match = re.search(
                r"(下个月\d{1,2}[日号]|本月\d{1,2}[日号]|\d{4}-\d{1,2}-\d{1,2})",
                text,
            )
            if date_match:
                parsed = parse_date(date_match.group(1), today)
                if parsed:
                    slots["purchase_date"] = parsed.isoformat()

        elif intent == "risk.playbook":
            match = re.search(r"(?:给我|为|关于)?(.{1,30}?)(?:的)?风险处置", text)
            if match:
                slots["case"] = match.group(1).strip("给我为 ，,。")
            else:
                merchant = self._merchant(text)
                if merchant:
                    slots["case"] = merchant
        elif intent == "risk.dispute":
            match = re.search(r"(TXN\d{6,})", text, re.I)
            if match:
                slots["txn_id"] = match.group(1).upper()
        elif intent == "account.reconcile_adjust":
            card = self._card_reference(text)
            if card:
                slots["card"] = card
            if self._has(text, "以银行卡余额为准", "按银行卡余额", "以卡片余额为准"):
                slots["source_of_truth"] = "card"
            elif self._has(text, "以关联账户余额为准", "按账户余额", "以账户余额为准"):
                slots["source_of_truth"] = "account"

        elif intent == "advisor.events":
            event_match = re.search(r"(EVT-[A-Z]+-\d+)", text, re.I)
            if event_match:
                slots["event_id"] = event_match.group(1).upper()
            if self._has(text, "忽略"):
                slots["action"] = "ignore"
            elif self._has(text, "稍后", "延后"):
                slots["action"] = "snooze"
            elif self._has(text, "处理"):
                slots["action"] = "handle"
            else:
                slots["action"] = "list"

        elif intent == "wealth.analytics":
            product = self._product(text)
            if product:
                slots["product"] = product

        elif intent in {"transfer.send", "transfer.schedule"}:
            payee = self._person(text)
            if payee:
                slots["payee"] = payee
            channel = self._channel(text)
            if channel:
                slots["channel"] = channel
            account = self._account(text)
            if account:
                slots["account"] = account
            amount = parse_amount(text)
            if amount is not None:
                slots["amount"] = amount
            note = self._note(text)
            if note:
                slots["note"] = note
            if intent == "transfer.schedule":
                when = self._when(text)
                if when:
                    slots["when"] = when
                slots["repeat"] = "monthly" if self._has(text, "每月", "每个月", "月月") else (
                    "weekly" if self._has(text, "每周", "每星期", "每个星期") else "once"
                )

        elif intent == "transfer.split_aa":
            amount = parse_amount(text)
            if amount is not None:
                slots["total"] = amount
            channel = self._channel(text)
            if channel:
                slots["channel"] = channel
            people = self._participants(text)
            if people:
                slots["participants"] = people
            note = self._aa_note(text)
            if note:
                slots["note"] = note

        elif intent in {"bill.analyze", "bill.anomaly", "bill.report", "bill.search"}:
            if today is not None:
                slots["period"] = parse_period(text, today)
            if intent == "bill.report":
                slots["mode"] = "annual" if self._has(text, "年") else "monthly"
            if intent == "bill.search":
                keyword = self._search_keyword(text)
                if keyword:
                    slots["keyword"] = keyword
            if intent == "bill.analyze":
                for cat in ["餐饮", "交通", "购物", "娱乐", "医疗", "教育", "住房", "订阅服务", "生活缴费"]:
                    if cat in text:
                        slots["category"] = cat
                        break

        elif intent == "bill.scan_receipt":
            match = RE_RECEIPT_PATH.search(text)
            if match:
                slots["image"] = match.group(1).strip()

        elif intent == "wealth.compare":
            if self.store is not None:
                matched_names = [
                    product.name
                    for product in self.store.products.values()
                    if product.name in text
                ]
                if matched_names:
                    slots["products"] = "、".join(matched_names[:3])
            if self._has(text, "基金", "ETF"):
                slots["asset_class"] = "fund"
            elif self._has(text, "债券", "国债", "可转债", "纯债"):
                slots["asset_class"] = "bond"
            elif self._has(text, "股票", "A股"):
                slots["asset_class"] = "stock"
            elif self._has(text, "存款", "存单"):
                slots["asset_class"] = "deposit"
            else:
                slots["asset_class"] = "mixed"

        elif intent == "wealth.recommend":
            amount = parse_amount(text)
            if amount is not None:
                slots["amount"] = amount
            # 明确区分"股票"和"基金/理财"，否则会把股票请求答成基金推荐
            # 按资产类别分池：股票 / 债券 / 基金 / 衍生品 / 存款
            if self._has(text, "衍生品", "期权", "期货", "雪球", "结构性", "杠杆"):
                slots["asset_class"] = "derivative"
            elif self._has(text, "债券基金", "纯债基金", "固收基金"):
                slots["asset_class"] = "bond_fund"
            elif self._has(text, "基金") and not self._has(text, "ETF"):
                slots["asset_class"] = "fund"
            elif self._has(text, "债券", "国债", "可转债", "信用债", "企业债"):
                slots["asset_class"] = "bond"
            elif self._has(text, "股票", "个股", "股市", "A股", "ETF", "指数"):
                slots["asset_class"] = "stock"
            elif self._has(text, "存款", "存单"):
                slots["asset_class"] = "deposit"
            # 钱的用途决定要不要先提示应急金
            if self._has(text, "生活费", "急用", "房租", "学费", "看病的", "借的钱", "救命"):
                slots["purpose"] = "living"
            elif self._has(text, "闲钱", "不急用", "长期不用", "短期不用"):
                slots["purpose"] = "idle"
            if self._has(text, "保守", "稳健", "低风险", "保本"):
                slots["risk_level"] = 2
            elif self._has(text, "高风险", "激进", "进取"):
                slots["risk_level"] = 5
            if self._has(text, "长期", "三年", "3年", "五年", "5年"):
                slots["horizon"] = "long"
            elif self._has(text, "短期", "三个月", "3个月", "半年", "随时要用"):
                slots["horizon"] = "short"
            else:
                slots["horizon"] = "mid"

        elif intent == "wealth.subscribe":
            amount = parse_amount(text)
            has_ordinal = bool(re.search(r"第?\s*[1-9]\d*\s*[只个款]", text))
            has_explicit_amount = bool(re.search(r"\d+(?:\.\d+)?\s*(?:元|块|万|千)", text))
            if has_ordinal and not has_explicit_amount:
                amount = None
            if amount is not None:
                slots["amount"] = amount
            product = self._product(text)
            if product:
                slots["product"] = product
            else:
                symbol = re.search(r"(?<![A-Za-z0-9])((?:sh|sz|bj)?\d{6})(?![A-Za-z0-9])", text, re.I)
                if symbol:
                    slots["product"] = symbol.group(1).lower()

        elif intent == "wealth.redeem":
            amount = parse_amount(text)
            if amount is not None:
                slots["amount"] = amount
            product = self._product(text)
            if product:
                slots["product"] = product
            else:
                symbol = re.search(r"(?<![A-Za-z0-9])((?:sh|sz|bj)?\d{6})(?![A-Za-z0-9])", text, re.I)
                if symbol:
                    slots["product"] = symbol.group(1).lower()
            if self._has(text, "全部", "所有", "都赎回"):
                slots.pop("amount", None)
                slots.pop("product", None)

        elif intent == "card.repay":
            source_text = ""
            target_text = ""
            if "给" in text and "信用卡" in text.split("给", 1)[1]:
                source_text, right = text.split("给", 1)
                verb = re.search(r"转|划转|还|偿还|支付", right)
                target_text = right[:verb.start()] if verb else right
            elif "到" in text and "信用卡" in text.split("到", 1)[1]:
                source_text, right = text.split("到", 1)
                verb = re.search(r"转|划转|还|偿还|支付", right)
                target_text = right[:verb.start()] if verb else right
            else:
                match = re.search(
                    r"(?:用|从)(.+?)(?:来)?(?:还款|还信用卡|还|偿还|归还信用卡)",
                    text,
                )
                if match:
                    source_text = match.group(1).strip()
                after_repay = re.search(
                    r"(?:还款|还信用卡|还|偿还|归还信用卡)(.+)$",
                    text,
                )
                if after_repay:
                    target_text = after_repay.group(1)
            source = self._card_reference(source_text) if source_text else ""
            target = (
                self._card_reference(target_text)
                if target_text and self._looks_like_card_reference(target_text)
                else ""
            )
            if source:
                slots["from_card"] = source
            if target:
                slots["card"] = target
            if not slots.get("card"):
                credit_card = self._card(text)
                if credit_card:
                    slots["card"] = credit_card
            amount = parse_amount(text)
            if amount is not None:
                slots["amount"] = amount

        elif intent == "card.limit_adjust":
            amount = parse_amount(text)
            if amount is not None:
                slots["new_limit"] = amount
            card = self._card(text)
            if card:
                slots["card"] = card
            if self._has(text, "单笔", "每笔"):
                slots["limit_type"] = "single"
            elif self._has(text, "日限额", "每日", "一天", "单日"):
                slots["limit_type"] = "daily"

        elif intent == "card.control":
            if self._has(text, "境外", "海外"):
                slots["scope"] = "overseas"
            elif self._has(text, "免密", "闪付", "云闪付"):
                slots["scope"] = "contactless"
            else:
                slots["scope"] = "online"
            if self._has(text, "关闭", "关掉", "禁用", "禁止", "停用", "取消"):
                slots["enable"] = False
            elif self._has(text, "开启", "打开", "启用", "允许", "恢复"):
                slots["enable"] = True
            card = self._card(text)
            if card:
                slots["card"] = card

        elif intent == "card.statement":
            card = self._card(text)
            slots["card"] = card or "信用卡"

        elif intent == "card.timeline":
            card = self._card(text)
            if card:
                slots["card"] = card
            slots["reveal"] = self._has(
                text,
                "显示", "查卡号", "查安全码", "查看卡号", "查看安全码", "展示",
            ) or (
                self._has(text, "查", "查看", "显示", "展示")
                and self._has(text, "卡号", "安全码", "CVV", "cvv")
            )

        elif intent in {"card.freeze", "card.unlock", "card.report_lost", "card.list"}:
            card = self._card(text)
            if card:
                slots["card"] = card

        elif intent == "card.apply":
            if self._has(text, "储蓄卡", "借记卡", "储蓄账户", "借记账户"):
                slots["kind"] = "debit"
            elif self._has(text, "虚拟"):
                slots["kind"] = "virtual"
            elif self._has(text, "附属"):
                slots["kind"] = "supplementary"
            elif self._has(text, "信用卡", "贷记卡"):
                slots["kind"] = "credit"
            amount = parse_amount(text)
            if amount is not None:
                slots["limit_request"] = amount
            for keyword, name in [
                ("工商", "工商银行"), ("工行", "工商银行"), ("农业", "农业银行"),
                ("农行", "农业银行"), ("中国银行", "中国银行"), ("中行", "中国银行"),
                ("建设", "建设银行"), ("建行", "建设银行"), ("招商", "招商银行"),
                ("招行", "招商银行"), ("交通", "交通银行"), ("交行", "交通银行"),
            ]:
                if keyword in text:
                    slots["bank"] = name
                    break

        elif intent == "card.bind":
            digits = re.findall(r"\d{16,19}", text)
            if not digits:
                compact = re.sub(r"[\s-]", "", text)
                digits = re.findall(r"\d{16,19}", compact)
            if digits:
                slots["card_no"] = digits[0]
            for keyword, name in [
                ("工商", "工商银行"), ("工行", "工商银行"),
                ("农业", "农业银行"), ("农行", "农业银行"),
                ("中国银行", "中国银行"), ("中行", "中国银行"),
                ("建设", "建设银行"), ("建行", "建设银行"),
                ("招商", "招商银行"), ("招行", "招商银行"),
                ("交通", "交通银行"), ("交行", "交通银行"),
            ]:
                if keyword in text:
                    slots["bank"] = name
                    break
            pwd = re.search(r"密码[：:是为]?\s*(\d{6})", text)
            if pwd:
                slots["password"] = pwd.group(1)
            remainder = text
            remainder = re.sub(r"(?<!\d)(?:\d[\s-]*){16,19}(?!\d)", " ", remainder)
            phone = RE_PHONE.search(remainder)
            if phone:
                slots["phone"] = phone.group(0)

        elif intent in {"subscription.cancel", "subscription.resume"}:
            merchant = self._merchant(text)
            if merchant:
                slots["merchant"] = merchant

        elif intent == "subscription.change_cycle":
            merchant = self._merchant(text)
            if merchant:
                slots["merchant"] = merchant
            term_match = TERM_RE.search(text)
            if term_match:
                term = int(term_match.group(1))
                slots["term_months"] = term * 12 if term_match.group(2) == "年" else term
            renewal_match = RENEWAL_RE.search(text)
            if renewal_match:
                slots["renewal_price"] = float(renewal_match.group(1))
            else:
                prices = PRICE_RE.findall(text)
                if prices:
                    slots["renewal_price"] = float(prices[-1])
            slots["auto_renew"] = "不自动续费" not in text

        elif intent == "membership.purchase":
            slots["items"] = [item.as_dict() for item in parse_membership_items(text)]

        elif intent == "subscription.remind":
            slots["days"] = 7

        elif intent == "crossscene.birthday_plan":
            person = ""
            for word in ["老婆", "爱人", "妻子", "女朋友", "未婚妻", "妈妈", "母亲", "老公", "爸爸"]:
                if word in text:
                    person = word
                    break
            if not person:
                # "帮我给王强准备生日惊喜" -> 王强
                match = re.search(r"(?:给|为)\s*([\u4e00-\u9fa5]{2,4}?)\s*(?:准备|安排|过)?生日", text)
                if match:
                    person = match.group(1)
            if not person:
                person = self._person(text) or ""
            if person:
                slots["person"] = person
            amount = parse_amount(text)
            if amount is not None:
                slots["budget"] = amount

        elif intent == "crossscene.gift_plan":
            person = self._person(text)
            if person:
                slots["person"] = person
            item = self._gift_item(text)
            if item:
                slots["item"] = item
            when = self._when(text)
            if when:
                slots["date"] = when
            amount = parse_amount(text)
            if amount is not None:
                slots["budget"] = amount

        elif intent == "goal.create":
            amount = parse_amount(text)
            if amount is not None:
                slots["target_amount"] = amount
            if self._has(text, "旅行", "旅游"):
                slots["name"] = "旅行基金"
            elif self._has(text, "应急"):
                slots["name"] = "应急金"
            elif self._has(text, "生日", "惊喜"):
                slots["name"] = "生日惊喜金"
            else:
                slots["name"] = "资金目标"

        return {k: v for k, v in slots.items() if v not in (None, "")}

    def _person(self, text: str) -> Optional[str]:
        phone = RE_PHONE.search(text)
        if phone:
            return phone.group(0)
        for word in RELATION_WORDS:
            if word in text:
                return word
        match = RE_NAME_AFTER_GIVE.search(text)
        if match:
            name = self._clean_name(match.group(1))
            return name if name not in GENERIC_PERSON_VALUES else None
        match = RE_NAME_BEFORE_VERB.search(text)
        if match:
            name = self._clean_name(match.group(1))
            return name if name not in GENERIC_PERSON_VALUES else None
        return None

    @staticmethod
    def _gift_item(text: str) -> str:
        generic = {"礼物", "毕业礼物", "伴手礼", "一份礼物", "一个礼物"}
        match = re.search(
            r"(?:改成|换成|改为|下单|送|订|买|准备)"
            r"\s*(?:一个|一份|一束|一件|个|份|束)?\s*"
            r"([^，,。；;！!？?]{1,30})",
            text,
        )
        if not match:
            return ""
        item = match.group(1).strip(" 的")
        item = re.split(
            r"\s*(?:预算|价格|报价|送达|明天|今天|后天|大后天|下周|本周|\d{1,2}月)",
            item,
            maxsplit=1,
        )[0].strip()
        if "花" in item:
            return "鲜花"
        if "蛋糕" in item:
            return "蛋糕"
        return "" if item in generic else item

    def _channel(self, text: str) -> str:
        """识别收款渠道：手机号（数字人民币）/ 微信 / 支付宝 / 银行卡。"""
        if self._has(text, "微信"):
            return "wechat"
        if self._has(text, "支付宝"):
            return "alipay"
        if self._has(text, "银行卡", "转到卡", "转账到卡"):
            return "bank"
        if self._has(text, "数字人民币", "手机号", "手机", "钱包"):
            return "phone"
        if RE_PHONE.search(text):
            return "phone"
        return ""

    def _account(self, text: str) -> str:
        match = re.search(
            r"从([^，,。；;\s]{1,16}?(?:卡|账户|活期|零钱|钱包))",
            text,
        )
        if match is None:
            match = re.search(
                r"用([^，,。；;\s]{1,16}?(?:卡|账户|活期|零钱|钱包))",
                text,
            )
        if match is None:
            return ""
        value = match.group(1)
        return "" if value in {"银行卡", "卡"} else value

    @staticmethod
    def _clean_name(name: str) -> str:
        return re.sub(r"^(一位|一个|个|了)", "", (name or "").strip())

    def _participants(self, text: str) -> List[str]:
        phones = RE_PHONE.findall(text)
        if phones:
            return ["我"] + phones
        segment = ""
        match = re.search(r"([\u4e00-\u9fa5、,，和\s]{3,40}?)(?:AA|aa|平摊|分摊|均摊|各付|拆分)", text)
        if match:
            segment = match.group(1)
        else:
            match = re.search(r"(?:我|本人)([\u4e00-\u9fa5、,，和\s]{2,40})", text)
            if match:
                segment = "我" + match.group(1)
        if not segment:
            return []
        names: List[str] = []
        for part in re.split(r"[、,，和\s]+", segment):
            part = re.sub(r"^(有|和|跟|还有|及)", "", part.strip())
            if re.fullmatch(r"[\u4e00-\u9fa5]{1,5}", part) and part not in {
                "花了", "一共", "总共", "我们", "个人", "大家", "昨晚", "今天"
            }:
                names.append(part)
        if "我" in text and "我" not in names:
            names.insert(0, "我")
        return names

    def _aa_note(self, text: str) -> str:
        for word in ["聚餐", "团建", "旅游", "打车", "晚饭", "午饭", "火锅", "看电影", "KTV", "住宿", "烧烤"]:
            if word in text:
                return word
        return ""

    def _when(self, text: str) -> str:
        for pattern in RE_WHEN_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(1)
        match = re.search(r"每月\s*(\d{1,2})[日号]", text)
        if match:
            return match.group(0)
        if "每月" in text or "每个月" in text:
            return "每月"
        return ""

    def _note(self, text: str) -> str:
        match = RE_NOTE.search(text)
        if match:
            return match.group(1)
        for word in ["房租", "生活费", "还款", "还钱", "货款", "工资", "尾款", "押金"]:
            if word in text:
                return word
        return ""

    def _search_keyword(self, text: str) -> str:
        """Extract the merchant/category from a natural-language lookup.

        Capability shortcuts send their label as the prompt, so words such as
        "消费记录检索" must not be treated as a merchant name.
        """
        cleaned = (text or "").strip()
        cleaned = re.sub(r"^(?:请|麻烦|帮我|劳驾|我想|我要|想|要)+", "", cleaned)
        cleaned = re.sub(
            r"^(?:查一下|查查|查询|查找|查|找一下|找找|找|搜一下|搜索|搜|"
            r"检索|筛选|筛|看看|看一下|列出|调出)+",
            "",
            cleaned,
        )
        cleaned = re.sub(
            r"(花了多少钱|花了多少|消费了多少|一共花了|总共花了|花多少|"
            r"多少钱|多少|花了|块钱)",
            "",
            cleaned,
        )
        cleaned = re.sub(
            r"(这个月|上个月|本月|今年|去年|最近|本周|这周|近\d+天|"
            r"最近\d+天|我|一共|总共)",
            "",
            cleaned,
        )
        cleaned = re.sub(r"^[在于是关于有关\s]+", "", cleaned)
        cleaned = re.sub(
            r"(?:相关)?(?:的)?(?:消费记录|消费|交易记录|交易|流水记录|流水|支出)$",
            "",
            cleaned,
        )
        cleaned = re.sub(r"^[的\s]+|[的\s]+$", "", cleaned)
        cleaned = re.sub(r"[?？。，,、：:；;\"'“”‘’\s]", "", cleaned)
        generic_terms = {
            "消费", "消费记录", "消费记录检索", "消费检索", "消费查询",
            "查消费", "交易记录", "交易检索", "流水", "流水记录",
            "账单消费", "查交易", "查流水", "检索消费", "检索交易",
        }
        if cleaned in generic_terms:
            return ""
        return cleaned[:20]

    def _product(self, text: str) -> str:
        if self.store is None:
            return ""
        for product in self.store.products.values():
            if product.name in text:
                return product.name
        for product in self.store.products.values():
            if product.category in text:
                return product.name
        return ""

    def _product_name(self, text: str) -> str:
        """只按产品名匹配（不能用类别，"股票"这个词会误命中所有股票产品）。"""
        if self.store is None:
            return ""
        for product in self.store.products.values():
            if product.name in text:
                return product.name
        return ""

    def _merchant(self, text: str) -> str:
        if self.store is None or not self.user_id:
            return ""
        for sub in self.store.subscriptions_of(self.user_id):
            if sub.merchant in text or sub.merchant[:2] in text:
                return sub.merchant
        keywords = {
            "腾讯视频": "腾讯视频VIP", "爱奇艺": "爱奇艺会员", "QQ音乐": "QQ音乐绿钻",
            "iCloud": "iCloud+ 云存储", "icloud": "iCloud+ 云存储",
            "网盘": "百度网盘超级会员", "外卖会员": "美团外卖会员",
            "Keep": "Keep会员", "keep": "Keep会员", "健身": "Keep会员",
        }
        for key, value in keywords.items():
            if key in text:
                return value
        return ""

    def _card(self, text: str) -> str:
        tail = re.search(r"(?:尾号|后四位|末四位)\s*(\d{4})", text)
        if tail:
            return tail.group(1)
        for word in ["信用卡", "借记卡", "储蓄卡", "虚拟卡", "附属卡"]:
            if word in text:
                return word
        digits = re.findall(r"(?<![\d])\d{4}(?![\d])", text)
        return digits[0] if digits else ""

    def _card_reference(self, text: str) -> str:
        """Return the most precise card reference available in one clause."""
        tail = re.search(r"(?:尾号|后四位|末四位)\s*(\d{4})", text)
        if tail:
            return tail.group(1)
        if self.store is not None and self.user_id:
            for card in self.store.cards_of(self.user_id):
                if card.note and card.note in text:
                    return card.note
        for alias in (
            "工资卡", "日常消费卡", "主刷信用卡", "定期存单", "定期卡",
            "网购专用虚拟卡", "信用卡", "借记卡", "储蓄卡", "虚拟卡",
        ):
            if alias in text:
                return alias
        cleaned = re.sub(r"[，。,；;\s]", "", text)
        return cleaned[:30]

    def _looks_like_card_reference(self, text: str) -> bool:
        if re.search(r"(?:尾号|后四位|末四位)\s*\d{4}", text):
            return True
        if self.store is not None and self.user_id:
            if any(card.note and card.note in text for card in self.store.cards_of(self.user_id)):
                return True
            bare_tail = re.fullmatch(r"\s*(\d{4})\s*", text)
            if bare_tail and any(
                card.tail == bare_tail.group(1)
                for card in self.store.cards_of(self.user_id)
            ):
                return True
        return self._has(
            text,
            "信用卡",
            "借记卡",
            "储蓄卡",
            "虚拟卡",
            "附属卡",
            "工资卡",
            "日常消费卡",
            "主刷信用卡",
            "定期存单",
            "定期卡",
        )


class IntentParser:
    """统一入口：大模型优先，规则兜底，两者结果合并。"""

    LLM_FINANCIAL_SLOTS = {
        "payee", "amount", "total", "budget", "target_amount", "new_limit",
        "limit_request", "account", "from_card", "to_card", "payee_account",
        "card_no", "password", "phone", "payment_password", "product",
        "participants", "when", "date", "deadline", "bank",
        "monthly_income", "existing_debt",
    }

    def __init__(self, store=None, user_id: str = "", llm: Optional[LLMClient] = None, mode: str = "auto") -> None:
        self.store = store
        self.user_id = user_id
        self.llm = llm
        self.mode = (mode or "auto").lower()
        self.rule = RuleIntentParser(store, user_id)
        self.last_source = "rule"

    def parse(self, text: str, history: Optional[List[Dict[str, str]]] = None) -> Intent:
        rule_intent = self.rule.parse(text)
        if self.mode == "off" or self.llm is None or not self.llm.available:
            self.last_source = "rule"
            return rule_intent

        payload = self._ask_llm(text, history or [])
        if not payload:
            self.last_source = "rule(大模型回退)"
            return rule_intent

        name = str(payload.get("intent", "")).strip()
        if name not in INTENTS:
            self.last_source = "rule(大模型返回未知意图)"
            return rule_intent
        if (
            not rule_intent.is_unknown()
            and rule_intent.confidence >= 0.75
            and name != rule_intent.name
        ):
            self.last_source = "rule(大模型意图与确定性规则冲突)"
            return rule_intent

        slots: Dict[str, Any] = {}
        raw_slots = payload.get("slots") or {}
        if isinstance(raw_slots, dict):
            spec = INTENTS[name]
            slot_specs = {item.name: item for item in spec.slots}
            for key, value in raw_slots.items():
                if key not in slot_specs or value in (None, "", []):
                    continue
                if (
                    key in self.LLM_FINANCIAL_SLOTS
                    and key not in rule_intent.slots
                ):
                    continue
                item = slot_specs[key]
                if item.enum and value not in item.enum:
                    continue
                if item.type == "number":
                    try:
                        value = float(value)
                    except (TypeError, ValueError):
                        continue
                elif item.type == "integer":
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        continue
                elif item.type == "boolean":
                    if isinstance(value, bool):
                        pass
                    elif str(value).lower() in {"true", "1", "yes"}:
                        value = True
                    elif str(value).lower() in {"false", "0", "no"}:
                        value = False
                    else:
                        continue
                elif item.type == "array" and not isinstance(value, list):
                    continue
                elif item.type == "string" and not isinstance(value, str):
                    value = str(value)
                slots[key] = value
        # 规则通道补全大模型漏掉的硬信息（金额、日期用正则更稳）
        for key, value in rule_intent.slots.items():
            slots.setdefault(key, value)

        if self.store is not None:
            today = self.store.clock.today()
            for key in ("when", "deadline", "date"):
                if isinstance(slots.get(key), str) and slots[key]:
                    parsed = parse_date(slots[key], today)
                    if parsed:
                        slots[key] = parsed.isoformat()

        confidence = float(payload.get("confidence", 0.9) or 0.9)
        self.last_source = f"llm({self.llm.model})"
        return Intent(name, slots, confidence, "llm", text, reason=str(payload.get("reason", "")))

    def _ask_llm(self, text: str, history: List[Dict[str, str]]) -> Optional[Dict[str, Any]]:
        assert self.llm is not None
        context_lines: List[str] = []
        if self.store is not None and self.user_id:
            user = self.store.get_user(self.user_id)
            if user:
                context_lines.append(f"当前用户：{user.name}，风险偏好 {user.risk_profile}")
                prefs = user.preferences or {}
                if prefs.get("spouse"):
                    context_lines.append(f"家人：{prefs['spouse']}（生日 {prefs.get('spouse_birthday', '未知')}）")
                payees = "、".join(p.name for p in self.store.payees_of(self.user_id)[:6])
                if payees:
                    context_lines.append(f"常用收款人：{payees}")
            context_lines.append(f"今天是 {self.store.clock.date_str()}")

        system_prompt = (
            "你是银行智能助手的意图识别模块。请把用户的一句话解析成 JSON，只输出 JSON，不要任何解释。\n"
            '格式：{"intent": "<意图名>", "slots": {...}, "confidence": 0.0-1.0, "reason": "简短理由"}\n'
            "没有提到的槽位不要出现在 slots 里；金额统一用数字（单位元）；"
            "时间槽位保留用户的原始表达（如 \"明天\"、\"下个月5号\"）。\n\n"
            "可选意图清单（意图名 | 含义 | 槽位（*为必填） | 示例）：\n" + catalog_text() + "\n\n"
            "槽位命名对照表：\n" + slot_dictionary_text() + "\n\n"
            '如果用户的话与任何意图都不匹配，返回 intent 为 "unknown"。'
        )
        messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
        if context_lines:
            messages.append({"role": "system", "content": "上下文：\n" + "\n".join(context_lines)})
        messages.extend(history[-6:])
        messages.append({"role": "user", "content": text})
        return self.llm.chat_json(messages)

