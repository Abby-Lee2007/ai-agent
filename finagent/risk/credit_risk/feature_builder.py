"""Read-only availability checks for business credit-risk features."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Optional, Sequence, Set, Tuple

from finagent.data.models import Account, Card, Transaction, User
from finagent.risk.credit_risk.contract import (
    FEATURE_VERSION,
    UCI_FEATURE_ORDER,
    CreditFeatureAvailability,
    CreditFeatureSnapshot,
    CustomerType,
    FeatureDiagnostic,
)


_HISTORICAL_CARD_STATUSES = {"active", "frozen", "lost"}
_MISSING_GENDERS = {"", "unknown", "not_provided", "prefer_not_to_say"}
_REPAYMENT_TAGS = {"credit_card_repayment"}
_REPAYMENT_CATEGORY = "信用卡还款"

# Card.statement_date is a date-only business calendar date (YYYY-MM-DD), with
# no timezone conversion. A statement remains eligible for the current review
# for at most one monthly cycle plus a small allowance. Older data is stale and
# must not be treated as a reliable current statement.
_STATEMENT_DATE_FORMAT = "%Y-%m-%d"
_MAX_STATEMENT_AGE_DAYS = 62


class _DiagnosticCollector:
    def __init__(self) -> None:
        self._counts: dict[tuple[str, str, str], int] = defaultdict(int)

    def add(self, code: str, message: str, feature: str = "") -> None:
        self._counts[(code, message, feature)] += 1

    def as_tuple(self) -> Tuple[FeatureDiagnostic, ...]:
        return tuple(
            FeatureDiagnostic(
                code=code,
                message=message,
                feature=feature,
                count=count,
            )
            for (code, message, feature), count in sorted(
                self._counts.items(),
                key=lambda item: (item[0][0], item[0][2], item[0][1]),
            )
        )


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _month_sequence(as_of: date) -> Tuple[Tuple[int, int], ...]:
    months = []
    year = as_of.year
    month = as_of.month
    for _ in range(6):
        months.append((year, month))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return tuple(months)


def _transaction_month(timestamp: str) -> Tuple[int, int] | None:
    try:
        parsed = datetime.fromisoformat(str(timestamp))
    except (TypeError, ValueError):
        return None
    return parsed.year, parsed.month


def _parse_statement_date(
    value: Any,
    as_of: date,
) -> tuple[date | None, str | None]:
    text = str(value or "").strip()
    if not text:
        return None, None
    try:
        parsed = datetime.strptime(text, _STATEMENT_DATE_FORMAT).date()
    except (TypeError, ValueError):
        return None, "invalid"
    if parsed > as_of:
        return None, "future"
    if (as_of - parsed).days > _MAX_STATEMENT_AGE_DAYS:
        return None, "expired"
    return parsed, None


def _record_invalid_number(
    diagnostics: _DiagnosticCollector,
    *,
    feature: str,
    source: str,
) -> None:
    diagnostics.add(
        "invalid_numeric_field",
        f"{source} could not be interpreted as a finite number.",
        feature,
    )


def build_credit_feature_snapshot(
    user: User,
    cards: Sequence[Card] = (),
    accounts: Sequence[Account] = (),
    transactions: Sequence[Transaction] = (),
    *,
    customer_type: Optional[CustomerType] = None,
    as_of: date | None = None,
    feature_version: str = FEATURE_VERSION,
) -> CreditFeatureSnapshot:
    """Inspect existing business objects without mutating them.

    Customer type is never inferred. Omitting ``customer_type`` records a
    default ``unknown``; passing it explicitly records an explicit source.
    """
    if customer_type is None:
        normalized_customer_type = "unknown"
        customer_type_source = "default_unknown"
    else:
        normalized_customer_type = str(customer_type).strip().lower()
        if normalized_customer_type not in {"new", "existing", "unknown"}:
            raise ValueError("customer_type must be new, existing, or unknown")
        customer_type_source = "explicit_argument"

    diagnostics = _DiagnosticCollector()
    as_of_date = as_of or date.today()

    all_cards_by_id: dict[str, Card] = {}
    owned_cards: list[Card] = []
    for card in cards:
        if card.card_id in all_cards_by_id:
            diagnostics.add(
                "duplicate_card_id",
                "More than one supplied card used the same identifier.",
            )
        all_cards_by_id[card.card_id] = card
        if card.user_id != user.user_id:
            diagnostics.add(
                "card_other_user",
                "A supplied card belonged to another user and was ignored.",
            )
            continue
        owned_cards.append(card)

    owned_accounts: list[Account] = []
    accounts_by_id: dict[str, Account] = {}
    for account in accounts:
        if account.user_id != user.user_id:
            diagnostics.add(
                "account_other_user",
                "A supplied account belonged to another user and was ignored.",
            )
            continue
        accounts_by_id[account.account_id] = account
        owned_accounts.append(account)

    owned_transactions: list[Transaction] = []
    for txn in transactions:
        if txn.user_id != user.user_id:
            diagnostics.add(
                "transaction_other_user",
                "A supplied transaction belonged to another user and was ignored.",
            )
            continue
        owned_transactions.append(txn)

    available: Set[str] = set()
    approximate: Set[str] = set()
    missing: Set[str] = set(UCI_FEATURE_ORDER)
    reliable_sources: dict[str, Any] = {}
    approximate_sources: dict[str, Any] = {}

    gender = str(getattr(user, "gender", "") or "").strip().lower()
    if gender not in _MISSING_GENDERS:
        available.add("X2")
        missing.discard("X2")
        reliable_sources["X2"] = "User.gender"

    raw_age = getattr(user, "age", None)
    age = _finite_number(raw_age)
    if age is None and raw_age not in (None, ""):
        _record_invalid_number(
            diagnostics,
            feature="X5",
            source="User.age",
        )
    elif age is not None and 18 <= age <= 100:
        available.add("X5")
        missing.discard("X5")
        reliable_sources["X5"] = "User.age"

    historical_cards = tuple(
        card
        for card in owned_cards
        if card.kind == "credit"
        and card.status in _HISTORICAL_CARD_STATUSES
    )

    if normalized_customer_type != "new":
        credit_limits: list[float] = []
        for card in historical_cards:
            limit = _finite_number(card.credit_limit)
            if limit is None:
                _record_invalid_number(
                    diagnostics,
                    feature="X1",
                    source="Card.credit_limit",
                )
            elif limit > 0:
                credit_limits.append(limit)
        for account in owned_accounts:
            if (
                account.kind != "credit"
                or account.status not in _HISTORICAL_CARD_STATUSES
            ):
                continue
            limit = _finite_number(account.credit_limit)
            if limit is None:
                _record_invalid_number(
                    diagnostics,
                    feature="X1",
                    source="Account.credit_limit",
                )
            elif limit > 0:
                credit_limits.append(limit)
        if credit_limits:
            available.add("X1")
            missing.discard("X1")
            reliable_sources["X1"] = "Card/Account credit_limit"

        valid_statement_cards: list[tuple[date, Card]] = []
        for card in historical_cards:
            statement_date = str(card.statement_date or "").strip()
            if not statement_date:
                continue
            parsed_date, date_error = _parse_statement_date(
                statement_date,
                as_of_date,
            )
            if date_error == "invalid":
                diagnostics.add(
                    "statement_date_invalid",
                    "Card.statement_date is not a valid YYYY-MM-DD date.",
                    "X12",
                )
                continue
            if date_error == "future":
                diagnostics.add(
                    "statement_date_future",
                    "Card.statement_date is later than the review date.",
                    "X12",
                )
                continue
            if date_error == "expired":
                diagnostics.add(
                    "statement_date_expired",
                    "Card.statement_date is older than the current statement window.",
                    "X12",
                )
                continue

            statement_amount = _finite_number(card.statement_amount)
            if statement_amount is None or statement_amount < 0:
                _record_invalid_number(
                    diagnostics,
                    feature="X12",
                    source="Card.statement_amount",
                )
                continue
            if parsed_date is not None:
                valid_statement_cards.append((parsed_date, card))

        if valid_statement_cards:
            _, latest_card = max(
                valid_statement_cards,
                key=lambda item: item[0],
            )
            available.add("X12")
            missing.discard("X12")
            reliable_sources["X12"] = {
                "source": "Card.statement_amount",
                "date_format": _STATEMENT_DATE_FORMAT,
                "timezone": "date-only calendar date; no timezone conversion",
                "max_age_days": _MAX_STATEMENT_AGE_DAYS,
                "card_id": latest_card.card_id,
            }

        month_order = _month_sequence(as_of_date)
        payment_months: Set[int] = set()
        for txn in owned_transactions:
            tags = set(str(tag) for tag in txn.tags)
            if _REPAYMENT_CATEGORY not in txn.category and not (
                tags & _REPAYMENT_TAGS
            ):
                continue

            raw_link = (txn.metadata or {}).get("credit_card")
            linked_card_id = str(raw_link or "").strip()
            if not linked_card_id:
                diagnostics.add(
                    "repayment_card_link_missing",
                    "A repayment transaction did not identify a credit card.",
                    "X18-X23",
                )
                continue

            linked_card = all_cards_by_id.get(linked_card_id)
            if linked_card is None:
                diagnostics.add(
                    "repayment_card_not_found",
                    "A repayment transaction referenced a card not present in the supplied cards.",
                    "X18-X23",
                )
                continue
            if linked_card.user_id != user.user_id:
                diagnostics.add(
                    "repayment_card_other_user",
                    "A repayment transaction referenced another user's card.",
                    "X18-X23",
                )
                continue
            if linked_card.kind != "credit":
                diagnostics.add(
                    "repayment_card_type_mismatch",
                    "A repayment transaction referenced a non-credit card.",
                    "X18-X23",
                )
                continue
            if linked_card.status not in _HISTORICAL_CARD_STATUSES:
                diagnostics.add(
                    "repayment_card_status_invalid",
                    "A repayment transaction referenced a card without eligible history status.",
                    "X18-X23",
                )
                continue
            if txn.status != "success" or txn.lifecycle_status != "completed":
                diagnostics.add(
                    "repayment_transaction_status_invalid",
                    "A repayment candidate was not successful and completed.",
                    "X18-X23",
                )
                continue

            amount = _finite_number(txn.amount)
            if amount is None:
                diagnostics.add(
                    "repayment_amount_invalid",
                    "A repayment transaction amount was not a finite number.",
                    "X18-X23",
                )
                continue
            if amount >= 0:
                diagnostics.add(
                    "repayment_direction_invalid",
                    "A repayment candidate did not have a negative outflow amount.",
                    "X18-X23",
                )
                continue

            txn_month = _transaction_month(txn.timestamp)
            if txn_month is None:
                diagnostics.add(
                    "invalid_transaction_timestamp",
                    "A repayment transaction timestamp could not be parsed.",
                    "X18-X23",
                )
                continue
            if txn_month not in month_order:
                diagnostics.add(
                    "repayment_transaction_out_of_window",
                    "A repayment transaction fell outside the recent six-month window.",
                    "X18-X23",
                )
                continue

            payment_account = accounts_by_id.get(txn.account_id)
            credit_account = accounts_by_id.get(linked_card.account_id)
            if payment_account is None or credit_account is None:
                diagnostics.add(
                    "repayment_currency_unverified",
                    "Transaction currency could not be verified from both linked accounts.",
                    "X18-X23",
                )
                continue
            payment_currency = str(payment_account.currency or "").strip().upper()
            credit_currency = str(credit_account.currency or "").strip().upper()
            if not payment_currency or not credit_currency:
                diagnostics.add(
                    "repayment_currency_unverified",
                    "Transaction currency was missing on a linked account.",
                    "X18-X23",
                )
                continue
            if payment_currency != credit_currency:
                diagnostics.add(
                    "repayment_currency_mismatch",
                    "Payment and credit-card account currencies did not match.",
                    "X18-X23",
                )
                continue

            payment_months.add(month_order.index(txn_month))

        for index in sorted(payment_months):
            feature = f"X{18 + index}"
            approximate.add(feature)
            missing.discard(feature)
            approximate_sources[feature] = (
                "Transaction credit_card_repayment aggregate; "
                "not a formal monthly statement record"
            )

    source_metadata: dict[str, Any] = {
        "feature_policy": FEATURE_VERSION,
        "customer_type_source": customer_type_source,
        "as_of": as_of_date.isoformat(),
        "counts": {
            "cards": len(owned_cards),
            "accounts": len(owned_accounts),
            "transactions": len(owned_transactions),
        },
        "reliable_sources": reliable_sources,
        "approximate_sources": approximate_sources,
        "history_completeness": {
            "claimed_complete": False,
            "reason": (
                "Repayment transactions do not prove complete monthly "
                "statement or repayment coverage."
            ),
        },
        "always_missing": {
            "X3": "No structured education field",
            "X4": "No structured marital-status field",
            "X6-X11": "No traceable formal monthly repayment-status history",
            "X13-X17": "No formal historical statement snapshots",
        },
    }

    return CreditFeatureSnapshot(
        customer_id=user.user_id,
        customer_type=normalized_customer_type,
        feature_version=feature_version,
        available_features=tuple(
            feature for feature in UCI_FEATURE_ORDER if feature in available
        ),
        approximate_features=tuple(
            feature for feature in UCI_FEATURE_ORDER if feature in approximate
        ),
        missing_features=tuple(
            feature for feature in UCI_FEATURE_ORDER if feature in missing
        ),
        source_metadata=source_metadata,
        diagnostics=diagnostics.as_tuple(),
    )


def check_credit_feature_availability(
    snapshot: CreditFeatureSnapshot,
) -> CreditFeatureAvailability:
    """Summarize a snapshot without treating approximate data as reliable."""
    available_count = len(snapshot.available_features)
    approximate_count = len(snapshot.approximate_features)
    missing_count = len(snapshot.missing_features)
    total = len(UCI_FEATURE_ORDER)
    reliable_coverage_ratio = available_count / total
    approximate_coverage_ratio = approximate_count / total
    potential_coverage_ratio = (
        available_count + approximate_count
    ) / total

    if missing_count == 0 and approximate_count == 0:
        status = "complete"
    elif available_count or approximate_count:
        status = "partial"
    else:
        status = "insufficient"

    warnings = []
    if approximate_count:
        warnings.append(
            "Approximate features are proxy data and are not reliable "
            "approval inputs."
        )
        warnings.append(
            "Recent repayment transactions do not prove complete six-month "
            "statement coverage."
        )
    if set(snapshot.missing_features) & {f"X{index}" for index in range(6, 12)}:
        warnings.append(
            "X6-X11 are missing because no formal monthly repayment-status "
            "history is available."
        )
    if missing_count:
        warnings.append(
            f"{missing_count} of {total} features are missing; "
            "missing features were not filled with zero."
        )

    return CreditFeatureAvailability(
        status=status,
        reliable_coverage_ratio=reliable_coverage_ratio,
        approximate_coverage_ratio=approximate_coverage_ratio,
        potential_coverage_ratio=potential_coverage_ratio,
        available_features=snapshot.available_features,
        approximate_features=snapshot.approximate_features,
        missing_features=snapshot.missing_features,
        warnings=tuple(warnings),
        diagnostics=snapshot.diagnostics,
    )
