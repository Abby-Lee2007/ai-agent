"""模拟数据生成。

全部数据都是伪造的，仅用于赛事演示。生成逻辑有两个特点：
1. 覆盖最近 5 个月的流水，让账单分析 / 异常识别有真实的分析对象；
2. 刻意埋入 4 类"可疑交易"，用于演示异常交易识别能力。
"""
from __future__ import annotations

import calendar
import random
from datetime import date, datetime
from typing import List, Tuple

from finagent.data.card_ledger import DEMO_CARD_PASSWORD, hash_card_password
from finagent.data.models import (
    Account,
    Card,
    Holding,
    MerchantOffer,
    Payee,
    Subscription,
    User,
    WealthProduct,
)


def _mark_demo(record, store, source: str = "seed:demo"):
    record.source = source
    record.updated_at = store.clock.iso()
    record.is_live = False
    record.is_demo = True
    return record


RNG_SEED = 20260101

# 商户池：分类 -> [(商户名, 最小金额, 最大金额, 权重)]
MERCHANTS = {
    "餐饮": [
        ("星巴克咖啡", 28, 62, 3),
        ("喜茶", 18, 45, 3),
        ("海底捞火锅", 120, 380, 1),
        ("美团外卖", 22, 78, 6),
        ("楼下肠粉店", 8, 22, 4),
        ("奈雪的茶", 20, 42, 2),
    ],
    "交通": [
        ("深圳地铁", 2, 12, 8),
        ("滴滴出行", 15, 68, 4),
        ("出租车", 18, 55, 2),
        ("中石化加油站", 200, 450, 1),
    ],
    "购物": [
        ("京东商城", 89, 1299, 3),
        ("淘宝", 45, 680, 4),
        ("优衣库", 99, 599, 2),
        ("山姆会员店", 180, 860, 2),
        ("名创优品", 25, 120, 2),
    ],
    "生活缴费": [
        ("南方电网", 85, 260, 1),
        ("深圳燃气", 30, 90, 1),
        ("中国移动话费", 58, 128, 1),
        ("物业管理费", 180, 320, 1),
    ],
    "娱乐": [
        ("猫眼电影", 40, 120, 2),
        ("腾讯游戏", 30, 648, 2),
        ("健身私教课", 200, 400, 1),
        ("KTV", 120, 380, 1),
    ],
    "医疗": [
        ("深圳市人民医院", 60, 780, 1),
        ("海王星辰药房", 25, 180, 2),
    ],
    "教育": [
        ("得到App课程", 99, 399, 1),
        ("深圳书城", 45, 260, 1),
    ],
}

SUBSCRIPTION_CATALOG = [
    # 商户, 金额, 每月扣费日, 分类, 涨价后金额(可选)
    ("腾讯视频VIP", 25.0, 8, "视频会员", None),
    ("QQ音乐绿钻", 15.0, 12, "音乐会员", None),
    ("iCloud+ 云存储", 6.0, 3, "云存储", 21.0),
    ("美团外卖会员", 15.0, 1, "生活会员", None),
    ("Keep会员", 19.0, 15, "健身会员", None),
    ("百度网盘超级会员", 25.0, 22, "云存储", None),
]


def _months_back(today: date, count: int = 12) -> List[Tuple[int, int]]:
    """返回最近 count 个月的 (年, 月)，按时间正序。"""
    y, m = today.year, today.month
    out: List[Tuple[int, int]] = []
    for _ in range(count):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def _ts(y: int, m: int, d: int, h: int = 12, mi: int = 0) -> str:
    return datetime(y, m, d, h, mi).strftime("%Y-%m-%dT%H:%M:%S")


