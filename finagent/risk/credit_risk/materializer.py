"""Read-only Business Credit Feature Materializer v0.1."""

from __future__ import annotations

import math
import uuid
from datetime import date, datetime, timezone
from typing import Iterable, Optional, Sequence

from finagent.data.models import Account, Card, Transaction, User
from finagent.risk.credit_risk.business_contract import (
    BUSINESS_FEATURE_KEYS,
    BusinessCreditFeatureSnapshot,
    BusinessFeatureContractError,
    FeatureAvailability,
    FeatureSource,
    MaterializationDiagnostic,
    MaterializedFeatureValue,
)
from finagent.risk.credit_risk.contract import CustomerType


_FEATURE_SCHEMA_VERSION = "business_credit_features_v1"
_SOURCE_SCHEMA_VERSION = "business_objects_v1"
_REF_NAMESPACE = uuid.UUID("8c4f93f8-6a1f-4d13-a4f7-23ff69155f9c")


class MaterializerInputError(ValueError):
    """Raised when the materializer receives invalid top-level input."""


def _opaque_ref(prefix: str, value: str) -> str:
    return f"{prefix}_{uuid.uuid5(_REF_NAMESPACE, value)}"


def _diagnostic(
    code: str,
    *,
    feature_key: str = "",
    severity: str = "warning",
) -> MaterializationDiagnostic:
    return MaterializationDiagnostic(
        code=code,
        feature_key=feature_key,
        severity=severity,  # type: ignore[arg-type]
    )


def _calculate_age(birthday: str, as_of: date) -> Optional[int]:
    try:
        birth_date = date.fromisoformat(str(birthday))
    except (TypeError, ValueError):
        return None
    if birth_date > as_of:
        return None
    age = as_of.year - birth_date.year
    if (as_of.month, as_of.day) < (birth_date.month, birth_date.day):
        age -= 1
    if not 18 <= age <= 100:
        return None
    return age


