"""Unified data-quality metrics for financial decision inputs."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List

from finagent.data.store import Clock, DataStore


METRIC_DICTIONARY: Dict[str, Dict[str, Any]] = {
    "transactions.sample_coverage": {
        "definition": "Distinct transaction days divided by the 90-day analysis window.",
        "unit": "ratio",
        "source": "transactions",
        "direction": "higher_is_better",
        "threshold": 0.60,
    },
    "transactions.missing_rate": {
        "definition": "Share of required transaction fields that are blank.",
        "unit": "ratio",
        "source": "transactions",
        "direction": "lower_is_better",
        "threshold": 0.02,
    },
    "transactions.anomaly_rate": {
        "definition": "Share of invalid, duplicate, or zero-value transactions.",
        "unit": "ratio",
        "source": "transactions",
        "direction": "lower_is_better",
        "threshold": 0.03,
    },
    "cards.missing_rate": {
        "definition": "Share of required card fields that are blank.",
        "unit": "ratio",
        "source": "cards",
        "direction": "lower_is_better",
        "threshold": 0.02,
    },
    "subscriptions.missing_rate": {
        "definition": "Share of required subscription fields that are blank or invalid.",
        "unit": "ratio",
        "source": "subscriptions",
        "direction": "lower_is_better",
        "threshold": 0.05,
    },
    "products.expected_return_coverage": {
        "definition": "Share of products with a positive scenario return or live market symbol.",
        "unit": "ratio",
        "source": "products",
        "direction": "higher_is_better",
        "threshold": 0.90,
    },
    "products.anomaly_rate": {
        "definition": "Share of products with invalid risk levels or missing source metadata.",
        "unit": "ratio",
        "source": "products",
        "direction": "lower_is_better",
        "threshold": 0.02,
    },
    "holdings.missing_rate": {
        "definition": "Share of holdings with missing product references.",
        "unit": "ratio",
        "source": "holdings",
        "direction": "lower_is_better",
        "threshold": 0.02,
    },
}

METRIC_DICTIONARY_VERSION = "2026-10-28"


def _safe_date(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw or "")[:10])
    except ValueError:
        return None


def _safe_timestamp(raw: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 1.0


def _freshness_coverage(updated_at: str, today: date, threshold_days: int) -> float:
    parsed = _safe_date(updated_at)
    if parsed is None:
        return 0.0
    age = max(0, (today - parsed).days)
    return round(max(0.0, 1.0 - age / max(threshold_days, 1)), 4)


def _status(score: float) -> str:
    if score >= 85:
        return "good"
    if score >= 65:
        return "warning"
    return "critical"


def _domain(
    *,
    record_count: int,
    missing_rate: float,
    anomaly_rate: float,
    updated_at: str,
    sample_coverage: float,
    freshness_coverage: float,
    source: str,
) -> Dict[str, Any]:
    score = 100.0 * (
        1.0
        - 0.40 * missing_rate
        - 0.35 * anomaly_rate
        - 0.15 * (1.0 - freshness_coverage)
        - 0.10 * (1.0 - sample_coverage)
    )
    score = round(max(0.0, min(100.0, score)), 1)
    return {
        "record_count": record_count,
        "missing_rate": round(missing_rate, 4),
        "anomaly_rate": round(anomaly_rate, 4),
        "updated_at": updated_at,
        "sample_coverage": round(sample_coverage, 4),
        "freshness_coverage": round(freshness_coverage, 4),
        "score": score,
        "status": _status(score),
        "source": source,
    }


def _missing_ratio(records: Iterable[Any], fields: List[str]) -> float:
    rows = list(records)
    if not rows:
        return 1.0
    missing = 0
    for row in rows:
        for field in fields:
            value = getattr(row, field, None)
            if value is None or value == "" or value == []:
                missing += 1
    return _rate(missing, len(rows) * len(fields))


def _latest_timestamp(records: Iterable[Any], fallback: str) -> str:
    values = [
        str(getattr(row, "updated_at", "") or "")
        for row in records
        if getattr(row, "updated_at", "")
    ]
    values = [value for value in values if _safe_timestamp(value) is not None]
    return max(values) if values else fallback


def build_data_quality_report(
    store: DataStore,
    providers: Any,
    clock: Clock,
    user_id: str,
) -> Dict[str, Any]:
    """Measure completeness, anomalies, freshness, and sample coverage."""
    today = clock.today()
    as_of = today.isoformat()
    start = today - timedelta(days=180)
    transactions = store.transactions_of(user_id, start.isoformat(), as_of)
    cards = providers.cards.of(user_id)
    subscriptions = providers.subscriptions.of(user_id)
    products = list(providers.wealth.products())
    holdings = providers.wealth.holdings(user_id)

    missing_txn = _missing_ratio(
        transactions,
        ["txn_id", "timestamp", "account_id", "merchant", "category"],
    )
    txn_ids = [txn.txn_id for txn in transactions]
    invalid_txns = sum(
        1
        for txn in transactions
        if txn.amount == 0
        or _safe_date(txn.timestamp) is None
        or not txn.category
    )
    duplicate_txns = len(txn_ids) - len(set(txn_ids))
    recent_days = {
        txn.timestamp[:10]
        for txn in transactions
        if _safe_date(txn.timestamp) and _safe_date(txn.timestamp) >= today - timedelta(days=90)
    }
    transaction_domain = _domain(
        record_count=len(transactions),
        missing_rate=missing_txn,
        anomaly_rate=_rate(invalid_txns + duplicate_txns, len(transactions)),
        updated_at=_latest_timestamp(transactions, as_of),
        sample_coverage=_rate(len(recent_days), 90),
        freshness_coverage=_freshness_coverage(
            _latest_timestamp(transactions, as_of),
            today,
            45,
        ),
        source="accounts and imported transaction ledger",
    )

    account_ids = set(store.accounts)
    card_anomalies = sum(
        1
        for card in cards
        if card.account_id and card.account_id not in account_ids
    )
    card_domain = _domain(
        record_count=len(cards),
        missing_rate=_missing_ratio(cards, ["card_id", "account_id", "tail"]),
        anomaly_rate=_rate(card_anomalies, len(cards)),
        updated_at=_latest_timestamp(cards, as_of),
        sample_coverage=_rate(
            sum(1 for card in cards if card.account_id in account_ids),
            len(cards),
        ),
        freshness_coverage=_freshness_coverage(
            _latest_timestamp(cards, as_of),
            today,
            180,
        ),
        source="card provider",
    )

    subscription_missing = sum(
        1
        for sub in subscriptions
        if not sub.next_charge_date or sub.amount <= 0
    )
    subscription_domain = _domain(
        record_count=len(subscriptions),
        missing_rate=_rate(subscription_missing, len(subscriptions)),
        anomaly_rate=_rate(
            sum(1 for sub in subscriptions if sub.amount < 0),
            len(subscriptions),
        ),
        updated_at=_latest_timestamp(subscriptions, as_of),
        sample_coverage=_rate(
            sum(1 for sub in subscriptions if sub.next_charge_date),
            len(subscriptions),
        ),
        freshness_coverage=_freshness_coverage(
            _latest_timestamp(subscriptions, as_of),
            today,
            180,
        ),
        source="subscription provider",
    )

    product_anomalies = sum(
        1
        for product in products
        if product.risk_level not in {1, 2, 3, 4, 5}
        or not product.source
    )
    product_domain = _domain(
        record_count=len(products),
        missing_rate=_missing_ratio(
            products,
            ["product_id", "name", "category", "risk_level", "source"],
        ),
        anomaly_rate=_rate(product_anomalies, len(products)),
        updated_at=_latest_timestamp(products, as_of),
        sample_coverage=_rate(
            sum(
                1
                for product in products
                if product.expected_return > 0 or product.has_live_data
            ),
            len(products),
        ),
        freshness_coverage=_freshness_coverage(
            _latest_timestamp(products, as_of),
            today,
            180,
        ),
        source="wealth product provider",
    )

    product_ids = {product.product_id for product in products}
    holding_anomalies = sum(
        1
        for holding in holdings
        if holding.product_id not in product_ids
    )
    holding_domain = _domain(
        record_count=len(holdings),
        missing_rate=_missing_ratio(
            holdings,
            ["holding_id", "user_id", "product_id"],
        ),
        anomaly_rate=_rate(holding_anomalies, len(holdings)),
        updated_at=_latest_timestamp(holdings, as_of),
        sample_coverage=_rate(
            sum(1 for holding in holdings if holding.product_id in product_ids),
            len(holdings),
        ),
        freshness_coverage=_freshness_coverage(
            _latest_timestamp(holdings, as_of),
            today,
            180,
        ),
        source="wealth holding provider",
    )

    domains = {
        "transactions": transaction_domain,
        "cards": card_domain,
        "subscriptions": subscription_domain,
        "products": product_domain,
        "holdings": holding_domain,
    }
    weights = {
        "transactions": 0.40,
        "cards": 0.15,
        "subscriptions": 0.15,
        "products": 0.20,
        "holdings": 0.10,
    }
    overall_score = round(
        sum(domains[key]["score"] * weights[key] for key in domains),
        1,
    )
    warnings = [
        {
            "domain": key,
            "status": value["status"],
            "message": (
                f"{key} quality score is {value['score']:.1f}; "
                f"missing={value['missing_rate']:.1%}, "
                f"anomaly={value['anomaly_rate']:.1%}, "
                f"coverage={value['sample_coverage']:.1%}"
            ),
        }
        for key, value in domains.items()
        if value["status"] != "good"
    ]
    critical_domains = {
        key
        for key, value in domains.items()
        if value["status"] == "critical"
    }
    blocks_personalization = bool(
        critical_domains & {"transactions", "products", "holdings"}
    )
    return {
        "as_of": as_of,
        "metric_dictionary_version": METRIC_DICTIONARY_VERSION,
        "overall_score": overall_score,
        "status": _status(overall_score),
        "blocks_personalization": blocks_personalization,
        "critical_domains": sorted(critical_domains),
        "domains": domains,
        "warnings": warnings,
    }


def write_quality_report(report: Dict[str, Any], path: str | Path) -> Path:
    """Write a Markdown quality report suitable for evidence review."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Financial Data Quality Report",
        "",
        f"- As of: {report['as_of']}",
        f"- Metric dictionary version: {report['metric_dictionary_version']}",
        f"- Overall score: {report['overall_score']}/100",
        f"- Status: {report['status']}",
        f"- Blocks personalization: {report['blocks_personalization']}",
        "",
        "## Domain Quality",
        "",
        "| Domain | Records | Missing rate | Anomaly rate | Updated at | Sample coverage | Status |",
        "|---|---:|---:|---:|---|---:|---|",
    ]
    for name, domain in report["domains"].items():
        lines.append(
            "| {name} | {records} | {missing:.1%} | {anomaly:.1%} | "
            "{updated} | {coverage:.1%} | {status} |".format(
                name=name,
                records=domain["record_count"],
                missing=domain["missing_rate"],
                anomaly=domain["anomaly_rate"],
                updated=domain["updated_at"],
                coverage=domain["sample_coverage"],
                status=domain["status"],
            )
        )
    lines.extend([
        "",
        "## Warnings",
        "",
    ])
    if report["warnings"]:
        for warning in report["warnings"]:
            lines.append(
                f"- **{warning['domain']}**: {warning['message']}"
            )
    else:
        lines.append("- None")
    lines.extend([
        "",
        "## Metric Dictionary",
        "",
        "| Metric | Definition | Unit | Source | Direction | Threshold |",
        "|---|---|---|---|---|---:|",
    ])
    for key, metric in METRIC_DICTIONARY.items():
        lines.append(
            f"| {key} | {metric['definition']} | {metric['unit']} | "
            f"{metric['source']} | {metric['direction']} | {metric['threshold']} |"
        )
    lines.append("")
    target.write_text("\n".join(lines), encoding="utf-8")
    return target