# --------------------------------------------------------------------------
def _add_users(store) -> None:
    owner = User(
        user_id="u_001",
        name="陈晓明",
        phone="13800008888",
        gender="male",
        age=31,
        city="深圳",
        occupation="互联网产品经理",
        monthly_income=18500.0,
        risk_profile="平衡型",
        risk_score=3,
        birthday="1994-06-18",
        verified=True,
        preferences={
            "spouse": "林雅雯",
            "spouse_birthday": "1996-11-20",
            "anniversary": "2027-05-20",
            "monthly_budget": 9000.0,
            "budget_categories": ["餐饮", "购物", "娱乐"],
            "favorite_gift": ["鲜花", "蛋糕"],
            "avoid": ["海鲜"],
            "notify_channel": "APP",
        },
    )
    store.users[owner.user_id] = _mark_demo(owner, store)
    store.users["u_002"] = _mark_demo(User(
        user_id="u_002",
        name="林雅雯",
        phone="13900002222",
        gender="female",
        age=29,
        city="深圳",
        occupation="中学教师",
        monthly_income=13000.0,
        risk_profile="稳健型",
        risk_score=2,
        birthday="1996-11-20",
        verified=True,
        preferences={"spouse": "陈晓明"},
    ), store)
    store.users["u_003"] = _mark_demo(User(
        user_id="u_003",
        name="王强",
        phone="13700003333",
        city="深圳",
        occupation="大学同学",
        risk_profile="进取型",
        risk_score=5,
        birthday="1994-03-02",
        verified=True,
    ), store)
    store.users["u_004"] = _mark_demo(User(
        user_id="u_004",
        name="李思琪",
        phone="13600004444",
        gender="female",
        city="深圳",
        occupation="同事",
        risk_profile="成长型",
        risk_score=4,
        birthday="1997-08-09",
        verified=True,
    ), store)


def _add_accounts(store) -> None:
    accounts = [
        Account("ACC000001", "u_001", "current", "活期账户", "6222", 32860.52),
        Account("ACC000002", "u_001", "savings", "定期存款", "6688", 150000.00),
        Account("ACC000003", "u_001", "credit", "微众信用卡", "8877", 12340.00, credit_limit=50000.0),
        Account("ACC000004", "u_001", "wallet", "WeBank 电子钱包", "0000", 1200.00),
        Account("ACC000005", "u_001", "wallet", "上传账单余额", "BILL", 0.00),
    ]
    for acc in accounts:
        store.accounts[acc.account_id] = _mark_demo(acc, store)
    upload_ledger = store.accounts["ACC000005"]
    upload_ledger.source = "agent:upload-ledger"
    upload_ledger.is_demo = False
    store.counters["accounts"] = 5


def _add_cards(store) -> None:
    cards = [
        Card(
            card_id="CARD000001",
            user_id="u_001",
            kind="credit",
            account_id="ACC000003",
            brand="银联",
            tail="8877",
            credit_limit=50000.0,
            used=12340.0,
            payment_password_hash=hash_card_password(DEMO_CARD_PASSWORD),
            daily_limit=20000.0,
            single_limit=5000.0,
            overseas_enabled=True,
            issued_at="2024-05-18T00:00:00",
            note="主刷信用卡",
        ),
        Card(
            card_id="CARD000002",
            user_id="u_001",
            kind="debit",
            account_id="ACC000001",
            brand="银联",
            tail="6222",
            balance=32860.52,
            payment_password_hash=hash_card_password(DEMO_CARD_PASSWORD),
            daily_limit=50000.0,
            single_limit=20000.0,
            overseas_enabled=True,
            issued_at="2022-03-01T00:00:00",
            note="工资卡",
        ),
        Card(
            card_id="CARD000003",
            user_id="u_001",
            kind="virtual",
            account_id="ACC000004",
            brand="微众银行",
            tail="3011",
            balance=1200.00,
            payment_password_hash=hash_card_password(DEMO_CARD_PASSWORD),
            daily_limit=5000.0,
            single_limit=1000.0,
            contactless_enabled=False,
            issued_at="2026-02-11T00:00:00",
            note="网购专用虚拟卡",
        ),
        Card(
            card_id="CARD000004",
            user_id="u_001",
            kind="debit",
            account_id="ACC000002",
            brand="微众银行",
            tail="6688",
            balance=150000.00,
            payment_password_hash=hash_card_password(DEMO_CARD_PASSWORD),
            daily_limit=20000.0,
            single_limit=20000.0,
            overseas_enabled=False,
            issued_at="2021-08-16T00:00:00",
            note="定期存单",
        ),
    ]
    for c in cards:
        store.cards[c.card_id] = _mark_demo(c, store)
    store.counters["cards"] = 4


