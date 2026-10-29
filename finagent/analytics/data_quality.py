"""Unified data-quality metrics for financial personalization.

The score is deliberately bounded and componentized so a high average cannot
hide a missing critical identifier. The OCR receipt score is a separate model.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence


QUALITY_VERSION = "data-quality-v1"
DOMAIN_WEIGHTS = {
    "transactions": 0.30,
    "cards": 0.20,
    "subscriptions": 0.10,
    "products": 0.25,
    "holdings": 0.15,
}
COMPONENT_WEIGHTS = {
    "completeness": 0.45,
    "freshness": 0.20,
    "anomaly": 0.15,
    "coverage": 0.20,
}
METRIC_DEFINITIONS = {
    "missing_rate": "缺失的必填字段数 / 必填字段总数，范围 0-1",
    "anomaly_rate": "命中域内异常规则的记录数 / 总记录数，范围 0-1",
    "freshness": "最新更新时间相对阈值的线性时效分，范围 0-1",
    "coverage": "实际样本数或时间桶 / 期望样本数或时间桶，封顶 1",
    "score": (
        "100 × (0.45×完整率 + 0.20×时效 + "
        "0.15×(1-异常率) + 0.20×覆盖率)"
    ),
}


def composite_quality_score(
    completeness: float,
    freshness: float,
    anomaly: float,
    coverage: float,
) -> float:
    """Calculate the public quality formula from normalized components."""
    values = {
        "completeness": max(0.0, min(float(completeness), 1.0)),
        "freshness": max(0.0, min(float(freshness), 1.0)),
        "anomaly": max(0.0, min(float(anomaly), 1.0)),
        "coverage": max(0.0, min(float(coverage), 1.0)),
    }
    score = 100.0 * (
        COMPONENT_WEIGHTS["completeness"] * values["completeness"]
        + COMPONENT_WEIGHTS["freshness"] * values["freshness"]
        + COMPONENT_WEIGHTS["anomaly"] * (1.0 - values["anomaly"])
        + COMPONENT_WEIGHTS["coverage"] * values["coverage"]
    )
    return round(score, 2)


def _parse_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:19])
    except ValueError:
        return None


def _freshness_ratio(
    latest: Optional[datetime],
    now: datetime,
    days: int,
) -> float:
    if latest is None:
        return 0.0
    age_days = max((now - latest).total_seconds() / 86400, 0.0)
    if age_days <= days:
        return 1.0
    if age_days >= days * 2:
        return 0.0
    return round(1.0 - (age_days - days) / max(days, 1), 4)


def _assess_domain(
    name: str,
    records: Sequence[Any],
    *,
    required_fields: Sequence[str],
    timestamp_getter: Callable[[Any], Any],
    now: datetime,
    freshness_days: int,
    coverage: float,
    anomaly_checker: Callable[[Any], List[str]],
    critical: bool,
) -> Dict[str, Any]:
    total = len(records)
    required_total = total * len(required_fields)
    missing_count = 0
    anomalies: List[str] = []
    timestamps: List[datetime] = []
    for record in records:
        for field in required_fields:
            value = (
                record.get(field)
                if isinstance(record, dict)
                else getattr(record, field, None)
            )
            if value is None or str(value).strip() == "":
                missing_count += 1
        parsed = _parse_datetime(timestamp_getter(record))
        if parsed is not None:
            timestamps.append(parsed)
        anomalies.extend(anomaly_checker(record))

    missing_rate = (
        missing_count / required_total if required_total else 0.0
    )
    anomaly_rate = len(anomalies) / total if total else 0.0
    latest = max(timestamps) if timestamps else None
    freshness = _freshness_ratio(latest, now, freshness_days)
    coverage_rate = max(0.0, min(float(coverage), 1.0))
    completeness = 1.0 - missing_rate if total else 0.0
    score = composite_quality_score(
        completeness,
        freshness,
        anomaly_rate,
        coverage_rate,
    )
    critical_issue = ""
    if total == 0:
        critical_issue = "没有可用样本"
    elif missing_rate > 0.20:
        critical_issue = "关键字段缺失率超过 20%"
    elif anomaly_rate > 0.30:
        critical_issue = "异常记录占比超过 30%"
    elif critical and freshness < 0.25:
        critical_issue = "关键数据已明显超过时效阈值"
    elif critical and coverage_rate < 0.50:
        critical_issue = "关键数据覆盖率低于 50%"
    if critical and critical_issue:
        score = min(score, 49.0)
    status = (
        "blocked"
        if critical and critical_issue
        else ("warning" if score < 70 or critical_issue else "good")
    )
    return {
        "domain": name,
        "record_count": total,
        "required_field_count": required_total,
        "missing_field_count": missing_count,
        "missing_rate": round(missing_rate, 4),
        "anomaly_count": len(anomalies),
        "anomaly_rate": round(anomaly_rate, 4),
        "anomaly_samples": anomalies[:5],
        "latest_update": latest.isoformat(timespec="seconds") if latest else "",
        "freshness_days_threshold": freshness_days,
        "freshness": round(freshness, 4),
        "coverage": round(coverage_rate, 4),
        "completeness": round(completeness, 4),
        "score": round(score, 2),
        "status": status,
        "critical": critical,
        "critical_issue": critical_issue,
        "weight": DOMAIN_WEIGHTS[name],
    }


def _transaction_anomalies(txn: Any) -> List[str]:
    issues: List[str] = []
    if not _parse_datetime(getattr(txn, "timestamp", "")):
        issues.append(f"{getattr(txn, 'txn_id', '')}: 时间格式无效")
    if not isinstance(getattr(txn, "amount", None), (int, float)):
        issues.append(f"{getattr(txn, 'txn_id', '')}: 金额不是数值")
    elif abs(float(txn.amount)) > 10_000_000:
        issues.append(f"{getattr(txn, 'txn_id', '')}: 金额极端")
    return issues


def _card_anomalies(card: Any) -> List[str]:
    issues: List[str] = []
    if getattr(card, "kind", "") not in {
        "debit", "credit", "virtual", "supplementary",
    }:
        issues.append(f"{getattr(card, 'card_id', '')}: 卡片类型未知")
    if float(getattr(card, "balance", 0.0) or 0.0) < 0:
        issues.append(f"{getattr(card, 'card_id', '')}: 借记卡余额为负")
    if float(getattr(card, "used", 0.0) or 0.0) < 0:
        issues.append(f"{getattr(card, 'card_id', '')}: 已用额度为负")
    return issues


def _subscription_anomalies(subscription: Any) -> List[str]:
    issues: List[str] = []
    if _parse_datetime(getattr(subscription, "next_charge_date", "")) is None:
        issues.append(f"{getattr(subscription, 'sub_id', '')}: 下次扣款日期无效")
    if float(getattr(subscription, "amount", 0.0) or 0.0) < 0:
        issues.append(f"{getattr(subscription, 'sub_id', '')}: 订阅金额为负")
    return issues


def _product_anomalies(product: Any) -> List[str]:
    issues: List[str] = []
    if not 1 <= int(getattr(product, "risk_level", 0) or 0) <= 5:
        issues.append(f"{getattr(product, 'product_id', '')}: 风险等级越界")
    if float(getattr(product, "expected_return", 0.0) or 0.0) < -1:
        issues.append(f"{getattr(product, 'product_id', '')}: 收益率异常")
    if float(getattr(product, "min_amount", 0.0) or 0.0) < 0:
        issues.append(f"{getattr(product, 'product_id', '')}: 起投金额为负")
    return issues


def _holding_anomalies(holding: Any, product_ids: Iterable[str]) -> List[str]:
    issues: List[str] = []
    product_id = str(getattr(holding, "product_id", "") or "")
    if product_id not in set(product_ids):
        issues.append(f"{getattr(holding, 'holding_id', '')}: 产品不存在")
    if float(getattr(holding, "principal", 0.0) or 0.0) < 0:
        issues.append(f"{getattr(holding, 'holding_id', '')}: 本金为负")
    if _parse_datetime(getattr(holding, "purchase_date", "")) is None:
        issues.append(f"{getattr(holding, 'holding_id', '')}: 购买日期无效")
    return issues


def build_data_quality(
    store: Any,
    providers: Any,
    clock: Any,
    user_id: str,
) -> Dict[str, Any]:
    """Build domain scores and an execution gate from the current snapshot."""
    now = clock.now()
    transactions = store.transactions_of(user_id)
    cards = list(providers.cards.of(user_id))
    subscriptions = list(providers.subscriptions.of(user_id))
    products = list(providers.wealth.products())
    holdings = list(providers.wealth.holdings(user_id, "holding"))

    txn_months = {
        str(getattr(txn, "timestamp", ""))[:7]
        for txn in transactions
        if _parse_datetime(getattr(txn, "timestamp", ""))
    }
    txn_coverage = len(txn_months) / 6.0
    card_coverage = (
        sum(1 for card in cards if getattr(card, "account_id", ""))
        / max(len(cards), 1)
    )
    subscription_coverage = (
        sum(
            1 for subscription in subscriptions
            if _parse_datetime(getattr(subscription, "next_charge_date", ""))
        )
        / max(len(subscriptions), 1)
    )
    product_coverage = min(len(products) / 5.0, 1.0)
    product_ids = {getattr(product, "product_id", "") for product in products}
    holding_coverage = (
        sum(
            1 for holding in holdings
            if getattr(holding, "product_id", "") in product_ids
        )
        / max(len(holdings), 1)
    )

    domains = {
        "transactions": _assess_domain(
            "transactions",
            transactions,
            required_fields=("timestamp", "account_id", "amount", "merchant"),
            timestamp_getter=lambda item: getattr(item, "timestamp", ""),
            now=now,
            freshness_days=45,
            coverage=txn_coverage,
            anomaly_checker=_transaction_anomalies,
            critical=True,
        ),
        "cards": _assess_domain(
            "cards",
            cards,
            required_fields=("card_id", "kind", "status", "account_id"),
            timestamp_getter=lambda item: getattr(item, "updated_at", ""),
            now=now,
            freshness_days=180,
            coverage=card_coverage,
            anomaly_checker=_card_anomalies,
            critical=True,
        ),
        "subscriptions": _assess_domain(
            "subscriptions",
            subscriptions,
            required_fields=(
                "sub_id", "merchant", "amount", "next_charge_date", "status",
            ),
            timestamp_getter=lambda item: getattr(item, "updated_at", ""),
            now=now,
            freshness_days=90,
            coverage=subscription_coverage,
            anomaly_checker=_subscription_anomalies,
            critical=False,
        ),
        "products": _assess_domain(
            "products",
            products,
            required_fields=(
                "product_id", "name", "category", "risk_level", "asset_class",
            ),
            timestamp_getter=lambda item: getattr(item, "updated_at", ""),
            now=now,
            freshness_days=90,
            coverage=product_coverage,
            anomaly_checker=_product_anomalies,
            critical=True,
        ),
        "holdings": _assess_domain(
            "holdings",
            holdings,
            required_fields=("holding_id", "product_id", "principal", "purchase_date"),
            timestamp_getter=lambda item: getattr(item, "purchase_date", ""),
            now=now,
            freshness_days=3650,
            coverage=holding_coverage,
            anomaly_checker=lambda item: _holding_anomalies(item, product_ids),
            critical=False,
        ),
    }
    weighted_score = round(sum(
        domain["score"] * DOMAIN_WEIGHTS[name]
        for name, domain in domains.items()
    ), 2)
    critical_issues = [
        f"{name}: {domain['critical_issue']}"
        for name, domain in domains.items()
        if domain["critical"] and domain["critical_issue"]
    ]
    if critical_issues:
        weighted_score = min(weighted_score, 49.0)
    blocked = bool(critical_issues)
    personalization_allowed = not blocked and weighted_score >= 70
    grade = (
        "不足"
        if weighted_score < 60
        else ("需关注" if weighted_score < 80 else "可用")
    )
    return {
        "version": QUALITY_VERSION,
        "metric_definitions": dict(METRIC_DEFINITIONS),
        "component_weights": dict(COMPONENT_WEIGHTS),
        "domain_weights": dict(DOMAIN_WEIGHTS),
        "domains": domains,
        "overall": {
            "score": weighted_score,
            "grade": grade,
            "critical_issue_count": len(critical_issues),
            "personalization_allowed": personalization_allowed,
            "execution_blocked": blocked,
        },
        "critical_issues": critical_issues,
        "block_reasons": critical_issues if blocked else [],
        "is_production_data_quality": False,
        "limitations": [
            "该分数衡量当前演示快照的字段完整、时效、异常与覆盖情况，不是真实银行数据证明。",
            "OCR 置信度与账单识别质量是独立字段，不参与本综合分。",
        ],
    }