class BusinessFeatureMaterializer:
    """Materialize only features supported by current business objects.

    The materializer never creates formal statement cycles. Therefore all
    repayment, statement and payment history features remain missing in v0.1.
    """

    def materialize(
        self,
        user: User,
        *,
        cards: Sequence[Card] = (),
        accounts: Sequence[Account] = (),
        transactions: Sequence[Transaction] = (),
        as_of: Optional[datetime] = None,
        customer_type: CustomerType = "unknown",
    ) -> BusinessCreditFeatureSnapshot:
        if not isinstance(user, User) or not str(user.user_id or "").strip():
            raise MaterializerInputError("user must be a valid User")
        timestamp = as_of or datetime.now(timezone.utc)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise MaterializerInputError("as_of must be timezone-aware")
        timestamp = timestamp.astimezone(timezone.utc)
        normalized_customer_type = str(customer_type).strip().lower()
        if normalized_customer_type not in {"new", "existing", "unknown"}:
            raise MaterializerInputError("invalid customer_type")

        cards = tuple(cards or ())
        accounts = tuple(accounts or ())
        transactions = tuple(transactions or ())
        diagnostics: dict[tuple[str, str], MaterializationDiagnostic] = {}

        def add_diagnostic(
            code: str,
            feature_key: str = "",
            severity: str = "warning",
        ) -> None:
            diagnostic = _diagnostic(
                code,
                feature_key=feature_key,
                severity=severity,
            )
            diagnostics[(diagnostic.code, diagnostic.feature_key)] = diagnostic

        customer_ref = _opaque_ref("CUST", f"customer:{user.user_id}")
        run_ref = _opaque_ref(
            "RUN",
            f"materialization:{user.user_id}:{timestamp.isoformat()}",
        )
        batch_ref = _opaque_ref(
            "UPL",
            f"business-object-snapshot:{user.user_id}:{timestamp.date().isoformat()}",
        )
        source_manifest: dict[tuple[str, str], FeatureSource] = {}

        def register_source(source: FeatureSource) -> FeatureSource:
            source_manifest[(source.source_type, source.source_record_ref)] = source
            return source

        user_source = register_source(
            FeatureSource(
                source_type="user_profile",
                source_schema_version=_SOURCE_SCHEMA_VERSION,
                observed_at=timestamp,
                source_record_ref=_opaque_ref(
                    "SRC",
                    f"user-profile:{user.user_id}",
                ),
                customer_id_matches=True,
                data_complete=True,
                source_batch_ref=batch_ref,
            )
        )

        features: dict[str, MaterializedFeatureValue] = {}
        status_by_feature = {
            feature_key: "missing" for feature_key in BUSINESS_FEATURE_KEYS
        }

        features["gender"] = MaterializedFeatureValue(
            feature_key="gender",
            value=None,
            unit="category",
            status="policy_blocked",
            availability_reason="sensitive_attribute_policy",
            sources=(user_source,),
        )
        status_by_feature["gender"] = "policy_blocked"
        add_diagnostic("sensitive_attribute_policy_blocked", "gender")

        for unavailable_feature in ("education_level", "marital_status"):
            features[unavailable_feature] = MaterializedFeatureValue(
                feature_key=unavailable_feature,
                value=None,
                unit="category",
                status="missing",
                availability_reason="no_structured_business_field",
                sources=(),
            )
            add_diagnostic("structured_profile_field_missing", unavailable_feature)

        age = _calculate_age(user.birthday, timestamp.date())
        if age is None:
            features["verified_age"] = MaterializedFeatureValue(
                feature_key="verified_age",
                value=None,
                unit="years",
                status="missing",
                availability_reason="verified_birthday_unavailable",
                sources=(),
            )
            add_diagnostic("verified_birthday_unavailable", "verified_age")
        else:
            features["verified_age"] = MaterializedFeatureValue(
                feature_key="verified_age",
                value=age,
                unit="years",
                status="approximate",
                availability_reason="profile_birthday_age",
                sources=(user_source,),
                completeness=0.8,
                approximation_method="profile_birthday_age",
            )
            status_by_feature["verified_age"] = "approximate"

        owned_cards = tuple(
            card for card in cards if card.user_id == user.user_id
        )
        if any(card.user_id != user.user_id for card in cards):
            add_diagnostic("card_ownership_mismatch", "current_credit_limit")
        accounts_by_id = {
            account.account_id: account for account in accounts
        }
        if any(account.user_id != user.user_id for account in accounts):
            add_diagnostic("account_ownership_mismatch", "current_credit_limit")

        active_credit_cards = tuple(
            card
            for card in owned_cards
            if card.kind == "credit" and card.status == "active"
        )
        current_limit_feature: Optional[MaterializedFeatureValue] = None
        if not active_credit_cards:
            add_diagnostic("no_active_credit_card", "current_credit_limit")
        elif len(active_credit_cards) > 1:
            add_diagnostic(
                "multiple_credit_cards_ambiguous",
                "current_credit_limit",
            )
        else:
            card = active_credit_cards[0]
            account = accounts_by_id.get(card.account_id)
            if (
                account is None
                or account.user_id != user.user_id
                or account.kind != "credit"
            ):
                add_diagnostic(
                    "credit_account_ownership_unverified",
                    "current_credit_limit",
                )
            elif not math.isfinite(float(card.credit_limit)) or card.credit_limit <= 0:
                add_diagnostic("credit_limit_invalid", "current_credit_limit")
            elif not math.isclose(
                float(card.credit_limit),
                float(account.credit_limit),
                abs_tol=0.01,
            ):
                add_diagnostic("credit_limit_source_mismatch", "current_credit_limit")
            else:
                try:
                    source = register_source(
                        FeatureSource(
                            source_type="account_snapshot",
                            source_schema_version=_SOURCE_SCHEMA_VERSION,
                            observed_at=timestamp,
                            source_record_ref=_opaque_ref(
                                "SRC",
                                f"credit-account:{user.user_id}:{account.account_id}",
                            ),
                            customer_id_matches=True,
                            account_id_matches=True,
                            card_id_matches=True,
                            currency_verified=True,
                            currency_code=account.currency,
                            data_complete=True,
                            source_batch_ref=batch_ref,
                        )
                    )
                except BusinessFeatureContractError:
                    add_diagnostic(
                        "credit_account_currency_invalid",
                        "current_credit_limit",
                    )
                else:
                    current_limit_feature = MaterializedFeatureValue(
                        feature_key="current_credit_limit",
                        value=float(card.credit_limit),
                        unit=account.currency.upper(),
                        status="reliable",
                        availability_reason="matched_card_and_account",
                        sources=(source,),
                        aggregation_scope="account",
                        account_ref=_opaque_ref(
                            "ACCT",
                            f"account:{account.account_id}",
                        ),
                        completeness=1.0,
                    )

        if current_limit_feature is None:
            features["current_credit_limit"] = MaterializedFeatureValue(
                feature_key="current_credit_limit",
                value=None,
                unit="CNY",
                status="missing",
                availability_reason="current_credit_limit_unavailable",
                sources=(),
            )
        else:
            features["current_credit_limit"] = current_limit_feature
            status_by_feature["current_credit_limit"] = "reliable"

        cards_by_id = {card.card_id: card for card in cards}
        for txn in transactions:
            if txn.user_id != user.user_id:
                add_diagnostic("transaction_ownership_mismatch")
                continue
            tags = set(str(tag) for tag in txn.tags)
            if "信用卡还款" not in txn.category and (
                "credit_card_repayment" not in tags
            ):
                continue
            if txn.status != "success" or txn.lifecycle_status != "completed":
                add_diagnostic("repayment_transaction_status_invalid")
                continue
            linked_card_id = str(
                (txn.metadata or {}).get("credit_card") or ""
            ).strip()
            linked_card = cards_by_id.get(linked_card_id)
            if linked_card is None:
                add_diagnostic("repayment_card_not_found")
                continue
            if linked_card.user_id != user.user_id:
                add_diagnostic("repayment_card_ownership_mismatch")
                continue
            if linked_card.kind != "credit":
                add_diagnostic("repayment_card_type_mismatch")
                continue
            payment_account = accounts_by_id.get(txn.account_id)
            credit_account = accounts_by_id.get(linked_card.account_id)
            if payment_account is None or credit_account is None:
                add_diagnostic("repayment_account_unverified")
            elif str(payment_account.currency or "").upper() != str(
                credit_account.currency or ""
            ).upper():
                add_diagnostic("repayment_currency_mismatch")
            add_diagnostic("formal_statement_cycle_missing")

        for feature_key in BUSINESS_FEATURE_KEYS:
            if feature_key.startswith(
                ("repayment_status_", "statement_amount_", "payment_amount_")
            ):
                add_diagnostic("formal_statement_history_missing", feature_key)

        reliable_features = tuple(
            feature_key
            for feature_key in BUSINESS_FEATURE_KEYS
            if status_by_feature[feature_key] == "reliable"
        )
        approximate_features = tuple(
            feature_key
            for feature_key in BUSINESS_FEATURE_KEYS
            if status_by_feature[feature_key] == "approximate"
        )
        missing_features = tuple(
            feature_key
            for feature_key in BUSINESS_FEATURE_KEYS
            if status_by_feature[feature_key] == "missing"
        )
        policy_blocked_features = tuple(
            feature_key
            for feature_key in BUSINESS_FEATURE_KEYS
            if status_by_feature[feature_key] == "policy_blocked"
        )
        total = len(BUSINESS_FEATURE_KEYS)
        reliable_ratio = len(reliable_features) / total
        approximate_ratio = len(approximate_features) / total
        availability = FeatureAvailability(
            reliable_features=reliable_features,
            approximate_features=approximate_features,
            missing_features=missing_features,
            policy_blocked_features=policy_blocked_features,
            reliable_coverage_ratio=reliable_ratio,
            approximate_coverage_ratio=approximate_ratio,
            potential_coverage_ratio=reliable_ratio + approximate_ratio,
            six_month_coverage_complete=False,
            six_month_reliable_complete=False,
            statement_cycles=(),
            completeness_by_period={},
        )

        return BusinessCreditFeatureSnapshot(
            customer_ref=customer_ref,
            customer_type=normalized_customer_type,
            as_of=timestamp,
            feature_schema_version=_FEATURE_SCHEMA_VERSION,
            features=features,
            coverage_summary=availability,
            source_manifest=tuple(source_manifest.values()),
            diagnostics=tuple(diagnostics.values()),
            materialization_run_ref=run_ref,
        )


def materialize_business_credit_features(
    user: User,
    *,
    cards: Sequence[Card] = (),
    accounts: Sequence[Account] = (),
    transactions: Sequence[Transaction] = (),
    as_of: Optional[datetime] = None,
    customer_type: CustomerType = "unknown",
) -> BusinessCreditFeatureSnapshot:
    """Convenience wrapper around :class:`BusinessFeatureMaterializer`."""
    return BusinessFeatureMaterializer().materialize(
        user,
        cards=cards,
        accounts=accounts,
        transactions=transactions,
        as_of=as_of,
        customer_type=customer_type,
    )


__all__ = [
    "BusinessFeatureMaterializer",
    "MaterializerInputError",
    "materialize_business_credit_features",
]