def _add_payees(store) -> None:
    payees = [
        Payee("PAY000001", "u_001", "林雅雯", "13900002222", "微众银行", "2222", "老婆", "家人",
              ["家人", "常用"], "2026-09-20T20:15:00",
              wallet_phone="13900002222", wechat="linyawen_0929", alipay="13900002222"),
        Payee("PAY000002", "u_001", "王强", "13700003333", "招商银行", "3333", "老王", "朋友",
              ["朋友", "AA常客"], "2026-10-02T19:40:00",
              wallet_phone="13700003333", wechat="wangqiang_sz", alipay="13700003333"),
        Payee("PAY000003", "u_001", "李思琪", "13600004444", "中国银行", "4444", "思琪", "同事",
              ["同事"], "2026-09-11T12:20:00",
              wallet_phone="13600004444", wechat="siqi_li", alipay="13600004444"),
        Payee("PAY000004", "u_001", "张建国", "13500005555", "工商银行", "5555", "房东", "其他",
              ["固定支出"], "2026-10-05T09:00:00",
              wallet_phone="13500005555", wechat="", alipay="13500005555"),
        Payee("PAY000005", "u_001", "陈妈妈", "13300006666", "农业银行", "6666", "妈妈", "家人",
              ["家人"], "2026-10-01T10:30:00",
              wallet_phone="13300006666", wechat="chen_mama", alipay="13300006666"),
    ]
    for p in payees:
        store.payees[p.payee_id] = p
    store.counters["payees"] = 5


