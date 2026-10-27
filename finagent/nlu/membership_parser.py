"""Parsing helpers for batch membership purchases."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


TERM_RE = re.compile(r"(\d+)\s*(个月|月|年)")
PRICE_RE = re.compile(r"(\d+(?:\.\d{1,2})?)\s*元")
RENEWAL_RE = re.compile(
    r"续费价(?:格)?\s*(?:每次|每月|每年|每期)?\s*(\d+(?:\.\d{1,2})?)\s*元"
)


@dataclass
class MembershipDraftItem:
    platform: str
    term_months: int
    purchase_price: float
    auto_renew: Optional[bool] = None
    renewal_price: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "term_months": self.term_months,
            "purchase_price": self.purchase_price,
            "auto_renew": self.auto_renew,
            "renewal_price": self.renewal_price,
        }


def _clean_platform(segment: str, term_start: int) -> str:
    value = segment[:term_start].strip()
    value = re.sub(r"^(?:帮我|给我|请|麻烦)?\s*", "", value)
    value = re.sub(r"^(?:充|购买|开通|充值|办理|办|买)\s*", "", value)
    value = re.sub(r"^(?:给|为)\s*", "", value)
    value = re.sub(r"\s*(?:充|购买|开通|充值|办理|办|买)$", "", value)
    value = value.strip(" ，,。:：")
    return value


def parse_membership_items(text: str) -> List[MembershipDraftItem]:
    """Parse one or more platform/term/price items from a user instruction."""
    segments = [part.strip() for part in re.split(r"[；;、]", text or "") if part.strip()]
    items: List[MembershipDraftItem] = []
    for segment in segments:
        term_match = TERM_RE.search(segment)
        if term_match is None:
            continue
        term = int(term_match.group(1))
        if term_match.group(2) == "年":
            term *= 12
        platform = _clean_platform(segment, term_match.start())
        if not platform:
            continue

        renewal_match = RENEWAL_RE.search(segment)
        renewal_price = float(renewal_match.group(1)) if renewal_match else 0.0
        price_spans = set()
        if renewal_match:
            price_spans.update(range(*renewal_match.span(1)))
        purchase_price = 0.0
        for match in PRICE_RE.finditer(segment, term_match.end()):
            if any(index in price_spans for index in range(*match.span(1))):
                continue
            purchase_price = float(match.group(1))
            break

        auto_renew: Optional[bool]
        if re.search(r"(?:不|无需|不要)自动续费|不续费|一次性", segment):
            auto_renew = False
        elif "自动续费" in segment:
            auto_renew = True
        else:
            auto_renew = None
        if auto_renew is False:
            renewal_price = 0.0
        elif "续费价同本次" in segment or "续费价格同本次" in segment:
            renewal_price = purchase_price

        items.append(MembershipDraftItem(
            platform=platform,
            term_months=term,
            purchase_price=purchase_price,
            auto_renew=auto_renew,
            renewal_price=renewal_price,
        ))
    return items


def _copy_items(items: List[Any]) -> List[Dict[str, Any]]:
    copied: List[Dict[str, Any]] = []
    for item in items:
        if hasattr(item, "as_dict"):
            value = item.as_dict()
        elif isinstance(item, dict):
            value = dict(item)
        else:
            continue
        value.setdefault("auto_renew", None)
        value.setdefault("renewal_price", 0.0)
        copied.append(value)
    return copied


def apply_membership_options(
    items: List[Any],
    text: str,
) -> List[Dict[str, Any]]:
    """Apply order-wide or numbered auto-renew choices to draft items."""
    updated = _copy_items(items)
    all_auto = "都不自动续费" not in text and (
        "都自动续费" in text or "全部自动续费" in text
    )
    all_manual = "都不自动续费" in text or "全部不自动续费" in text
    same_price = (
        "同本次价格" in text
        or "同本次价" in text
        or "按本次价格" in text
        or "按本次价" in text
    )
    order_renewal = RENEWAL_RE.search(text)

    if all_auto or all_manual:
        for item in updated:
            item["auto_renew"] = all_auto
            if all_manual:
                item["renewal_price"] = 0.0
            elif order_renewal:
                item["renewal_price"] = float(order_renewal.group(1))
            elif same_price:
                item["renewal_price"] = float(item.get("purchase_price") or 0)

    pattern = re.compile(
        r"(?:^|[；;，,\s])(\d+)\s*"
        r"(自动续费|不自动续费|不续费|自动|不自动)"
        r"([^；;]*)"
    )
    for number, choice, tail in pattern.findall(text):
        index = int(number) - 1
        if index < 0 or index >= len(updated):
            continue
        enabled = choice in {"自动续费", "自动"}
        updated[index]["auto_renew"] = enabled
        if not enabled:
            updated[index]["renewal_price"] = 0.0
            continue
        renewal = RENEWAL_RE.search(tail)
        if renewal:
            updated[index]["renewal_price"] = float(renewal.group(1))
        elif same_price:
            updated[index]["renewal_price"] = float(
                updated[index].get("purchase_price") or 0
            )
    return updated


def apply_membership_renewal_prices(
    items: List[Any],
    text: str,
) -> List[Dict[str, Any]]:
    """Apply numbered or order-wide renewal prices to auto-renew items."""
    updated = _copy_items(items)
    if (
        "同本次价格" in text
        or "同本次价" in text
        or "按本次价格" in text
        or "按本次价" in text
    ):
        for item in updated:
            item["renewal_price"] = float(item.get("purchase_price") or 0)
        return updated

    assigned = False
    for number, price in re.findall(
        r"(\d+)\D{0,8}(\d+(?:\.\d{1,2})?)\s*元",
        text,
    ):
        index = int(number) - 1
        if 0 <= index < len(updated):
            updated[index]["renewal_price"] = float(price)
            assigned = True
    if assigned:
        return updated

    auto_items = [item for item in updated if item.get("auto_renew")]
    prices = [
        float(value)
        for value in re.findall(r"(\d+(?:\.\d{1,2})?)\s*元", text)
    ]
    if len(auto_items) == 1 and prices:
        auto_items[0]["renewal_price"] = prices[0]
    return updated


def validate_membership_items(items: List[Dict[str, Any]]) -> List[str]:
    errors: List[str] = []
    platforms: Dict[str, int] = {}
    for index, item in enumerate(items, start=1):
        platform = str(item.get("platform") or "").strip()
        if not platform:
            errors.append(f"第 {index} 项缺少平台名称")
        else:
            key = platform.casefold()
            if key in platforms:
                errors.append(f"平台「{platform}」重复，请合并为一个项目")
            platforms[key] = index
        try:
            term = int(item.get("term_months"))
        except (TypeError, ValueError):
            term = 0
        if term < 1 or term > 36:
            errors.append(f"「{platform or index}」周期必须在 1 到 36 个月之间")
        try:
            price = float(item.get("purchase_price"))
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            errors.append(f"「{platform or index}」本次价格必须大于 0")
        auto_renew = item.get("auto_renew")
        if auto_renew is None:
            errors.append(f"「{platform or index}」尚未选择是否自动续费")
        elif auto_renew:
            try:
                renewal_price = float(item.get("renewal_price"))
            except (TypeError, ValueError):
                renewal_price = 0.0
            if renewal_price <= 0:
                errors.append(f"「{platform or index}」开启自动续费后需要填写续费价格")
    return errors
