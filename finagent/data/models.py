"""领域模型定义。

全部使用 dataclass，好处是：
1. 结构清晰，字段含义一眼可见，方便评委阅读；
2. 用 `to_dict` / `build` 与 JSON 存储无损互转；
3. 新增业务对象时只需加一个 dataclass，不影响其它模块。
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Type, TypeVar

T = TypeVar("T")


def build(cls: Type[T], data: Dict[str, Any]) -> T:
    """从 dict 安全构造 dataclass（自动忽略多余字段，便于版本升级）。"""
    names = {f.name for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
    return cls(**{k: v for k, v in data.items() if k in names})  # type: ignore[call-arg]


def to_dict(obj: Any) -> Dict[str, Any]:
    """dataclass -> dict（递归展开）。"""
    return dataclasses.asdict(obj)


# --------------------------------------------------------------------------
# 用户与账户
# --------------------------------------------------------------------------
@dataclass
class User:
    """客户画像。风控、理财推荐、跨场景联动都会读取这里的字段。"""

    user_id: str
    name: str
    phone: str
    gender: str = "unknown"
    age: int = 30
    city: str = "深圳"
    occupation: str = ""
    monthly_income: float = 0.0
    risk_profile: str = "平衡型"          # 保守型 / 稳健型 / 平衡型 / 成长型 / 进取型
    risk_score: int = 3                    # 1-5
    birthday: str = ""
    preferences: Dict[str, Any] = field(default_factory=dict)
    verified: bool = False                 # 是否已完成实名/人脸核身（原型里用布尔模拟）
    source: str = "agent:simulation"
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    is_live: bool = False
    is_demo: bool = True


@dataclass
class Account:
    """名下账户：活期 / 定期 / 信用 / 电子钱包。"""

    account_id: str
    user_id: str
    kind: str                  # current 活期 / savings 定期 / credit 信用 / wallet 电子钱包
    name: str
    tail: str                  # 卡号后四位
    balance: float = 0.0       # 活期/定期=余额；信用=已用额度（负数表示欠款）
    currency: str = "CNY"
    credit_limit: float = 0.0  # 仅信用卡
    locked: float = 0.0        # 被目标/计划锁定的金额
    status: str = "active"
    source: str = "agent:simulation"
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    is_live: bool = False
    is_demo: bool = True

    @property
    def available(self) -> float:
        """可用余额。"""
        if self.kind == "credit":
            return max(self.credit_limit - self.balance, 0.0)
        return max(self.balance - self.locked, 0.0)


@dataclass
class Payee:
    """收款人（常用联系人）。用于按人名/手机号/备注转账。"""

    payee_id: str
    user_id: str
    name: str
    phone: str = ""
    bank: str = "微众银行"
    tail: str = ""
    alias: str = ""            # 备注/昵称，如"老婆""房东"
    relation: str = ""         # 家人 / 朋友 / 同事 / 商户
    tags: List[str] = field(default_factory=list)
    last_used: str = ""
    # 各收款渠道的账号：手机号（数字人民币钱包）/ 微信 / 支付宝
    wallet_phone: str = ""
    wechat: str = ""
    alipay: str = ""
    source: str = "agent:simulation"
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    is_live: bool = False
    is_demo: bool = True
    profile_person_id: str = ""


@dataclass
class Transaction:
    """一笔流水。金额正数=收入，负数=支出（沿用银行流水习惯）。"""

    txn_id: str
    user_id: str
    account_id: str
    timestamp: str            # ISO8601，如 2026-10-12T09:31:00
    amount: float
    merchant: str
    category: str             # 餐饮/交通/购物/生活缴费/娱乐/医疗/教育/住房/转账/理财/收入
    channel: str = "APP"      # 扫码 / APP / 转账 / 刷卡 / 代扣
    counterparty: str = ""    # 对方姓名或手机号
    note: str = ""
    status: str = "success"
    city: str = "深圳"
    is_subscription: bool = False
    subscription_id: str = ""
    tags: List[str] = field(default_factory=list)
    import_batch_id: str = ""
    source: str = "agent:simulation"
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    is_live: bool = False
    is_demo: bool = True

    @property
    def direction(self) -> str:
        return "in" if self.amount >= 0 else "out"


# --------------------------------------------------------------------------
# 卡片
# --------------------------------------------------------------------------
@dataclass
class Card:
    """银行卡。含交易开关，支撑"交易限制/解锁"场景。"""

    card_id: str
    user_id: str
    kind: str                  # debit 借记卡 / credit 信用卡 / virtual 虚拟卡 / supplementary 附属卡
    brand: str = "银联"
    tail: str = ""
    status: str = "active"     # active / frozen 冻结 / lost 挂失 / cancelled 销卡 / pending 待激活
    credit_limit: float = 0.0
    used: float = 0.0
    daily_limit: float = 20_000.0
    single_limit: float = 5_000.0
    online_enabled: bool = True
    overseas_enabled: bool = False
    contactless_enabled: bool = True
    issued_at: str = ""
    note: str = ""
    source: str = "agent:simulation"
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    is_live: bool = False
    is_demo: bool = True


# --------------------------------------------------------------------------
# 订阅 / 代扣
# --------------------------------------------------------------------------
@dataclass
class Subscription:
    """自动续费订阅（由 Agent 从流水里识别出来）。"""

    sub_id: str
    user_id: str
    merchant: str
    amount: float
    cycle: str = "monthly"      # monthly / yearly / weekly
    category: str = "会员服务"
    account_id: str = ""
    next_charge_date: str = ""
    expires_at: str = ""
    last_charge_date: str = ""
    status: str = "active"      # active / cancelled / paused / trial
    auto_renew: bool = True
    term_months: int = 1
    pending_term_months: int = 0
    pending_amount: float = 0.0
    pending_auto_renew: bool = False
    pending_effective_at: str = ""
    free_trial_end: str = ""
    auto_detected: bool = True
    price_history: List[float] = field(default_factory=list)
    source: str = "agent:simulation"
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    is_live: bool = False
    is_demo: bool = True


@dataclass
class MembershipOrder:
    """One confirmed batch membership purchase."""

    order_id: str
    user_id: str
    account_id: str
    total_amount: float
    status: str = "completed"
    created_at: str = ""
    confirmed_at: str = ""
    executed_at: str = ""
    transaction_id: str = ""
    failure_reason: str = ""
    source: str = "user:quote"
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    is_live: bool = False
    is_demo: bool = True


@dataclass
class MembershipOrderItem:
    """One platform/term line in a membership order."""

    item_id: str
    order_id: str
    user_id: str
    platform: str
    term_months: int
    purchase_price: float
    auto_renew: bool = False
    renewal_price: float = 0.0
    status: str = "pending"       # pending / fulfilled / failed
    subscription_id: str = ""
    result: str = ""
    source: str = "user:quote"
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    is_live: bool = False
    is_demo: bool = True


# --------------------------------------------------------------------------
# 理财
# --------------------------------------------------------------------------
@dataclass
class WealthProduct:
    """理财产品。risk_level 1-5 对应 R1-R5。"""

    product_id: str
    name: str
    category: str              # 货币基金 / 债券基金 / 混合基金 / 指数基金 / 股票基金 / 大额存单
    risk_level: int
    expected_return: float     # 演示参数，仅用于原型测算，不是实时收益率
    min_amount: float = 1.0
    term_days: int = 0         # 0 表示活期随时赎回
    liquidity: str = "T+1"
    issuer: str = "微众银行"
    tags: List[str] = field(default_factory=list)
    description: str = ""
    # 真实行情代码：股票/ETF 用 sh600036 这种格式，场外基金用 6 位代码
    symbol: str = ""
    symbol_type: str = ""          # stock / fund / ""（无行情数据）
    asset_class: str = "fund"      # stock / bond / fund / derivative / deposit
    fee_rate: float = 0.0
    redemption_rule: str = ""
    risk_disclosure: str = ""
    source: str = "agent:simulation"
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    is_live: bool = False
    is_demo: bool = True

    @property
    def has_live_data(self) -> bool:
        return bool(self.symbol and self.symbol_type)


@dataclass
class Holding:
    """理财持仓。"""

    holding_id: str
    user_id: str
    product_id: str
    principal: float
    market_value: float
    purchase_date: str
    status: str = "holding"     # holding / redeemed
    redeem_date: str = ""
    profit: float = 0.0
    source: str = "agent:simulation"
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    is_live: bool = False
    is_demo: bool = True


@dataclass
class MerchantOffer:
    """External merchant offer for gifts and other cross-scene services."""

    offer_id: str
    merchant: str
    item: str
    price: float
    inventory: int = 0
    delivery_cities: List[str] = field(default_factory=list)
    delivery_radius_km: float = 0.0
    available_from: str = ""
    available_to: str = ""
    source: str = "agent:simulation"
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    is_live: bool = False
    is_demo: bool = True


@dataclass
class UploadBatch:
    """A user-uploaded bill import batch."""

    batch_id: str
    user_id: str
    filename: str
    imported_at: str
    record_count: int
    total_expense: float
    backend: str = ""
    transaction_ids: List[str] = field(default_factory=list)
    source: str = "upload:user"
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    is_live: bool = False
    is_demo: bool = False


# --------------------------------------------------------------------------
# 计划 / 目标 / 跨场景
# --------------------------------------------------------------------------
@dataclass
class ScheduledTask:
    """定时任务：定时转账、续费提醒、生日礼物下单等都由它承载。"""

    task_id: str
    user_id: str
    kind: str                  # scheduled_transfer / gift_order / reminder / subscription_cancel
    run_date: str              # YYYY-MM-DD
    title: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    status: str = "pending"    # pending / done / cancelled / failed
    repeat: str = "once"       # once / monthly / weekly
    created_at: str = ""
    executed_at: str = ""
    result: str = ""
    source: str = "agent:simulation"
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    is_live: bool = False
    is_demo: bool = True


@dataclass
class Goal:
    """资金目标：生日惊喜资金、旅行基金、应急金等。"""

    goal_id: str
    user_id: str
    name: str
    target_amount: float
    locked_amount: float = 0.0
    account_id: str = ""
    deadline: str = ""
    status: str = "active"     # active / achieved / cancelled
    scenario: str = ""         # birthday / travel / emergency ...
    note: str = ""
    created_at: str = ""
    source: str = "agent:simulation"
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    is_live: bool = False
    is_demo: bool = True


@dataclass
class AaSheet:
    """AA 收款单。"""

    sheet_id: str
    user_id: str
    title: str
    total_amount: float
    participants: List[Dict[str, Any]] = field(default_factory=list)
    status: str = "collecting"
    created_at: str = ""
    note: str = ""
    source: str = "agent:simulation"
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    is_live: bool = False
    is_demo: bool = True


@dataclass
class Alert:
    """Agent 主动产生的提醒 / 风控告警。"""

    alert_id: str
    user_id: str
    level: str                 # info / warning / danger
    title: str
    detail: str = ""
    created_at: str = ""
    source: str = ""
    read: bool = False
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    is_live: bool = False
    is_demo: bool = True