def _add_products(store) -> None:
    products = [
        WealthProduct("P001", "微众活期+", "货币基金", 1, 0.0210, 1.0, 0, "T+0",
                      "微众银行", ["活期", "随存随取"], "低波动现金管理工具，适合短期闲钱",
                      asset_class="fund"),
        WealthProduct("P002", "稳利债券A", "债券基金", 2, 0.0360, 100.0, 30, "T+1",
                      "微众银行", ["稳健", "固收"], "以中短久期利率债为主，波动小",
                      asset_class="fund"),
        WealthProduct("P003", "平衡配置精选", "混合基金", 3, 0.0620, 100.0, 90, "T+2",
                      "微众银行", ["股债平衡"], "股债 4:6 配置，兼顾收益与回撤",
                      asset_class="fund"),
        WealthProduct("P004", "沪深300指数增强", "指数基金", 4, 0.0850, 100.0, 180, "T+2",
                      "微众银行", ["宽基", "指数增强"], "跟踪沪深300并做量化增强",
                      asset_class="fund"),
        WealthProduct("P005", "科技成长先锋", "股票基金", 5, 0.1280, 1000.0, 365, "T+3",
                      "微众银行", ["高波动", "主题"], "聚焦半导体、AI、新能源等成长赛道",
                      asset_class="fund"),
        WealthProduct("P006", "大额存单3年期", "大额存单", 1, 0.0235, 200000.0, 1095, "到期",
                      "微众银行", ["保本", "存款保险"], "受存款保险条例保护，锁定长期利率",
                      asset_class="deposit"),
        # ---- 权益类（A 股 / 场内 ETF），行情实时 ----
        WealthProduct("P007", "红利低波ETF", "指数基金", 4, 0.0, 100.0, 0, "T+1",
                      "华泰柏瑞", ["红利", "低波动", "权益"], "跟踪红利低波动指数，权益类里波动相对较小",
                      symbol="sh512890", symbol_type="stock", asset_class="stock"),
        WealthProduct("P008", "沪深300ETF", "指数基金", 4, 0.0, 100.0, 0, "T+1",
                      "华泰柏瑞", ["宽基", "权益"], "跟踪沪深300指数，比单只个股分散",
                      symbol="sh510300", symbol_type="stock", asset_class="stock"),
        WealthProduct("P009", "招商银行", "股票", 4, 0.0, 100.0, 0, "T+1",
                      "A股", ["个股", "银行", "高股息"], "A股银行龙头，高股息但仍是权益资产",
                      symbol="sh600036", symbol_type="stock", asset_class="stock"),
        WealthProduct("P010", "长江电力", "股票", 4, 0.0, 100.0, 0, "T+1",
                      "A股", ["个股", "公用事业", "高股息"], "公用事业龙头，现金流稳定，但仍会随大盘波动",
                      symbol="sh600900", symbol_type="stock", asset_class="stock"),
        WealthProduct("P011", "中国平安", "股票", 4, 0.0, 100.0, 0, "T+1",
                      "A股", ["个股", "保险", "蓝筹"], "A股保险龙头，蓝筹属性但仍是权益资产",
                      symbol="sh601318", symbol_type="stock", asset_class="stock"),
        # ---- 债券（交易所国债 / 可转债），行情实时 ----
        WealthProduct("P012", "16国债19", "国债", 2, 0.0, 1000.0, 0, "T+1",
                      "财政部", ["国债", "利率债"], "交易所上市的记账式国债，价格随利率波动",
                      symbol="sh019547", symbol_type="stock", asset_class="bond"),
        WealthProduct("P013", "南银转债", "可转债", 3, 0.0, 1000.0, 0, "T+1",
                      "上交所", ["可转债", "股债混合"], "可转股债券，兼具债性与股性，波动大于纯债",
                      symbol="sh113050", symbol_type="stock", asset_class="bond"),
        # ---- 场外基金（真实净值） ----
        WealthProduct("P014", "易方达裕丰回报债券", "债券基金", 2, 0.0, 10.0, 0, "T+1",
                      "易方达基金", ["债券基金", "真实净值"], "以债券为主的固收类基金",
                      symbol="000171", symbol_type="fund", asset_class="fund"),
        WealthProduct("P015", "银河创新成长混合", "混合基金", 4, 0.0, 10.0, 0, "T+1",
                      "银河基金", ["混合基金", "真实净值"], "偏成长风格的混合基金，波动较大",
                      symbol="519674", symbol_type="fund", asset_class="fund"),
        # ---- 衍生品（R5，仅作了解，需合格投资者） ----
        WealthProduct("P016", "上证50ETF期权", "期权", 5, 0.0, 0.0, 0, "T+0",
                      "上交所", ["期权", "杠杆", "衍生品"], "以50ETF为标的的期权，买方最大损失为权利金",
                      asset_class="derivative"),
        WealthProduct("P017", "沪深300股指期货", "股指期货", 5, 0.0, 0.0, 0, "T+0",
                      "中金所", ["期货", "杠杆", "衍生品"], "保证金交易，杠杆放大盈亏，可能穿仓",
                      asset_class="derivative"),
        WealthProduct("P018", "雪球结构收益凭证", "结构性产品", 5, 0.0, 100000.0, 365, "到期",
                      "券商资管", ["雪球", "结构性", "场外"], "敲入敲出结构，极端行情下可能损失本金",
                      asset_class="derivative"),
    ]
    for p in products:
        _mark_demo(p, store)
        p.fee_rate = 0.0015 if p.asset_class == "fund" else 0.0
        p.redemption_rule = f"{p.liquidity} 赎回；期限 {p.term_days or '随时'} 天"
        p.risk_disclosure = (
            f"R{p.risk_level} 产品，演示参数不构成收益承诺；"
            "实际收益与本金均可能发生波动。"
        )
        store.products[p.product_id] = p


