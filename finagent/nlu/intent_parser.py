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
from finagent.nlu.capability_catalog import resolve_capability_intent
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

RELATION_WORDS = ["老婆", "老公", "爱人", "妻子", "丈夫", "妈妈", "爸爸", "母亲", "父亲",
                  "房东", "老师", "老板", "女朋友", "男朋友", "未婚妻", "未婚夫"]

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
    "transfer.schedule": ["定时转", "自动转", "每月转", "每周转", "预约转"],
    "transfer.split_aa": ["aa", "分摊", "平摊", "均摊", "各付"],
    "transfer.list_scheduled": ["定时转账", "待执行", "预约任务"],
    "bill.analyze": ["账单", "消费", "支出", "开销", "花销"],
    "bill.anomaly": ["异常", "可疑", "盗刷", "重复扣", "乱扣"],
    "bill.report": ["账单报告", "月报", "年报", "年度账单"],
    "bill.search": ["花了多少", "消费了多少", "花在"],
    "bill.scan_receipt": ["截图", "小票", "收据", "识别账"],
    "bill.supported_formats": ["上传哪些格式", "支持哪些格式", "账单格式", "文件格式", "能识别哪些文件"],
    "wealth.risk_assess": ["风险测评", "风险承受", "测评"],
    "wealth.recommend": ["推荐", "理财产品", "配置", "买什么"],
    "wealth.subscribe": ["申购", "买入", "认购"],
    "wealth.redeem": ["赎回", "卖出"],
    "wealth.portfolio": ["持仓", "赚了多少", "收益"],
    "card.list": ["有哪些卡", "卡列表", "我的卡"],
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
        "transfer.split_aa",
        "transfer.list_scheduled",
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
        cleaned = normalize_synonyms((text or "").strip())
        if not cleaned:
            return Intent("unknown", {}, 0.0, "rule", cleaned)
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

    def _match_transfer_split_aa(self, t: str) -> bool:
        # Keep "aa" as a standalone token so random UUID/path fragments do not
        # hijack a bill-image upload.
        if re.search(r"(?<![a-z0-9])aa(?![a-z0-9])", t.lower()):
            return True
        return self._has(t, "分摊", "平摊", "均摊", "各付", "拆分", "每人", "一起付")

    def _match_transfer_list_scheduled(self, t: str) -> bool:
        return self._has(t, "定时转账", "预约任务", "待执行任务", "有哪些定时", "我的计划")

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
        if not re.search(r"(花了多少|花多少|消费了多少|一共花|总共花|花了多少钱)", t):
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
        if self._has(t, "申购", "买入", "认购", "买点", "投点", "帮我买", "帮我投"):
            return True
        return bool(re.search(r"买\s*\d", t))

    def _match_wealth_portfolio(self, t: str) -> bool:
        return self._has(t, "持仓", "我的理财", "收益怎么样", "赚了多少", "亏了多少", "我的基金")

    def _match_wealth_recommend(self, t: str) -> bool:
        # "我想买点股票/基金" 没有具体标的，本质是让 Agent 推荐
        if self._has(t, "买点", "买些", "想买", "买什么") and self._has(
                t, "股票", "基金", "理财", "债券", "期权", "期货", "衍生品"):
            return True
        return self._has(t, "推荐", "理财产品", "买什么好", "怎么配置", "资产配置", "有哪些产品", "基金推荐", "理财")

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
        return self._has(t, "额度", "限额", "上限") and self._has(
            t, "提", "调", "改", "降", "升", "调整", "设成", "设为"
        )

    def _match_card_apply(self, t: str) -> bool:
        actions = ("申请", "申领", "领取", "办理", "办一张", "办张", "办个",
                   "开一张", "开张", "开个", "开通", "想要一张", "要一张", "来一张")
        return self._has(t, *actions) and self._has(t, "卡")

    def _match_card_bind(self, t: str) -> bool:
        return self._has(t, "绑定", "添加", "绑一张", "绑定一张") and self._has(
            t, "银行卡", "储蓄卡", "借记卡", "他行卡")

    def _match_card_list(self, t: str) -> bool:
        return self._has(t, "我的卡", "有哪些卡", "卡片列表", "卡列表", "我有什么卡", "卡种", "卡的情况")

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
        return self._has(
            t, "伴手礼", "礼物预约", "准备礼物", "纪念日礼物", "送礼", "礼物",
        )

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

        if intent in {"transfer.send", "transfer.schedule"}:
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

        elif intent == "wealth.recommend":
            amount = parse_amount(text)
            if amount is not None:
                slots["amount"] = amount
            # 明确区分"股票"和"基金/理财"，否则会把股票请求答成基金推荐
            # 按资产类别分池：股票 / 债券 / 基金 / 衍生品 / 存款
            if self._has(text, "衍生品", "期权", "期货", "雪球", "结构性", "杠杆"):
                slots["asset_class"] = "derivative"
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
            if amount is not None:
                slots["amount"] = amount
            product = self._product(text)
            if product:
                slots["product"] = product

        elif intent == "wealth.redeem":
            amount = parse_amount(text)
            if amount is not None:
                slots["amount"] = amount
            product = self._product(text)
            if product:
                slots["product"] = product
            if self._has(text, "全部", "所有", "都赎回"):
                slots.pop("amount", None)
                slots.pop("product", None)

        elif intent == "card.limit_adjust":
            amount = parse_amount(text)
            if amount is not None:
                slots["new_limit"] = amount
            card = self._card(text)
            if card:
                slots["card"] = card

        elif intent == "card.control":
            if self._has(text, "境外", "海外"):
                slots["scope"] = "overseas"
            elif self._has(text, "免密", "闪付", "云闪付"):
                slots["scope"] = "contactless"
            else:
                slots["scope"] = "online"
            slots["enable"] = not self._has(text, "关闭", "关掉", "禁用", "禁止", "停用", "取消")
            card = self._card(text)
            if card:
                slots["card"] = card

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
            if self._has(text, "鲜花", "花束", "一束花", "伴手礼"):
                slots["item"] = "鲜花"
            elif self._has(text, "蛋糕"):
                slots["item"] = "蛋糕"
            else:
                slots["item"] = "鲜花"
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
            return self._clean_name(match.group(1))
        match = RE_NAME_BEFORE_VERB.search(text)
        if match:
            return self._clean_name(match.group(1))
        return None

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
                r"用(活期账户|活期|储蓄卡|信用卡|借记卡|零钱|钱包)",
                text,
            )
        return match.group(1) if match else ""

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
        cleaned = re.sub(
            r"(花了多少钱|花了多少|消费了多少|一共花了|总共花了|花多少|多少钱|多少|花了|块钱|"
            r"这个月|上个月|本月|今年|去年|最近|查一下|查询|看看|帮我|一共|总共|"
            r"我|这|在|元)",
            "",
            text,
        )
        return re.sub(r"[?？。，,、\s]", "", cleaned)[:10]

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
        for word in ["信用卡", "借记卡", "储蓄卡", "虚拟卡", "附属卡"]:
            if word in text:
                return word
        tail = re.search(r"(?:尾号|后四位|末四位)\s*(\d{4})", text)
        if tail:
            return tail.group(1)
        digits = re.findall(r"(?<![\d])\d{4}(?![\d])", text)
        return digits[0] if digits else ""


class IntentParser:
    """统一入口：大模型优先，规则兜底，两者结果合并。"""

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

        slots: Dict[str, Any] = {}
        raw_slots = payload.get("slots") or {}
        if isinstance(raw_slots, dict):
            slots.update({k: v for k, v in raw_slots.items() if v not in (None, "", [])})
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

