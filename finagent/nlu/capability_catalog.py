"""Single source of truth for business navigation and capability discovery."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class CapabilityAction:
    action_id: str
    label: str
    prompt: str
    intent: str
    input_mode: str = "prompt"
    panel_id: str = ""
    aliases: Tuple[str, ...] = field(default_factory=tuple)

    def payload(self) -> Dict[str, str]:
        shortcut_text = self.label if self.input_mode == "prompt" else self.prompt
        return {
            "id": self.action_id,
            "label": self.label,
            "prompt": shortcut_text,
            "intent": self.intent,
            "input_mode": self.input_mode,
            "panel_id": self.panel_id,
        }


@dataclass(frozen=True)
class CapabilityCategory:
    category_id: str
    label: str
    description: str
    actions: Tuple[CapabilityAction, ...]

    def payload(self) -> Dict[str, object]:
        return {
            "id": self.category_id,
            "label": self.label,
            "description": self.description,
            "actions": [action.payload() for action in self.actions],
        }


CATEGORIES: Tuple[CapabilityCategory, ...] = (
    CapabilityCategory(
        "transfer",
        "智能转账",
        "收付款、余额与定时任务",
        (
            CapabilityAction(
                "balance", "总资产与余额", "我还有多少钱", "account.balance",
                aliases=("总资产", "余额", "还有多少钱"),
            ),
            CapabilityAction(
                "transfer_send", "给他人转账", "给老婆转2000块", "transfer.send",
                aliases=("转账", "转给", "打点钱", "汇款"),
            ),
            CapabilityAction(
                "transfer_schedule", "定时/周期转账", "下个月5号给妈妈转500",
                "transfer.schedule",
                aliases=("定时转账", "自动转账", "每月转", "每周转"),
            ),
            CapabilityAction(
                "transfer_aa", "AA 收款", "昨晚聚餐960，我、王强、李思琪AA",
                "transfer.split_aa",
                aliases=("AA", "平摊", "分摊", "均摊"),
            ),
            CapabilityAction(
                "tasks", "待执行任务", "我有哪些定时转账",
                "transfer.list_scheduled",
                aliases=("待执行任务", "定时任务", "预约任务"),
            ),
        ),
    ),
    CapabilityCategory(
        "bills",
        "账单分析",
        "周期账单、异常与消费检索",
        (
            CapabilityAction(
                "bill_week", "本周账单", "生成本周账单报告", "bill.report",
                aliases=("本周账单", "这周账单", "最近7天账单"),
            ),
            CapabilityAction(
                "bill_month", "本月账单", "生成本月账单报告", "bill.report",
                aliases=("本月账单", "这个月账单"),
            ),
            CapabilityAction(
                "bill_year", "年度账单", "生成本年度账单报告", "bill.report",
                aliases=("年度账单", "今年账单", "年报"),
            ),
            CapabilityAction(
                "bill_anomaly", "异常交易识别", "帮我看看最近有没有异常交易",
                "bill.anomaly",
                aliases=("异常交易", "盗刷", "可疑交易", "重复扣款"),
            ),
            CapabilityAction(
                "bill_category", "消费分类统计", "分析我这个月的消费分类",
                "bill.analyze",
                aliases=("消费分类", "钱都花哪了", "花哪了", "支出构成"),
            ),
            CapabilityAction(
                "bill_search", "消费记录检索", "查一下我最近在餐饮上的消费",
                "bill.search",
                aliases=("消费记录", "花在", "花了多少", "查消费"),
            ),
            CapabilityAction(
                "bill_upload", "上传识别账单", "识别账单", "bill.scan_receipt",
                input_mode="upload",
                aliases=("上传账单", "识别账单", "账单截图", "导入账单"),
            ),
            CapabilityAction(
                "bill_formats", "支持哪些账单格式", "可以上传哪些格式的账单",
                "bill.supported_formats",
                aliases=("账单格式", "支持什么格式", "能上传什么文件"),
            ),
            CapabilityAction(
                "bill_history", "查看上传记录", "", "bill.scan_receipt",
                input_mode="panel", panel_id="upload-history-overlay",
            ),
        ),
    ),
    CapabilityCategory(
        "wealth",
        "理财操作",
        "风险评估、推荐、申购与赎回",
        (
            CapabilityAction(
                "wealth_risk", "风险测评", "帮我做个风险测评",
                "wealth.risk_assess",
                aliases=("风险测评", "风险承受", "测评"),
            ),
            CapabilityAction(
                "wealth_recommend", "产品推荐", "推荐几个稳健的理财产品",
                "wealth.recommend",
                aliases=("推荐理财产品", "买什么", "产品推荐"),
            ),
            CapabilityAction(
                "wealth_compare", "产品对比", "比较一下稳健型理财产品",
                "wealth.recommend",
                aliases=("产品对比", "比较产品", "对比理财"),
            ),
            CapabilityAction(
                "wealth_portfolio", "持仓与收益", "我的理财持仓",
                "wealth.portfolio",
                aliases=("理财持仓", "持仓收益", "赚了多少"),
            ),
            CapabilityAction(
                "wealth_subscribe", "一键申购", "买1万块稳利债券A",
                "wealth.subscribe",
                aliases=("申购", "买入", "认购"),
            ),
            CapabilityAction(
                "wealth_redeem", "一键赎回", "赎回稳利债券A",
                "wealth.redeem",
                aliases=("赎回", "卖出"),
            ),
        ),
    ),
    CapabilityCategory(
        "cards",
        "卡片管理",
        "申请、交易权限、冻结与挂失",
        (
            CapabilityAction(
                "card_list", "卡片列表", "我有哪些卡", "card.list",
                aliases=("我的卡", "卡片列表", "有哪些银行卡"),
            ),
            CapabilityAction(
                "card_apply", "申请卡片", "我想申请一张信用卡", "card.apply",
                aliases=("申请卡", "开卡", "办卡", "申请信用卡"),
            ),
            CapabilityAction(
                "card_bind", "绑定银行卡", "绑定一张工商银行的储蓄卡",
                "card.bind",
                aliases=("绑定银行卡", "添加银行卡", "绑卡"),
            ),
            CapabilityAction(
                "card_limit", "额度调整", "把信用卡额度提到3万",
                "card.limit_adjust",
                aliases=("额度调整", "提额", "降额", "限额"),
            ),
            CapabilityAction(
                "card_online", "线上交易设置", "关闭信用卡的线上交易",
                "card.control",
                aliases=("线上交易", "网络支付", "关闭线上"),
            ),
            CapabilityAction(
                "card_overseas", "境外交易设置", "关闭信用卡的境外交易",
                "card.control",
                aliases=("境外交易", "海外交易", "境外消费"),
            ),
            CapabilityAction(
                "card_contactless", "小额免密设置", "关闭信用卡的小额免密",
                "card.control",
                aliases=("小额免密", "免密支付", "闪付"),
            ),
            CapabilityAction(
                "card_freeze", "冻结卡片", "把信用卡冻结", "card.freeze",
                aliases=("冻结卡", "锁卡", "锁上", "暂停卡"),
            ),
            CapabilityAction(
                "card_unlock", "解冻卡片", "解冻信用卡", "card.unlock",
                aliases=("解冻卡", "解锁卡", "恢复卡片"),
            ),
            CapabilityAction(
                "card_lost", "挂失补卡", "我的卡丢了，帮我挂失",
                "card.report_lost",
                aliases=("挂失", "补卡", "卡丢了"),
            ),
        ),
    ),
    CapabilityCategory(
        "subscriptions",
        "订阅管理",
        "办理、续费、取消与周期调整",
        (
            CapabilityAction(
                "sub_detect", "自动续费检测", "帮我看看有哪些自动续费",
                "subscription.detect",
                aliases=("自动续费", "自动扣款", "订阅检测"),
            ),
            CapabilityAction(
                "sub_list", "订阅清单", "我的订阅都有什么", "subscription.list",
                aliases=("订阅清单", "我的订阅", "有哪些会员"),
            ),
            CapabilityAction(
                "sub_purchase", "会员办理/充值",
                "帮我充腾讯视频3个月60元、Keep12个月199元",
                "membership.purchase",
                aliases=("充会员", "会员充值", "购买会员", "开通会员"),
            ),
            CapabilityAction(
                "sub_cancel", "取消自动续费", "取消腾讯视频会员",
                "subscription.cancel",
                aliases=("取消续费", "停止自动续费", "关掉自动续费", "退订"),
            ),
            CapabilityAction(
                "sub_resume", "恢复自动续费", "恢复腾讯视频VIP自动续费",
                "subscription.resume",
                aliases=("恢复续费", "重新开通", "续回去"),
            ),
            CapabilityAction(
                "sub_change", "修改续费周期",
                "把腾讯视频改成12个月续费，续费价288元",
                "subscription.change_cycle",
                aliases=("修改续费周期", "改成年度续费", "调整周期"),
            ),
            CapabilityAction(
                "sub_remind", "续费提醒", "最近有哪些订阅要扣费",
                "subscription.remind",
                aliases=("续费提醒", "快到期", "什么时候扣费"),
            ),
        ),
    ),
    CapabilityCategory(
        "gifts",
        "人际礼物",
        "生日、伴手礼、关爱对象与礼物资金",
        (
            CapabilityAction(
                "gift_birthday", "生日惊喜", "我老婆快生日了，帮我准备一下",
                "crossscene.birthday_plan",
                aliases=("生日惊喜", "准备生日", "生日礼物", "生日安排"),
            ),
            CapabilityAction(
                "gift_souvenir", "伴手礼/礼物预约", "给对象准备一份伴手礼",
                "crossscene.gift_plan",
                aliases=("伴手礼", "礼物预约", "准备礼物", "纪念日礼物", "送礼"),
            ),
            CapabilityAction(
                "gift_people", "关爱对象与纪念日", "", "profile.show",
                input_mode="panel", panel_id="profile-settings-overlay",
                aliases=("关爱对象", "纪念日", "重要日期", "关心的人"),
            ),
            CapabilityAction(
                "gift_goal_create", "礼物资金目标", "锁定1000块做礼物基金",
                "goal.create",
                aliases=("礼物基金", "礼物资金", "攒钱买礼物"),
            ),
            CapabilityAction(
                "gift_goal_list", "查看礼物资金", "我的攒钱目标",
                "goal.list",
                aliases=("礼物目标", "攒钱目标", "心愿清单"),
            ),
        ),
    ),
)


GLOBAL_ACTIONS: Tuple[CapabilityAction, ...] = (
    CapabilityAction(
        "daily_brief", "每日简报", "今天有什么要注意的",
        "crossscene.daily_brief",
        aliases=("每日简报", "今日简报", "今日待办", "今天要处理什么", "有什么要处理"),
    ),
    CapabilityAction(
        "all_capabilities", "全部能力", "你能做什么", "help",
        aliases=("全部能力", "能做什么", "有什么功能"),
    ),
)


def capability_catalog_payload() -> Dict[str, object]:
    return {
        "categories": [category.payload() for category in CATEGORIES],
        "global_actions": [action.payload() for action in GLOBAL_ACTIONS],
    }


def resolve_capability_intent(text: str) -> Optional[str]:
    """Resolve a paraphrased request through catalog aliases."""
    normalized = "".join((text or "").lower().split())
    if not normalized:
        return None
    scores: Dict[str, int] = {}
    for action in (*GLOBAL_ACTIONS, *(a for c in CATEGORIES for a in c.actions)):
        for alias in action.aliases:
            key = "".join(alias.lower().split())
            if key and key in normalized:
                scores[action.intent] = max(scores.get(action.intent, 0), len(key))
    return max(scores, key=scores.get) if scores else None


def resolve_capability_label(text: str) -> Optional[str]:
    """Resolve an exact card label before broader rule matching."""
    normalized = "".join((text or "").lower().split())
    if not normalized:
        return None
    for action in (*GLOBAL_ACTIONS, *(a for c in CATEGORIES for a in c.actions)):
        if "".join(action.label.lower().split()) == normalized:
            return action.intent
    return None