def _add_holdings(store) -> None:
    holdings = [
        Holding("HLD000001", "u_001", "P001", 8000.0, 8052.30, "2026-08-12T10:00:00", profit=52.30),
        Holding("HLD000002", "u_001", "P002", 12000.0, 12110.40, "2026-07-03T14:20:00", profit=110.40),
        Holding("HLD000003", "u_001", "P003", 5000.0, 5118.75, "2026-06-20T09:30:00", profit=118.75),
    ]
    for h in holdings:
        store.holdings[h.holding_id] = _mark_demo(h, store)
    store.counters["holdings"] = 3


def _add_merchant_offers(store) -> None:
    offers = [
        MerchantOffer(
            "OFFER000001", "花间集", "鲜花", 299.0, 20, ["深圳"], 15.0,
            "2026-01-01", "2027-12-31",
        ),
        MerchantOffer(
            "OFFER000002", "Rose Only", "鲜花", 399.0, 8, ["深圳"], 10.0,
            "2026-01-01", "2027-12-31",
        ),
        MerchantOffer(
            "OFFER000003", "麦香蛋糕", "蛋糕", 368.0, 12, ["深圳"], 20.0,
            "2026-01-01", "2027-12-31",
        ),
        MerchantOffer(
            "OFFER000004", "云端甜品", "蛋糕", 428.0, 5, ["深圳"], 12.0,
            "2026-01-01", "2027-12-31",
        ),
    ]
    for offer in offers:
        store.merchant_offers[offer.offer_id] = _mark_demo(offer, store, source="seed:merchant-demo")
    store.counters["merchant_offers"] = len(offers)


def _add_subscriptions(store) -> None:
    """生成订阅数据；iCloud 在最近一个月涨价，用于演示订阅涨价识别。"""
    today = store.clock.today()
    for idx, (merchant, amount, day, category, new_amount) in enumerate(SUBSCRIPTION_CATALOG, start=1):
        history = [amount] * 4
        current_amount = amount
        if new_amount:
            current_amount = new_amount
            history = [amount, amount, amount, new_amount]
        # 下次扣费日：本月扣费日若已过，则顺延到下个月
        y, m = today.year, today.month
        if today.day >= day:
            m += 1
            if m == 13:
                m, y = 1, y + 1
        last_day = min(day, calendar.monthrange(y, m)[1])
        sub = Subscription(
            sub_id=f"SUB{idx:06d}",
            user_id="u_001",
            merchant=merchant,
            amount=current_amount,
            cycle="monthly",
            category=category,
            account_id="ACC000003" if category in {"视频会员", "音乐会员", "云存储"} else "ACC000001",
            next_charge_date=date(y, m, last_day).isoformat(),
            expires_at=date(y, m, last_day).isoformat(),
            last_charge_date=_ts(today.year, today.month, min(day, 28))[:10],
            status="active",
            auto_renew=True,
            auto_detected=True,
            price_history=history,
        )
        _mark_demo(sub, store)
        store.subscriptions[sub.sub_id] = sub
    store.counters["subscriptions"] = len(SUBSCRIPTION_CATALOG)


