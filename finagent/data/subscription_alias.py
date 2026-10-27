"""Platform aliases and canonical subscription selection."""
from __future__ import annotations

from typing import Dict, Iterable, Optional

from finagent.data.models import Subscription


PLATFORM_ALIASES = (
    ("qq音乐", "QQ音乐绿钻"),
    ("腾讯视频", "腾讯视频VIP"),
    ("爱奇艺", "爱奇艺会员"),
    ("icloud", "iCloud+ 云存储"),
    ("keep", "Keep会员"),
    ("百度网盘", "百度网盘超级会员"),
    ("美团外卖", "美团外卖会员"),
)


def canonical_platform_key(name: str) -> str:
    normalized = "".join(str(name or "").lower().split())
    for alias, canonical in PLATFORM_ALIASES:
        if alias in normalized:
            return "".join(canonical.lower().split())
    return normalized


def canonical_platform_name(name: str) -> str:
    key = canonical_platform_key(name)
    for _, canonical in PLATFORM_ALIASES:
        if canonical_platform_key(canonical) == key:
            return canonical
    return str(name or "").strip()


def subscription_matches_platform(subscription: Subscription, platform: str) -> bool:
    left = canonical_platform_key(subscription.merchant)
    right = canonical_platform_key(platform)
    return bool(left and right) and (
        left == right
        or left in right
        or right in left
    )


def select_subscription(
    subscriptions: Iterable[Subscription],
    platform: str = "",
) -> Optional[Subscription]:
    candidates = [
        subscription
        for subscription in subscriptions
        if not platform or subscription_matches_platform(subscription, platform)
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda subscription: (
            subscription.status == "active",
            subscription.expires_at or subscription.next_charge_date or "",
            subscription.updated_at,
            subscription.sub_id,
        ),
    )


def deduplicate_subscriptions(
    records: Dict[str, Subscription],
) -> Dict[str, Subscription]:
    """Collapse legacy short-name/full-name duplicates into one record."""
    grouped: Dict[str, list[Subscription]] = {}
    for subscription in records.values():
        grouped.setdefault(
            canonical_platform_key(subscription.merchant),
            [],
        ).append(subscription)
    if all(len(group) == 1 for group in grouped.values()):
        return records

    deduplicated: Dict[str, Subscription] = {}
    for group in grouped.values():
        selected = select_subscription(group)
        if selected is None:
            continue
        canonical_name = canonical_platform_name(selected.merchant)
        if canonical_name:
            selected.merchant = canonical_name
        deduplicated[selected.sub_id] = selected
    return deduplicated
