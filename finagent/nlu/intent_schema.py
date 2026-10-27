"""意图与槽位的结构定义。

这份 schema 同时承担三个角色：
1. 规则解析器的匹配目标；
2. 大模型提示词里的"意图清单"（由 `catalog_text()` 自动生成）；
3. 对话管理器判断"少了哪个槽位"的依据。

新增业务场景时，只需要在这里加一条 IntentSpec，并在 Planner 里加一条映射。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SlotSpec:
    """一个槽位（参数）的声明。"""

    name: str
    description: str
    required: bool = False
    type: str = "string"
    enum: Optional[List[str]] = None


@dataclass
class IntentSpec:
    """一个意图的声明。"""

    name: str
    category: str
    description: str
    slots: List[SlotSpec] = field(default_factory=list)
    examples: List[str] = field(default_factory=list)

    def required_slots(self) -> List[str]:
        return [s.name for s in self.slots if s.required]


@dataclass
class Intent:
    """解析结果。"""

    name: str
    slots: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    source: str = "rule"          # rule / llm / merged / system
    raw_text: str = ""
    reason: str = ""

    def is_unknown(self) -> bool:
        return self.name in {"unknown", ""}


L = SlotSpec  # 简写，让下面的声明更紧凑

INTENTS: Dict[str, IntentSpec] = {
    # ---------------- 智能转账 ----------------
    "transfer.send": IntentSpec(
        "transfer.send", "智能转账", "向某人转账（按姓名/手机号/备注）",
        [
            L("payee", "收款人姓名、昵称备注或手机号", True),
            L("amount", "金额（元）", True, "number"),
            L("note", "转账备注"),
            L("account", "付款账户，如 活期/信用卡/零钱"),
        ],
        ["给老婆转2000块", "转500给13700003333", "给房东转3800块交房租"],
    ),
    "transfer.schedule": IntentSpec(
        "transfer.schedule", "智能转账", "设置定时/周期性转账",
        [
            L("payee", "收款人", True),
            L("amount", "金额（元）", True, "number"),
            L("when", "执行时间", True),
            L("repeat", "重复方式", False, "string", ["once", "monthly", "weekly"]),
            L("note", "备注"),
        ],
        ["下个月5号自动给房东转3800", "每周一给妈妈转500"],
    ),
    "transfer.split_aa": IntentSpec(
        "transfer.split_aa", "智能转账", "把共同消费按人数平摊并向他人发起 AA 收款",
        [
            L("total", "消费总金额（元）", True, "number"),
            L("participants", "参与人列表（含自己）", True, "array"),
            L("note", "消费说明"),
        ],
        ["昨晚聚餐960，我、王强、李思琪AA", "团建花了3000块，5个人平摊"],
    ),
    "transfer.list_scheduled": IntentSpec(
        "transfer.list_scheduled", "智能转账", "查看已设置的定时转账与预约任务",
        [], ["我有哪些定时转账", "看看我的预约任务"],
    ),
    # ---------------- 账单分析 ----------------
    "bill.supported_formats": IntentSpec(
        "bill.supported_formats", "账单分析",
        "查看账单图片、文本和 Excel 支持的上传格式及分析能力",
        [], ["可以上传哪些格式的账单", "Excel 支持什么格式", "能识别哪些文件"],
    ),
    "bill.analyze": IntentSpec(
        "bill.analyze", "账单分析", "消费分类统计与收支概览",
        [L("period", "统计期间，如 本月/上月/今年/最近7天"), L("category", "只看某个类别")],
        ["分析我这个月的账单", "上个月花了多少", "我这个月餐饮花了多少"],
    ),
    "bill.anomaly": IntentSpec(
        "bill.anomaly", "账单分析", "识别异常/可疑交易",
        [L("period", "检查期间")],
        ["帮我看看有没有异常交易", "最近是不是被盗刷了", "有没有重复扣款"],
    ),
    "bill.report": IntentSpec(
        "bill.report", "账单分析", "生成月度或年度账单报告",
        [L("period", "报告期间"), L("mode", "报告类型", False, "string", ["monthly", "annual"])],
        ["生成我的年度账单报告", "出一份本月账单", "今年的账单报告"],
    ),
    "bill.search": IntentSpec(
        "bill.search", "账单分析", "按商户或关键词检索消费",
        [L("keyword", "商户名或关键词", True), L("period", "检索期间")],
        ["我今年在星巴克花了多少钱", "查一下咖啡消费"],
    ),
    "bill.scan_receipt": IntentSpec(
        "bill.scan_receipt", "账单分析",
        "识别账单/小票截图（图片或账单文件），解析出交易明细",
        [L("image", "账单图片或账单文件的路径", True)],
        ["识别这张账单截图 samples/bill_sample.png", "帮我看看这张小票"],
    ),
    # ---------------- 理财操作 ----------------
    "wealth.risk_assess": IntentSpec(
        "wealth.risk_assess", "理财操作", "评估风险承受能力并给出配置建议",
        [], ["帮我做个风险测评", "我能承受多大风险"],
    ),
    "wealth.recommend": IntentSpec(
        "wealth.recommend", "理财操作", "推荐理财产品并给出组合配置",
        [
            L("risk_level", "可接受的最高风险等级 1-5", False, "integer"),
            L("amount", "计划投入金额（元）", False, "number"),
            L("horizon", "投资期限", False, "string", ["short", "mid", "long"]),
            L("asset_class", "资产类别：股票/基金/存款", False, "string", ["stock", "fund", "deposit"]),
            L("purpose", "这笔钱的性质：生活费/闲钱", False, "string", ["living", "idle"]),
        ],
        ["推荐几个理财产品", "我有5万块，稳健一点怎么配置",
         "我有2万生活费，想买股票，给我低风险的股票推荐"],
    ),
    "wealth.subscribe": IntentSpec(
        "wealth.subscribe", "理财操作", "申购理财产品",
        [
            L("product", "产品名称或代码", True),
            L("amount", "申购金额（元）", True, "number"),
            L("account", "扣款账户"),
        ],
        ["买1万块稳利债券A", "帮我申购5000元微众活期+"],
    ),
    "wealth.redeem": IntentSpec(
        "wealth.redeem", "理财操作", "赎回理财产品",
        [L("product", "产品名称或代码"), L("amount", "赎回金额（元）", False, "number")],
        ["赎回稳利债券A", "全部赎回", "赎回1000块"],
    ),
    "wealth.portfolio": IntentSpec(
        "wealth.portfolio", "理财操作", "查看理财持仓与收益",
        [], ["我的理财持仓", "我赚了多少钱"],
    ),
    # ---------------- 卡片管理 ----------------
    "card.list": IntentSpec(
        "card.list", "卡片管理", "查看名下卡片与交易开关",
        [], ["我有哪些卡", "我的信用卡额度多少"],
    ),
    "card.apply": IntentSpec(
        "card.apply", "卡片管理", "申请新卡",
        [
            L("kind", "卡类型", False, "string", ["credit", "virtual", "supplementary"]),
            L("limit_request", "期望额度（元）", False, "number"),
            L("note", "备注"),
        ],
        ["我想申请一张信用卡", "办一张虚拟卡"],
    ),
    "card.bind": IntentSpec(
        "card.bind", "卡片管理",
        "绑定他行储蓄卡（工商/农业/中国/建设/招商/交通），需卡号与取款密码",
        [
            L("card_no", "16-19 位银行卡号", True),
            L("password", "取款密码", True),
            L("bank", "发卡行"),
            L("phone", "银行预留手机号"),
        ],
        ["绑定一张工商银行的储蓄卡", "添加我的建设银行卡"],
    ),
    "card.limit_adjust": IntentSpec(
        "card.limit_adjust", "卡片管理", "调整额度或交易限额",
        [L("card", "卡片，如 信用卡/尾号8877"), L("new_limit", "新额度（元）", True, "number")],
        ["把信用卡额度提到3万", "把虚拟卡单笔限额降到500"],
    ),
    "card.control": IntentSpec(
        "card.control", "卡片管理", "开关线上/境外/免密交易",
        [
            L("card", "卡片"),
            L("scope", "开关范围", True, "string", ["online", "overseas", "contactless"]),
            L("enable", "true=开启 false=关闭", True, "boolean"),
        ],
        ["关闭信用卡的境外交易", "把虚拟卡线上交易打开"],
    ),
    "card.freeze": IntentSpec(
        "card.freeze", "卡片管理", "冻结卡片，暂停全部交易",
        [L("card", "卡片"), L("reason", "冻结原因")],
        ["把信用卡冻结", "我的卡好像被盗刷了，先冻结"],
    ),
    "card.unlock": IntentSpec(
        "card.unlock", "卡片管理", "解冻卡片",
        [L("card", "卡片")],
        ["解冻信用卡", "把卡恢复正常"],
    ),
    "card.report_lost": IntentSpec(
        "card.report_lost", "卡片管理", "挂失卡片并补卡",
        [L("card", "卡片")],
        ["我的卡丢了，帮我挂失", "挂失信用卡"],
    ),
    # ---------------- 订阅管理 ----------------
    "subscription.detect": IntentSpec(
        "subscription.detect", "订阅管理", "从流水里自动识别订阅扣费",
        [], ["帮我看看有哪些自动续费", "哪些服务在偷偷扣钱"],
    ),
    "subscription.list": IntentSpec(
        "subscription.list", "订阅管理", "列出订阅服务与月度合计",
        [], ["我的订阅都有什么", "每个月自动扣多少钱"],
    ),
    "subscription.cancel": IntentSpec(
        "subscription.cancel", "订阅管理", "取消订阅的自动续费",
        [L("merchant", "订阅商户名称", True)],
        ["取消腾讯视频会员", "把iCloud订阅取消掉"],
    ),
    "subscription.resume": IntentSpec(
        "subscription.resume", "订阅管理", "恢复已取消订阅的自动续费",
        [L("merchant", "订阅商户名称", True)],
        ["恢复腾讯视频VIP自动续费", "重新开通Keep会员"],
    ),
    "subscription.change_cycle": IntentSpec(
        "subscription.change_cycle", "订阅管理", "修改已有订阅的下期周期和续费价格",
        [
            L("merchant", "订阅商户名称", True),
            L("term_months", "下期周期月数", True, "integer"),
            L("renewal_price", "下期续费价格", True, "number"),
            L("auto_renew", "下期是否自动续费", False, "boolean"),
        ],
        ["把腾讯视频改成12个月续费，续费价288元"],
    ),
    "membership.purchase": IntentSpec(
        "membership.purchase", "订阅管理",
        "批量购买一个或多个平台的会员周期",
        [L("items", "会员购买项目列表", True, "array")],
        [
            "充一个QQ音乐绿钻的年度会员",
            "帮我充腾讯视频3个月60元、Keep12个月199元",
            "开通B站大会员12个月148元，不自动续费",
            "购买网易云音乐3个月45元，自动续费，续费价同本次价格",
            "给稻壳阅读器充1个月12元、百度网盘6个月150元",
        ],
    ),
    "subscription.remind": IntentSpec(
        "subscription.remind", "订阅管理", "查看即将扣费的订阅并设置提醒",
        [L("days", "提前提醒天数", False, "integer")],
        ["最近有哪些订阅要扣费", "帮我设置续费提醒"],
    ),
    # ---------------- 跨场景联动 ----------------
    "crossscene.birthday_plan": IntentSpec(
        "crossscene.birthday_plan", "跨场景联动",
        "识别重要日期并自动规划：锁定资金 + 预约礼物下单",
        [
            L("person", "对象，如 老婆/爱人"),
            L("budget", "预算（元）", False, "number"),
        ],
        ["我老婆快生日了，帮我准备一下", "爱人下个月生日，帮我安排惊喜", "帮我准备生日惊喜，预算1000"],
    ),
    "crossscene.daily_brief": IntentSpec(
        "crossscene.daily_brief", "跨场景联动", "主动汇总今日待办与风险提醒",
        [], ["今天有什么要注意的", "帮我做个每日简报"],
    ),
    "goal.create": IntentSpec(
        "goal.create", "跨场景联动", "创建资金目标并锁定活期资金",
        [
            L("name", "目标名称", True),
            L("target_amount", "目标金额（元）", True, "number"),
            L("deadline", "目标日期"),
        ],
        ["建一个5000元的旅行基金", "锁定2000块做应急金"],
    ),
    "goal.list": IntentSpec(
        "goal.list", "跨场景联动", "查看资金目标与锁定金额",
        [], ["我的攒钱目标", "看看我锁了多少钱"],
    ),
    # ---------------- 账户服务 ----------------
    "account.balance": IntentSpec(
        "account.balance", "账户服务", "查询余额与总资产",
        [], ["我还有多少钱", "查一下余额"],
    ),
    "profile.show": IntentSpec(
        "profile.show", "账户服务", "查看客户画像与风险偏好",
        [], ["我的资料", "我的风险等级是什么"],
    ),
    "help": IntentSpec(
        "help", "账户服务", "查看 Agent 支持的全部能力",
        [], ["你能做什么", "帮助", "有什么功能"],
    ),
    "agent.about": IntentSpec(
        "agent.about", "账户服务",
        "回答关于本 Agent 自身的问题：用什么大模型、怎么实现的、技术架构",
        [], ["你用的什么大模型", "你是谁", "你是怎么实现的"],
    ),
}


def catalog_text() -> str:
    """生成给大模型看的意图清单文本。"""
    lines: List[str] = []
    for spec in INTENTS.values():
        slots = "、".join(
            f"{s.name}{'*' if s.required else ''}" for s in spec.slots
        ) or "无"
        examples = " / ".join(spec.examples[:2])
        lines.append(f"- {spec.name} | {spec.description} | 槽位: {slots} | 例如: {examples}")
    return "\n".join(lines)


def slot_dictionary_text() -> str:
    """生成槽位字典，帮助大模型统一命名。"""
    return (
        "payee=收款人, amount=金额(数字,单位元), note=备注, when=时间表达原样保留, "
        "repeat=once/monthly/weekly, total=AA总金额, participants=参与人姓名数组, "
        "period=期间关键词, category=消费类别, keyword=检索关键词, "
        "product=理财产品名, risk_level=1-5整数, horizon=short/mid/long, "
        "card=卡片描述, new_limit=新额度, scope=online/overseas/contactless, enable=布尔, "
        "merchant=订阅商户, person=对象称呼, budget=预算金额, name=目标名称, "
        "target_amount=目标金额, deadline=日期, kind=卡类型, days=天数"
    )