def _add_transactions(store) -> None:
    """生成最近 12 个月流水（让月度/年度账单都有真实的分析对象），并埋入若干可疑交易。"""
    rng = random.Random(RNG_SEED)
    today = store.clock.today()
    account_id = "ACC000001"
    rows = []

    def push(y, m, d, amount, merchant, category, channel, **extra):
        if date(y, m, d) > today:
            return
        rows.append((y, m, d, amount, merchant, category, channel, extra))

    for (y, m) in _months_back(today, 12):
        last_day = min(calendar.monthrange(y, m)[1], 28)

        # 固定支出
        push(y, m, 5, -3800.0, "张建国（房租）", "住房", "转账", counterparty="张建国", note="10月房租")
        push(y, m, 10, -3200.0, "信用卡还款", "还款", "APP", note="信用卡自动还款")
        push(y, m, 25, 18500.0, "工资代发", "收入", "转账", note="月度工资")
        push(y, m, 3, -120.0, "中国移动话费", "生活缴费", "代扣")

        # 日常消费
        for _ in range(16):
            merchant, lo, hi, _w = rng.choices(MERCHANTS["餐饮"], weights=[x[3] for x in MERCHANTS["餐饮"]])[0]
            push(y, m, rng.randint(1, last_day), -round(rng.uniform(lo, hi), 2), merchant, "餐饮", "扫码")
        for _ in range(10):
            merchant, lo, hi, _w = rng.choices(MERCHANTS["交通"], weights=[x[3] for x in MERCHANTS["交通"]])[0]
            push(y, m, rng.randint(1, last_day), -round(rng.uniform(lo, hi), 2), merchant, "交通", "扫码")
        for _ in range(5):
            merchant, lo, hi, _w = rng.choices(MERCHANTS["购物"], weights=[x[3] for x in MERCHANTS["购物"]])[0]
            push(y, m, rng.randint(1, last_day), -round(rng.uniform(lo, hi), 2), merchant, "购物", "APP")
        for _ in range(3):
            merchant, lo, hi, _w = rng.choices(MERCHANTS["生活缴费"], weights=[x[3] for x in MERCHANTS["生活缴费"]])[0]
            push(y, m, rng.randint(1, last_day), -round(rng.uniform(lo, hi), 2), merchant, "生活缴费", "代扣")
        for _ in range(3):
            merchant, lo, hi, _w = rng.choices(MERCHANTS["娱乐"], weights=[x[3] for x in MERCHANTS["娱乐"]])[0]
            push(y, m, rng.randint(1, last_day), -round(rng.uniform(lo, hi), 2), merchant, "娱乐", "APP")
        if rng.random() < 0.6:
            merchant, lo, hi, _w = rng.choices(MERCHANTS["医疗"], weights=[x[3] for x in MERCHANTS["医疗"]])[0]
            push(y, m, rng.randint(1, last_day), -round(rng.uniform(lo, hi), 2), merchant, "医疗", "扫码")
        if rng.random() < 0.4:
            merchant, lo, hi, _w = rng.choices(MERCHANTS["教育"], weights=[x[3] for x in MERCHANTS["教育"]])[0]
            push(y, m, rng.randint(1, last_day), -round(rng.uniform(lo, hi), 2), merchant, "教育", "APP")

        # 订阅代扣（带涨价的服务只在最近一期按新价格扣费，方便演示涨价识别）
        for merchant, amount, day, _cat, new_amount in SUBSCRIPTION_CATALOG:
            if (y, m) == (today.year, today.month) and day > today.day:
                continue
            charge = new_amount if (new_amount and (y, m) == (today.year, today.month)) else amount
            push(y, m, day, -charge, merchant, "订阅服务", "代扣",
                 note="自动续费", is_subscription=True)

        # 转账往来
        push(y, m, rng.randint(1, last_day), -2000.0, "陈妈妈", "转账", "转账",
             counterparty="陈妈妈", note="给妈妈的生活费")
        push(y, m, rng.randint(1, last_day), -round(rng.uniform(80, 320), 2), "王强", "转账", "转账",
             counterparty="王强", note="聚餐AA")
        push(y, m, rng.randint(1, last_day), 500.0, "李思琪", "转账", "转账",
             counterparty="李思琪", note="还我上次垫付")

        # 理财申赎
        push(y, m, rng.randint(1, last_day), -3000.0, "微众活期+", "理财", "APP",
             note="基金申购")

    # ------------------------------------------------------------------
    # 埋点：4 类可疑交易（用于异常交易识别演示）
    # ------------------------------------------------------------------
    # 埋点 1：深夜大额消费（凌晨 03:12 刷卡 8888 元）
    d1 = today.replace(day=max(today.day - 5, 1))
    push(d1.year, d1.month, d1.day, -8888.0, "深圳华强北数码城", "购物", "刷卡",
         hour=3, minute=12, tags=["night", "high_amount"])

    # 埋点 2：重复扣款（同一商户 1 分钟内被扣两次）
    d2 = today.replace(day=max(today.day - 9, 1))
    push(d2.year, d2.month, d2.day, -68.0, "木屋烧烤", "餐饮", "扫码",
         hour=12, minute=30, note="桌号A17")
    push(d2.year, d2.month, d2.day, -68.0, "木屋烧烤", "餐饮", "扫码",
         hour=12, minute=31, note="桌号A17", tags=["possible_duplicate"])

    # 埋点 3：金额明显偏离同类别的消费
    d3 = today.replace(day=max(today.day - 7, 1))
    push(d3.year, d3.month, d3.day, -2880.0, "深圳湾云端会所", "餐饮", "刷卡",
         hour=20, minute=40, tags=["amount_outlier"])

    # 埋点 4：境外消费（该卡默认未开通境外交易）
    d4 = today.replace(day=max(today.day - 16, 1))
    push(d4.year, d4.month, d4.day, -3200.0, "DFS免税店（境外）", "购物", "刷卡",
         tags=["overseas"])

    # 埋点 5：1 小时内多笔小额扣款（盗刷前的试探性消费）
    d5 = today.replace(day=max(today.day - 3, 1))
    for offset, (merchant, amount) in enumerate(
        [("快付通-游戏充值", 15.0), ("云闪付-便利店", 12.0), ("某出行App", 18.0), ("未知商户", 9.0)]
    ):
        push(d5.year, d5.month, d5.day, -amount, merchant, "其他", "免密支付",
             hour=2, minute=5 + offset * 9, tags=["micro_burst"])

    # ------------------------------------------------------------------
    # 写入 store
    # ------------------------------------------------------------------
    for (y, m, d, amount, merchant, category, channel, extra) in rows:
        hour = extra.pop("hour", rng.randint(7, 22))
        minute = extra.pop("minute", rng.randint(0, 59))
        # 今天还没到的时间点不要出现在账单里，否则"最新一笔流水"会指向未来
        if date(y, m, d) == today:
            now = store.clock.now()
            if hour > now.hour:
                hour, minute = max(now.hour, 0), min(minute, 59)
            elif hour == now.hour:
                minute = min(minute, now.minute)
        txn = store.add_transaction(
            user_id="u_001",
            # 订阅代扣走信用卡，其余流水都走活期账户
            account_id="ACC000003" if category == "订阅服务" else account_id,
            amount=amount,
            merchant=merchant,
            category=category,
            channel=channel,
            timestamp=_ts(y, m, d, hour, minute),
            **extra,
        )
        _mark_demo(txn, store)
        if txn.is_subscription:
            sub = next(
                (s for s in store.subscriptions.values() if s.merchant == merchant),
                None,
            )
            if sub:
                txn.subscription_id = sub.sub_id
                sub.last_charge_date = txn.timestamp[:10]


def _add_goals(store) -> None:
    """预置一个进行中的旅行基金目标，让跨场景联动有上下文。"""
    store.add_goal(
        user_id="u_001",
        name="国庆旅行基金",
        target_amount=8000.0,
        deadline="2026-09-25",
        scenario="travel",
        account_id="ACC000001",
        note="与老婆去云南",
    )
    goal = store.goals_of("u_001")[0]
    goal.locked_amount = 8000.0
    goal.status = "achieved"
    store.accounts["ACC000001"].locked = 0.0


def build_seed(store) -> None:
    """入口：把全部模拟数据装进 DataStore。"""
    _add_users(store)
    _add_accounts(store)
    _add_cards(store)
    _add_payees(store)
    _add_products(store)
    _add_holdings(store)
    _add_merchant_offers(store)
    _add_subscriptions(store)
    _add_transactions(store)
    _add_goals(store)

