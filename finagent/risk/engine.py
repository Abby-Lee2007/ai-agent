"""风控引擎。

围绕赛事要求里的"基础金融风控校验"实现四件事：

1. 限额校验：单笔上限、单日累计上限、新收款人上限；
2. 二次确认：超过阈值或高风险操作，先让用户确认再执行；
3. 身份校验：大额操作模拟短信验证码验证；
4. 异常交易识别：金额离群、深夜大额、重复扣款、订阅涨价、境外消费等。

所有判断都会返回"原因"，这样 Agent 可以自然地解释给用户听，
而不是简单丢一句"操作失败"。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from config import RiskPolicy
from finagent.data.models import Account, Payee, Transaction, User
from finagent.data.providers import DataProviderRegistry, build_data_providers
from finagent.data.store import Clock, DataStore


@dataclass
class RiskDecision:
    """一次风控评估的结论。"""

    level: str = "low"                     # low / medium / high / block
    reasons: List[str] = field(default_factory=list)
    requires_confirmation: bool = False
    requires_verification: bool = False
    blocked: bool = False

    def summary(self) -> str:
        if not self.reasons:
            return "风控校验通过"
        return "；".join(self.reasons)


@dataclass
class Anomaly:
    """一笔被标记为可疑的交易。"""

    txn_id: str
    date: str
    merchant: str
    amount: float
    category: str
    kind: str
    level: str                             # info / warning / danger
    reason: str
    suggestion: str = ""
    score: float = 0.0                     # 0-100 风险分
    metrics: Dict[str, Any] = field(default_factory=dict)


class RiskEngine:
    """风控规则引擎。规则集中在 `check_*` 方法里，方便后续增删。"""

    # 危险等级工具：即使金额很小也要二次确认
    HIGH_RISK_TOOLS = {
        "card.report_lost": "挂失属于不可逆操作，补卡后卡号会变更",
        "card.freeze": "冻结卡片会影响所有交易",
        "wealth.subscribe": "理财申购在确认后不可撤单",
        "wealth.redeem": "赎回后资金将回到活期账户",
        "subscription.cancel": "取消订阅后权益立即失效",
        "subscription.resume": "恢复后将在每个订阅周期继续自动扣款",
        "membership.purchase": "会员充值确认后将合并扣款且不可撤销",
    }

    # 中等风险工具：需要二次确认但原因由规则给出
    MEDIUM_RISK_TOOLS = {
        "card.limit_adjust": "调整额度会即刻生效",
        "card.control": "交易开关会即刻生效",
        "card.repay": "信用卡还款会立即扣减付款卡余额",
        "transfer.split_aa": "将向多人发起收款",
        "crossscene.birthday_plan": "将锁定资金并预约下单",
        "goal.create": "将锁定活期资金",
        "subscription.change_cycle": "将改变当前订阅到期后的续费周期和价格",
    }

    # 只有这些工具会真正动到资金，金额阈值才对它们生效
    # （理财推荐里的 amount 只是配置建议，不该触发二次确认和短信验证）
    AMOUNT_SENSITIVE_TOOLS = {
        "transfer.send",
        "transfer.schedule",
        "wealth.subscribe",
        "wealth.redeem",
        "goal.create",
        # 额度调整虽然不动资金，但改动额度过大同样属于敏感操作
        "card.limit_adjust",
        "card.repay",
    }

    def __init__(
        self,
        store: DataStore,
        clock: Clock,
        providers: Optional[DataProviderRegistry] = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.providers = providers or build_data_providers(store)

    # ==================================================================
    # 一、转账风控
    # ==================================================================
    def check_transfer(
        self,
        user: User,
        account: Optional[Account],
        payee: Optional[Payee],
        payee_keyword: str,
        amount: float,
        when: Optional[str] = None,
    ) -> RiskDecision:
        decision = RiskDecision()
        if amount <= 0:
            decision.blocked = True
            decision.level = "block"
            decision.reasons.append("转账金额必须大于 0")
            return decision

        if account is None:
            decision.blocked = True
            decision.level = "block"
            decision.reasons.append("未找到可用的付款账户")
            return decision

        # --- 限额校验（先看限额，让"超额"这类风险提示优先于余额提示）---
        if amount > RiskPolicy.SINGLE_TRANSFER_LIMIT:
            decision.blocked = True
            decision.level = "block"
            decision.reasons.append(
                f"超过单笔转账限额 {RiskPolicy.SINGLE_TRANSFER_LIMIT:,.0f} 元"
            )
            return decision

        today_total = self.daily_transfer_total(user.user_id)
        if today_total + amount > RiskPolicy.DAILY_TRANSFER_LIMIT:
            decision.blocked = True
            decision.level = "block"
            decision.reasons.append(
                f"超过单日累计转账限额 {RiskPolicy.DAILY_TRANSFER_LIMIT:,.0f} 元"
                f"（今日已转出 {today_total:,.2f} 元）"
            )
            return decision

        is_new_payee = payee is None
        if is_new_payee and amount > RiskPolicy.NEW_PAYEE_LIMIT:
            decision.blocked = True
            decision.level = "block"
            decision.reasons.append(
                f"首次向「{payee_keyword}」转账，单笔不得超过 "
                f"{RiskPolicy.NEW_PAYEE_LIMIT:,.0f} 元，请先小额验证或改用已保存的收款人"
            )
            return decision

        if account.kind != "credit" and account.available < amount:
            decision.blocked = True
            decision.level = "block"
            decision.reasons.append(
                f"账户可用余额不足（可用 {account.available:,.2f} 元，需转出 {amount:,.2f} 元）"
            )
            return decision

        # --- 二次确认 ---
        # 常用收款人（通讯录里已有）门槛更宽松，新收款人或定时转账从严
        threshold = RiskPolicy.CONFIRM_THRESHOLD if is_new_payee else RiskPolicy.TRUSTED_CONFIRM_THRESHOLD
        if amount >= threshold:
            decision.requires_confirmation = True
            decision.level = "medium"
            decision.reasons.append(
                f"金额 {amount:,.2f} 元超过 {threshold:,.0f} 元，需要二次确认"
            )
        if is_new_payee:
            decision.requires_confirmation = True
            decision.level = max(decision.level, "medium", key=["low", "medium", "high", "block"].index)
            decision.reasons.append(f"「{payee_keyword}」是首次转账的新收款人")

        # --- 身份验证 ---
        if amount >= RiskPolicy.VERIFY_THRESHOLD:
            decision.requires_verification = True
            decision.level = "high"
            decision.reasons.append(
                f"金额 {amount:,.2f} 元超过 {RiskPolicy.VERIFY_THRESHOLD:,.0f} 元，需要短信验证码校验身份"
            )

        # --- 行为特征 ---
        hour = self.clock.now().hour
        if RiskPolicy.NIGHT_START_HOUR <= hour < RiskPolicy.NIGHT_END_HOUR and amount >= 2000:
            decision.requires_confirmation = True
            decision.level = "high"
            decision.reasons.append("当前处于 00:00-05:00 夜间时段且金额较大，属于高风险时段")

        recent = self._recent_transfers(user.user_id, RiskPolicy.RAPID_TRANSFER_WINDOW_MINUTES)
        if len(recent) >= RiskPolicy.RAPID_TRANSFER_COUNT:
            decision.requires_confirmation = True
            decision.level = "high"
            decision.reasons.append(
                f"{RiskPolicy.RAPID_TRANSFER_WINDOW_MINUTES} 分钟内已连续转出 "
                f"{len(recent)} 笔，疑似被他人操控，请确认是本人操作"
            )

        if not decision.requires_confirmation and not decision.requires_verification:
            if account.kind == "credit":
                decision.requires_confirmation = True
                decision.reasons.append("从信用卡账户转出可能产生手续费和利息")
        return decision

    def daily_transfer_total(self, user_id: str, day: Optional[str] = None) -> float:
        """当日已转出金额（转账 + AA 收款）。"""
        target = day or self.clock.date_str()
        total = 0.0
        for txn in self.providers.transactions.of(user_id, target, target):
            if txn.amount < 0 and txn.category in {"转账", "AA收款"}:
                total += abs(txn.amount)
        return round(total, 2)

    def _recent_transfers(self, user_id: str, minutes: int) -> List[Transaction]:
        since = (self.clock.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")
        rows = self.providers.transactions.of(user_id, since)
        return [t for t in rows if t.amount < 0 and t.category in {"转账", "AA收款"}]

    # ==================================================================
    # 二、通用业务操作风控（非转账类工具）
    # ==================================================================
    def check_operation(
        self,
        tool_name: str,
        args: Dict[str, Any],
        user: User,
        account: Optional[Account] = None,
    ) -> RiskDecision:
        decision = RiskDecision()
        amount = self._extract_amount(args)

        if tool_name in self.HIGH_RISK_TOOLS:
            decision.level = "high"
            decision.requires_confirmation = True
            decision.reasons.append(self.HIGH_RISK_TOOLS[tool_name])
        elif tool_name in self.MEDIUM_RISK_TOOLS:
            decision.level = "medium"
            decision.requires_confirmation = True
            decision.reasons.append(self.MEDIUM_RISK_TOOLS[tool_name])

        if amount is not None:
            if tool_name not in self.AMOUNT_SENSITIVE_TOOLS:
                return decision
            if account is not None and account.kind != "credit" and account.available < amount:
                decision.blocked = True
                decision.level = "block"
                decision.reasons.append(
                    f"账户可用余额不足（可用 {account.available:,.2f} 元，需要 {amount:,.2f} 元）"
                )
                return decision
            if amount >= RiskPolicy.CONFIRM_THRESHOLD and not decision.requires_confirmation:
                decision.level = "medium"
                decision.requires_confirmation = True
                decision.reasons.append(
                    f"金额 {amount:,.2f} 元超过 {RiskPolicy.CONFIRM_THRESHOLD:,.0f} 元，需要二次确认"
                )
            if amount >= RiskPolicy.VERIFY_THRESHOLD:
                decision.level = "high"
                decision.requires_verification = True
                decision.reasons.append(
                    f"金额 {amount:,.2f} 元超过 {RiskPolicy.VERIFY_THRESHOLD:,.0f} 元，需要短信验证码校验身份"
                )

        # 未实名用户不允许做资金类操作
        if not user.verified and tool_name.split(".")[0] in {"transfer", "wealth"}:
            decision.blocked = True
            decision.level = "block"
            decision.reasons.append("账户尚未完成实名认证，请先完成身份核验")
        return decision

    @staticmethod
    def _extract_amount(args: Dict[str, Any]) -> Optional[float]:
        for key in ("amount", "total_amount", "budget", "new_limit", "principal"):
            value = args.get(key)
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value)
                except ValueError:
                    continue
        return None

    # ==================================================================
    # 三、异常交易识别
    # ==================================================================
    def detect_anomalies(
        self,
        user_id: str,
        txns: Optional[List[Transaction]] = None,
        include_subscriptions: bool = True,
    ) -> List[Anomaly]:
        """返回按严重程度排序的异常交易清单。

        include_subscriptions 用来控制是否连带检查订阅涨价。
        导入一小批账单时应该关掉，否则会把跟这批数据无关的订阅涨价也报出来。
        """
        rows = txns if txns is not None else self.providers.transactions.of(user_id)
        rows = [t for t in rows if t.amount < 0 and t.status == "success"]
        found: List[Anomaly] = []
        found.extend(self._detect_amount_outliers(rows))
        found.extend(self._detect_night_large(rows))
        found.extend(self._detect_duplicates(rows))
        found.extend(self._detect_overseas(rows))
        found.extend(self._detect_micro_bursts(rows))
        found.extend(self._detect_new_merchant_large(rows))
        if include_subscriptions:
            found.extend(self._detect_subscription_hikes(user_id))
        order = {"danger": 0, "warning": 1, "info": 2}
        return sorted(found, key=lambda a: (order.get(a.level, 3), -abs(a.amount)))

    def _detect_amount_outliers(self, rows: List[Transaction]) -> List[Anomaly]:
        """金额离群：同一消费类别内，金额显著高于历史均值 + N 倍标准差。"""
        by_category: Dict[str, List[float]] = {}
        for t in rows:
            by_category.setdefault(t.category, []).append(abs(t.amount))
        result: List[Anomaly] = []
        for category, values in by_category.items():
            if len(values) < 8:
                continue
            mean = statistics.mean(values)
            stdev = statistics.pstdev(values) or 0.0
            threshold = mean + RiskPolicy.ANOMALY_ZSCORE * stdev
            for t in rows:
                if t.category != category:
                    continue
                size = abs(t.amount)
                if size >= 500 and size > threshold and size > mean * 3:
                    result.append(
                        Anomaly(
                            txn_id=t.txn_id,
                            date=t.timestamp[:10],
                            merchant=t.merchant,
                            amount=t.amount,
                            category=t.category,
                            kind="amount_outlier",
                            level="warning",
                            reason=(
                                f"{category}类消费的常见金额约 {mean:,.0f} 元，"
                                f"该笔 {size:,.0f} 元明显偏离历史区间"
                            ),
                            suggestion="确认是否本人消费；若不是，可立即冻结卡片并申请交易争议处理",
                            score=round(min(95.0, 55.0 + max((size - threshold) / (stdev or 1), 0) * 5), 1),
                            metrics={"category_mean": round(mean, 2), "category_stdev": round(stdev, 2),
                                     "threshold": round(threshold, 2),
                                     "zscore": round((size - mean) / (stdev or 1), 2)},
                        )
                    )
        return result

    def _detect_night_large(self, rows: List[Transaction]) -> List[Anomaly]:
        result = []
        for t in rows:
            hour = self._hour_of(t.timestamp)
            if RiskPolicy.NIGHT_START_HOUR <= hour < RiskPolicy.NIGHT_END_HOUR and abs(t.amount) >= 1000:
                result.append(
                    Anomaly(
                        txn_id=t.txn_id,
                        date=t.timestamp[:10],
                        merchant=t.merchant,
                        amount=t.amount,
                        category=t.category,
                        kind="night_large",
                        level="danger",
                        reason=f"{hour:02d}:00 左右的深夜大额支出 {abs(t.amount):,.2f} 元，与日常消费时段不符",
                        suggestion="深夜大额交易是盗刷高发特征，建议核对本人及家人是否为持卡人",
                        score=round(min(95.0, 70.0 + abs(t.amount) / 1000.0 * 5), 1),
                        metrics={"hour": hour, "amount": abs(t.amount)},
                    )
                )
        return result

    def _detect_duplicates(self, rows: List[Transaction]) -> List[Anomaly]:
        """重复扣款：同商户、同金额、时间相近的两笔以上支出。"""
        result: List[Anomaly] = []
        seen: set = set()
        ordered = sorted(rows, key=lambda t: t.timestamp)
        for i, first in enumerate(ordered):
            if first.txn_id in seen:
                continue
            group = [first]
            for second in ordered[i + 1:]:
                gap = self._minutes_between(first.timestamp, second.timestamp)
                if gap is None or gap > RiskPolicy.DUPLICATE_WINDOW_MINUTES:
                    continue
                if second.merchant == first.merchant and abs(abs(second.amount) - abs(first.amount)) < 0.01:
                    group.append(second)
            if len(group) > 1:
                for item in group:
                    seen.add(item.txn_id)
                result.append(
                    Anomaly(
                        txn_id=group[-1].txn_id,
                        date=group[-1].timestamp[:10],
                        merchant=group[0].merchant,
                        amount=group[0].amount,
                        category=group[0].category,
                        kind="duplicate_charge",
                        level="warning",
                        reason=(
                            f"「{group[0].merchant}」在 {RiskPolicy.DUPLICATE_WINDOW_MINUTES} 分钟内"
                            f"被扣款 {len(group)} 次，每笔 {abs(group[0].amount):,.2f} 元，疑似重复扣款"
                        ),
                        suggestion="可联系商户核实并申请退回多扣款项，或通过异议交易通道发起调单",
                        score=70.0,
                        metrics={"window_minutes": RiskPolicy.DUPLICATE_WINDOW_MINUTES,
                                 "count": len(group), "unit_amount": abs(group[0].amount)},
                    )
                )
        return result

    def _detect_overseas(self, rows: List[Transaction]) -> List[Anomaly]:
        result = []
        for t in rows:
            text = f"{t.merchant}{t.note}"
            overseas = "overseas" in t.tags or any(k in text for k in ("境外", "海外", "免税店", "DFS"))
            if overseas:
                result.append(
                    Anomaly(
                        txn_id=t.txn_id,
                        date=t.timestamp[:10],
                        merchant=t.merchant,
                        amount=t.amount,
                        category=t.category,
                        kind="overseas",
                        level="info",
                        reason=f"检测到境外商户消费 {abs(t.amount):,.2f} 元，请确认是否为本人出境使用",
                        suggestion="如非本人出境消费，建议关闭境外交易开关并冻结卡片",
                        score=45.0,
                        metrics={"amount": abs(t.amount)},
                    )
                )
        return result

    def _detect_micro_bursts(self, rows: List[Transaction]) -> List[Anomaly]:
        """高频小额试探：短时间内多笔小额支出，常见于盗刷前的试探性消费。"""
        result: List[Anomaly] = []
        ordered = sorted(rows, key=lambda t: t.timestamp)
        i = 0
        while i < len(ordered):
            window = [ordered[i]]
            j = i + 1
            while j < len(ordered):
                gap = self._minutes_between(ordered[i].timestamp, ordered[j].timestamp)
                if gap is None or gap > 60:
                    break
                if abs(ordered[j].amount) <= 20:
                    window.append(ordered[j])
                j += 1
            small = [t for t in window if abs(t.amount) <= 20]
            if len(small) >= 4:
                total = sum(abs(t.amount) for t in small)
                result.append(
                    Anomaly(
                        txn_id=small[-1].txn_id,
                        date=small[-1].timestamp[:10],
                        merchant=f"{small[0].merchant} 等 {len({t.merchant for t in small})} 个商户",
                        amount=-total,
                        category=small[0].category,
                        kind="micro_burst",
                        level="warning",
                        reason=f"1 小时内出现 {len(small)} 笔 20 元以下小额扣款，合计 {total:,.2f} 元，疑似试探性盗刷",
                        suggestion="建议先冻结卡片，再逐笔确认",
                        score=60.0,
                        metrics={"window_minutes": 60, "count": len(small), "total": round(total, 2)},
                    )
                )
                i = j
                continue
            i += 1
        return result

    def _detect_new_merchant_large(self, rows: List[Transaction]) -> List[Anomaly]:
        """首次出现的大额商户：历史从未在这家商户消费过。"""
        ordered = sorted(rows, key=lambda t: t.timestamp)
        seen: set = set()
        result: List[Anomaly] = []
        skip_categories = {"还款", "住房", "理财", "转账", "收入"}
        for t in ordered:
            if t.merchant not in seen:
                seen.add(t.merchant)
                if t.category in skip_categories:
                    continue
                if abs(t.amount) >= 5000:
                    result.append(
                        Anomaly(
                            txn_id=t.txn_id,
                            date=t.timestamp[:10],
                            merchant=t.merchant,
                            amount=t.amount,
                            category=t.category,
                            kind="new_merchant_large",
                            level="warning",
                            reason=f"首次在「{t.merchant}」消费即达到 {abs(t.amount):,.2f} 元",
                            suggestion="首次交易大额商户属于典型风险特征，建议确认是否本人操作",
                            score=round(min(90.0, 50.0 + abs(t.amount) / 1000.0 * 3), 1),
                            metrics={"amount": abs(t.amount), "first_seen": True},
                        )
                    )
        return result

    def _detect_subscription_hikes(self, user_id: str) -> List[Anomaly]:
        result: List[Anomaly] = []
        for sub in self.providers.subscriptions.of(user_id, "active"):
            history = [x for x in sub.price_history if x > 0]
            if len(history) < 2:
                continue
            previous = max(history[:-1])
            if sub.amount > previous * 1.2:
                increase = (sub.amount - previous) / previous * 100
                result.append(
                    Anomaly(
                        txn_id=sub.sub_id,
                        date=sub.last_charge_date or sub.next_charge_date,
                        merchant=sub.merchant,
                        amount=-sub.amount,
                        category="订阅服务",
                        kind="subscription_hike",
                        level="warning",
                        reason=(
                            f"「{sub.merchant}」月费由 {previous:,.2f} 元涨至 {sub.amount:,.2f} 元"
                            f"（涨幅 {increase:.0f}%）"
                        ),
                        suggestion="若已不需要该服务，可以让 Agent 一键取消订阅",
                        score=round(min(75.0, 40.0 + increase / 2), 1),
                        metrics={"previous_amount": previous, "current_amount": sub.amount,
                                 "increase_pct": round(increase, 1)},
                    )
                )
        return result

    # ------------------------------------------------------------------
    # 工具函数
    # ------------------------------------------------------------------
    @staticmethod
    def _hour_of(timestamp: str) -> int:
        try:
            return datetime.fromisoformat(timestamp).hour
        except ValueError:
            return 12

    @staticmethod
    def _minutes_between(a: str, b: str) -> Optional[float]:
        try:
            delta = abs((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds())
        except ValueError:
            return None
        return delta / 60.0

