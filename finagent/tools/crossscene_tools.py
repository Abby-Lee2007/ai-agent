"""跨场景联动工具集。

这是最能体现"Agent 自主规划"的部分，覆盖赛题示例场景：
    检测我爱人生日 -> 当月锁定 1000 元活期 -> 生日前 2 天订购鲜花、蛋糕

拆成了几个可复用的原子工具：
    goal.create                锁定资金、建立目标
    crossscene.schedule_gift   预约某天为某人下单某件礼物
    crossscene.daily_brief     主动汇总"今天需要关注什么"
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from finagent.data.models import MerchantOffer, User
from finagent.data.providers import MerchantOfferProvider
from finagent.tools.base import Tool, ToolContext, ToolParam, ToolResult, format_money, render_table
from finagent.tools.transfer_tools import resolve_account
from finagent.utils import parse_date


def lookup_person(ctx: ToolContext, name: str) -> Tuple[Optional[User], Optional[date]]:
    """按称呼找到"人"以及 ta 的生日。

    支持两种线索：
    1. 称呼命中客户画像里的关系（如"老婆" -> preferences.spouse）；
    2. 直接按姓名匹配通讯录 / 模拟用户表。
    """
    store = ctx.store
    user = ctx.user
    prefs = user.preferences or {}
    keyword = (name or "").strip()

    relation_keys = {
        "老婆": "spouse", "妻子": "spouse", "爱人": "spouse", "老公": "spouse", "丈夫": "spouse",
        "妈妈": "mother", "母亲": "mother", "爸爸": "father", "父亲": "father",
    }
    key = relation_keys.get(keyword)
    resolved_name = prefs.get(key, keyword) if key else keyword

    birthday_raw = ""
    relation_groups = {
        "spouse": {"老婆", "妻子", "爱人", "配偶", "丈夫", "老公"},
        "mother": {"妈妈", "母亲", "妈"},
        "father": {"爸爸", "父亲", "爸"},
    }
    wanted_relations = relation_groups.get(key or "", set())
    for item in prefs.get("care_people") or []:
        if not isinstance(item, dict):
            continue
        item_name = str(item.get("name") or "").strip()
        relation = str(item.get("relation") or "").strip()
        if (
            item_name == keyword
            or (item_name and item_name in keyword)
            or (relation and relation in keyword)
            or bool(wanted_relations & {relation})
        ):
            resolved_name = item_name or resolved_name
            birthday_raw = str(item.get("birthday") or "")
            key = "care"
            break

    if not birthday_raw and key == "spouse" and prefs.get("spouse_birthday"):
        birthday_raw = prefs["spouse_birthday"]

    person: Optional[User] = None
    for candidate in store.users.values():
        if candidate.name in {resolved_name, keyword}:
            person = candidate
            break
    if person is None:
        payee = (
            ctx.providers.payees.find(user.user_id, resolved_name)
            if getattr(ctx, "providers", None) is not None
            else store.find_payee(user.user_id, resolved_name)
        )
        if payee:
            resolved_name = payee.name
            for candidate in store.users.values():
                if candidate.name == resolved_name:
                    person = candidate
                    break
    if person is None and resolved_name:
        # The user can edit the spouse's display name without creating a full
        # customer record. Keep the profile-provided name in the conversation.
        person = User(
            user_id=f"profile:{key or 'contact'}",
            name=resolved_name,
            phone="",
            city=user.city,
            birthday=birthday_raw,
            source="user:profile",
            is_demo=False,
        )

    if not birthday_raw and person and person.birthday:
        birthday_raw = person.birthday

    birthday: Optional[date] = None
    if birthday_raw:
        try:
            parsed = date.fromisoformat(birthday_raw)
        except ValueError:
            parsed = None
        if parsed:
            today = ctx.clock.today()
            month, day = parsed.month, parsed.day
            try:
                upcoming = date(today.year, month, day)
            except ValueError:
                upcoming = date(today.year, 3, 1)
            if upcoming < today:
                try:
                    upcoming = date(today.year + 1, month, day)
                except ValueError:
                    upcoming = date(today.year + 1, 3, 1)
            birthday = upcoming
    return person, birthday


def lookup_person_preferences(ctx: ToolContext, name: str) -> Dict[str, Any]:
    """Read gift preferences and restrictions from the editable care-person profile."""
    prefs = ctx.user.preferences or {}
    keyword = (name or "").strip()
    if keyword in {"老婆", "妻子", "爱人", "配偶", "老公", "丈夫"}:
        keyword = str(prefs.get("spouse") or keyword)
    for item in prefs.get("care_people") or []:
        if not isinstance(item, dict):
            continue
        person_name = str(item.get("name") or "").strip()
        relation = str(item.get("relation") or "").strip()
        if (
            person_name == keyword
            or (person_name and person_name in name)
            or (relation and relation in name)
        ):
            return {
                "favorite_gift": list(item.get("favorite_gift") or []),
                "avoid": list(item.get("avoid") or []),
                "relation": relation,
            }
    return {
        "favorite_gift": list(prefs.get("favorite_gift") or []),
        "avoid": list(prefs.get("avoid") or []),
        "relation": "配偶" if name in {"老婆", "妻子", "爱人"} else "",
    }


GIFT_OFFER_LABELS = {
    "OFFER000005": "定制相册",
    "OFFER000006": "银饰礼盒",
    "OFFER000007": "双人手作体验",
    "OFFER000008": "纪念照片书",
    "OFFER000009": "定制饰品",
    "OFFER000010": "大疆 Osmo Mobile 7P 手机云台",
    "OFFER000011": "大疆 Mini 4K 无人机",
    "OFFER000012": "大疆 Action 4 运动相机",
}

FRUIT_AVOID_WORDS = (
    "水果", "芒果", "草莓", "桃", "苹果", "梨", "柑橘", "橙", "柚", "莓",
    "樱桃", "葡萄", "猕猴桃", "菠萝", "榴莲", "椰", "荔枝", "龙眼", "哈密瓜",
    "甜瓜",
)
SEAFOOD_AVOID_WORDS = (
    "海鲜", "虾", "蟹", "鱼", "贝", "蛤", "蚝", "鱿鱼", "章鱼",
)
DAIRY_EGG_AVOID_WORDS = (
    "奶", "蛋过敏", "蛋", "麸质", "小麦", "花生", "坚果", "芝麻",
)
FLOWER_AVOID_WORDS = ("花粉", "鲜花", "花过敏")
CAKE_AVOID_WORDS = ("蛋糕", "甜食", "甜品", "奶", "蛋过敏", "麸质", "小麦")


def offer_product_label(offer: MerchantOffer) -> str:
    return GIFT_OFFER_LABELS.get(offer.offer_id, f"{offer.merchant}{offer.item}")


def offer_risk_text(offer: MerchantOffer) -> str:
    return f"{offer.merchant} {offer.item} {offer_product_label(offer)}"


def ensure_gift_offer_catalog(ctx: ToolContext) -> None:
    """Backfill non-flower, non-food gifts for editable preference filtering."""
    catalog = [
        ("OFFER000005", "时光纪", "礼物", 269.0, "定制相册"),
        ("OFFER000006", "拾光银饰", "礼物", 499.0, "银饰礼盒"),
        ("OFFER000007", "周末手作", "礼物", 399.0, "双人手作体验"),
        ("OFFER000008", "纸上时光", "礼物", 329.0, "纪念照片书"),
        ("OFFER000009", "匠作工坊", "礼物", 599.0, "定制饰品"),
        ("OFFER000010", "大疆", "礼物", 599.0, "大疆 Osmo Mobile 7P 手机云台"),
        ("OFFER000011", "大疆", "礼物", 1499.0, "大疆 Mini 4K 无人机"),
        ("OFFER000012", "大疆", "礼物", 2199.0, "大疆 Action 4 运动相机"),
    ]
    for offer_id, merchant, item, price, label in catalog:
        GIFT_OFFER_LABELS[offer_id] = label
        if offer_id in ctx.store.merchant_offers:
            continue
        offer = MerchantOffer(
            offer_id=offer_id,
            merchant=merchant,
            item=item,
            price=price,
            inventory=20,
            delivery_cities=[ctx.user.city],
            delivery_radius_km=20.0,
            available_from="2026-01-01",
            available_to="2027-12-31",
            source="catalog:gift-demo",
            is_demo=True,
        )
        offer.updated_at = ctx.clock.iso()
        ctx.store.merchant_offers[offer.offer_id] = offer
        ctx.store.touch(offer)


def remember_gift_preferences(
    ctx: ToolContext,
    person: str,
    preferences: str,
    avoid: str,
) -> None:
    """Persist newly supplied gift preferences to the related care-person profile."""
    user = ctx.user
    prefs = dict(user.preferences or {})
    people = list(prefs.get("care_people") or [])
    target = None
    target_index = -1
    spouse_name = str(prefs.get("spouse") or "")
    for index, item in enumerate(people):
        if not isinstance(item, dict):
            continue
        if (
            str(item.get("name") or "") in {person, spouse_name}
            or str(item.get("relation") or "") in {"配偶", "老婆", "妻子", "爱人"}
        ):
            target = dict(item)
            target_index = index
            break

    def append_values(existing: Any, value: str, ignored: set[str]) -> List[str]:
        if isinstance(existing, list):
            values = [str(item).strip() for item in existing if str(item).strip()]
        else:
            values = [
                item.strip()
                for item in re.split(r"[,，、]", str(existing or ""))
                if item.strip()
            ]
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in ignored and cleaned not in values:
            values.append(cleaned)
        return values

    next_favorites = append_values(
        (target or {}).get("favorite_gift", prefs.get("favorite_gift")),
        preferences,
        {"沿用资料", "没有补充", "无"},
    )
    next_avoid = append_values(
        (target or {}).get("avoid", prefs.get("avoid")),
        avoid,
        {"无", "没有", "没有过敏", "无过敏"},
    )
    if target is not None and target_index >= 0:
        target["favorite_gift"] = next_favorites
        target["avoid"] = next_avoid
        people[target_index] = target
        prefs["care_people"] = people
    else:
        prefs["favorite_gift"] = next_favorites
        prefs["avoid"] = next_avoid
    user.preferences = prefs
    user.source = "user:profile"
    user.updated_at = ctx.clock.iso()
    ctx.store.touch(user)


class GoalCreateTool(Tool):
    """创建资金目标并锁定活期资金。"""

    name = "goal.create"
    category = "跨场景联动"
    description = "创建一个资金目标（如生日惊喜金、旅行基金），并从活期账户锁定相应金额"
    risk_level = "medium"
    requires_confirmation = True
    params = [
        ToolParam("name", "string", True, "目标名称"),
        ToolParam("target_amount", "number", True, "锁定/目标金额（元）"),
        ToolParam("deadline", "string", False, "目标日期，如 2026-11-20"),
        ToolParam("scenario", "string", False, "场景标识，如 birthday / travel"),
        ToolParam("note", "string", False, "备注"),
    ]
    examples = ["锁定1000块做生日基金", "建一个5000元的旅行基金"]

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        name: str,
        target_amount: Any,
        deadline: str = "",
        scenario: str = "",
        note: str = "",
    ) -> ToolResult:
        store = ctx.store
        try:
            amount = round(float(target_amount), 2)
        except (TypeError, ValueError):
            return ToolResult(False, f"金额「{target_amount}」无法识别。")
        if amount <= 0:
            return ToolResult(False, "锁定金额需要大于 0。")

        account = resolve_account(ctx)
        if account is None:
            return ToolResult(False, "没有找到可用的活期账户。")
        if account.available < amount:
            return ToolResult(
                False,
                f"活期可用余额不足：可用 {account.available:,.2f} 元，需要锁定 {amount:,.2f} 元。",
            )

        goal = (
            ctx.providers.goals.add(
                user_id=ctx.user_id,
                name=name,
                target_amount=amount,
                deadline=deadline or "",
                scenario=scenario,
                account_id=account.account_id,
                note=note,
            )
            if getattr(ctx, "providers", None) is not None
            else store.add_goal(
                user_id=ctx.user_id,
                name=name,
                target_amount=amount,
                deadline=deadline or "",
                scenario=scenario,
                account_id=account.account_id,
                note=note,
            )
        )
        goal.locked_amount = amount
        account.locked = round(account.locked + amount, 2)
        store.touch(goal, account)

        lines = [
            f"已创建资金目标「{name}」，锁定 {amount:,.2f} 元",
            f"  锁定账户：{account.name}（尾号 {account.tail}）",
            f"  账户余额 {account.balance:,.2f} 元，其中锁定 {account.locked:,.2f} 元，"
            f"可用 {account.available:,.2f} 元",
        ]
        if deadline:
            lines.append(f"  目标日期：{deadline}")
        lines.append(f"  目标编号：{goal.goal_id}（可随时说「解冻这笔钱」取消锁定）")
        return ToolResult(
            True,
            "\n".join(lines),
            data={"goal_id": goal.goal_id, "amount": amount, "account": account.account_id},
            summary=f"已锁定 {amount:,.2f} 元到目标「{name}」",
        )


class GoalListTool(Tool):
    """目标 / 心愿清单。"""

    name = "goal.list"
    category = "跨场景联动"
    description = "查看所有资金目标、锁定金额与进度"
    risk_level = "low"
    params = []
    examples = ["我的攒钱目标", "看看我锁了多少钱"]

    def run(self, ctx: ToolContext) -> ToolResult:  # type: ignore[override]
        goals = (
            ctx.providers.goals.of(ctx.user_id)
            if getattr(ctx, "providers", None) is not None
            else ctx.store.goals_of(ctx.user_id)
        )
        if not goals:
            return ToolResult(True, "您还没有设置资金目标。", summary="暂无目标")
        rows = []
        for g in goals:
            progress = g.locked_amount / g.target_amount * 100 if g.target_amount else 0
            rows.append([
                g.name,
                f"{g.target_amount:,.2f}",
                f"{g.locked_amount:,.2f}",
                f"{progress:.0f}%",
                g.deadline or "-",
                {"active": "进行中", "achieved": "已完成", "cancelled": "已取消"}.get(g.status, g.status),
            ])
        total_locked = sum(g.locked_amount for g in goals if g.status == "active")
        lines = [
            f"共有 {len(goals)} 个资金目标，当前锁定 {total_locked:,.2f} 元：",
            render_table(["目标", "目标金额", "已锁定", "进度", "目标日期", "状态"], rows),
        ]
        return ToolResult(
            True,
            "\n".join(lines),
            data={"count": len(goals), "total_locked": round(total_locked, 2)},
            summary=f"{len(goals)} 个目标，锁定 {total_locked:,.2f} 元",
        )


class GiftRecommendTool(Tool):
    """Recommend gift bundles without placing an order."""

    name = "crossscene.gift_recommend"
    category = "跨场景联动"
    description = "结合对象偏好、禁忌和预算生成可切换的生日礼物方案"
    risk_level = "low"
    params = [
        ToolParam("person", "string", True, "收礼人"),
        ToolParam("birthday", "string", True, "生日日期"),
        ToolParam("budget", "number", True, "礼物预算"),
        ToolParam("preferences", "string", False, "补充喜好"),
        ToolParam("avoid", "string", False, "过敏或禁忌"),
        ToolParam("offset", "integer", False, "换一批偏移量"),
    ]
    examples = ["给我老婆推荐生日礼物", "换一批生日礼物"]

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        person: str,
        birthday: str,
        budget: Any,
        preferences: str = "",
        avoid: str = "",
        offset: int = 0,
    ) -> ToolResult:
        target, parsed_birthday = lookup_person(ctx, person)
        display_name = target.name if target else person
        profile = lookup_person_preferences(ctx, person)
        remember_gift_preferences(ctx, display_name, preferences, avoid)
        try:
            max_budget = round(float(budget), 2)
        except (TypeError, ValueError):
            return ToolResult(False, "礼物预算无法识别。")
        if max_budget <= 0:
            return ToolResult(False, "礼物预算必须大于 0。")
        birthday_day = parse_date(str(birthday), ctx.clock.today(), base=parsed_birthday)
        if birthday_day is None:
            return ToolResult(False, "生日日期无法识别，请重新输入。")
        delivery_day = birthday_day - timedelta(days=2)
        avoid_text = " ".join([
            " ".join(profile.get("avoid") or []),
            str(avoid or ""),
        ])
        preference_text = " ".join([
            " ".join(profile.get("favorite_gift") or []),
            str(preferences or ""),
        ])

        ensure_gift_offer_catalog(ctx)
        offers = []
        for offer in ctx.store.merchant_offers.values():
            if offer.inventory <= 0:
                continue
            if offer.delivery_cities and ctx.user.city not in offer.delivery_cities:
                continue
            if offer.available_from and delivery_day.isoformat() < offer.available_from:
                continue
            if offer.available_to and delivery_day.isoformat() > offer.available_to:
                continue
            offers.append(offer)
        flowers = sorted(
            [item for item in offers if item.item == "鲜花"],
            key=lambda item: item.price,
        )
        cakes = sorted(
            [item for item in offers if item.item == "蛋糕"],
            key=lambda item: item.price,
        )
        gifts = sorted(
            [item for item in offers if item.item == "礼物"],
            key=lambda item: item.price,
        )
        fruit_risk = any(word in avoid_text for word in FRUIT_AVOID_WORDS)
        seafood_risk = any(word in avoid_text for word in SEAFOOD_AVOID_WORDS)
        dairy_egg_risk = any(word in avoid_text for word in DAIRY_EGG_AVOID_WORDS)
        flower_risk = any(word in avoid_text for word in FLOWER_AVOID_WORDS)
        cake_risk = fruit_risk or any(word in avoid_text for word in CAKE_AVOID_WORDS)
        if flower_risk:
            flowers = []
        if cake_risk:
            cakes = []
        if fruit_risk:
            gifts = [
                gift for gift in gifts
                if not any(word in offer_risk_text(gift) for word in FRUIT_AVOID_WORDS)
            ]
        if seafood_risk:
            gifts = [
                gift for gift in gifts
                if not any(word in offer_risk_text(gift) for word in SEAFOOD_AVOID_WORDS)
            ]
        if dairy_egg_risk:
            gifts = [
                gift for gift in gifts
                if not any(word in offer_risk_text(gift) for word in DAIRY_EGG_AVOID_WORDS)
            ]

        preference_groups = {
            ("大疆", "无人机", "云台", "运动相机", "数码", "电子", "相机", "摄影"): (
                "大疆", "无人机", "云台", "运动相机",
            ),
            ("相册", "照片", "纪念", "回忆", "记录"): ("相册", "照片书", "纪念"),
            ("银饰", "首饰", "饰品", "项链"): ("银饰", "饰品"),
            ("手作", "体验", "一起", "共同"): ("手作", "体验"),
        }
        gift_scores: Dict[str, int] = {}
        for triggers, matches in preference_groups.items():
            if not any(trigger in preference_text for trigger in triggers):
                continue
            for gift in gifts:
                text = offer_risk_text(gift)
                if any(match in text for match in matches):
                    gift_scores[gift.offer_id] = gift_scores.get(gift.offer_id, 0) + 1
        matched_gifts = [gift for gift in gifts if gift.offer_id in gift_scores]
        if matched_gifts:
            gifts = matched_gifts + [
                gift for gift in gifts if gift.offer_id not in gift_scores
            ]

        options: List[Dict[str, Any]] = []

        def add_option(name: str, items: List[Any], reason: str) -> None:
            if not items:
                return
            total = round(sum(item.price for item in items), 2)
            key = tuple(item.offer_id for item in items)
            if total > max_budget or any(option["key"] == key for option in options):
                return
            options.append({
                "key": key,
                "name": name,
                "items": [
                    {
                        "item": item.item,
                        "label": offer_product_label(item),
                        "offer_id": item.offer_id,
                        "merchant": item.merchant,
                        "price": item.price,
                    }
                    for item in items
                ],
                "total": total,
                "reason": reason,
            })

        explicit_preference = str(preferences or "").strip()
        preference_label = (
            explicit_preference
            if explicit_preference not in {"", "沿用资料", "无"}
            else ""
        )
        if not preference_label and not matched_gifts:
            profile_favorites = [
                str(item).strip()
                for item in (profile.get("favorite_gift") or [])
                if str(item).strip() not in {"", "鲜花", "蛋糕"}
            ]
            preference_label = "、".join(profile_favorites)
        if preference_label and not matched_gifts:
            for suffix in ("生日礼物", "礼盒", "精选"):
                query = f"{preference_label} {suffix}"
                options.append({
                    "key": (f"taobao:{query}",),
                    "name": query,
                    "items": [{
                        "item": "礼物",
                        "label": query,
                        "offer_id": "",
                        "merchant": "淘宝搜索",
                        "price": 0.0,
                        "price_pending": True,
                    }],
                    "total": 0.0,
                    "reason": (
                        f"按本次偏好「{preference_label}」生成；"
                        "价格与库存请在淘宝页面核实"
                    ),
                    "external_only": True,
                    "search_query": query,
                })

        prefer_safe_gifts = fruit_risk or dairy_egg_risk or flower_risk or cake_risk
        prioritize_gifts = prefer_safe_gifts or bool(matched_gifts)

        def add_gift_options() -> None:
            gift_reasons = [
                "已避开当前食物与气味禁忌，适合长期留存",
                "预算内提升质感和纪念意义",
                "适合记录共同经历，实用性较强",
            ]
            for index, gift in enumerate(gifts[:3]):
                reason = gift_reasons[index]
                if gift.offer_id in gift_scores:
                    reason = f"匹配本次偏好「{preferences or preference_text}」，{reason}"
                add_option(
                    offer_product_label(gift),
                    [gift],
                    reason,
                )
            if len(gifts) >= 2:
                first_gift = gifts[0]
                second_gift = gifts[2] if len(gifts) >= 3 else gifts[1]
                add_option(
                    f"{offer_product_label(first_gift)} + {offer_product_label(second_gift)}",
                    [first_gift, second_gift],
                    "两份已避忌礼物组合，兼顾实用与纪念",
                )
            if len(gifts) >= 4:
                first_gift = gifts[1]
                second_gift = gifts[3]
                add_option(
                    f"{offer_product_label(first_gift)} + {offer_product_label(second_gift)}",
                    [first_gift, second_gift],
                    "兼顾纪念意义和实用体验",
                )

        if prioritize_gifts:
            add_gift_options()
        if flowers and cakes:
            flower_label = offer_product_label(flowers[0])
            cake_label = offer_product_label(cakes[0])
            premium_flower = offer_product_label(flowers[-1])
            premium_cake = offer_product_label(cakes[-1])
            add_option(
                f"{flower_label} + {cake_label}",
                [flowers[0], cakes[0]],
                "经典组合，兼顾仪式感和分享氛围",
            )
            add_option(
                f"{premium_flower} + {premium_cake}",
                [flowers[-1], cakes[-1]],
                "预算内品质升级，适合重视仪式感的对象",
            )
        if flowers:
            add_option(
                offer_product_label(flowers[0]),
                [flowers[0]],
                "简洁稳妥，预算占用较低",
            )
            if len(flowers) > 1:
                add_option(
                    offer_product_label(flowers[-1]),
                    [flowers[-1]],
                    "更精致的花材与包装",
                )
        if cakes:
            add_option(
                offer_product_label(cakes[0]),
                [cakes[0]],
                "适合喜欢甜品或需要与家人朋友分享",
            )
            if len(cakes) > 1:
                add_option(
                    offer_product_label(cakes[-1]),
                    [cakes[-1]],
                    "预算内提升口味和造型",
                )
        if not prioritize_gifts:
            add_gift_options()
        if not options:
            return ToolResult(
                False,
                f"在预算 {max_budget:,.2f} 元和当前禁忌条件下没有可预约礼物，"
                "请提高预算或调整禁忌条件。",
                data={"gift_choice": None},
            )

        start = int(offset or 0) % len(options)
        rotated = options[start:] + options[:start]
        visible = rotated[:3]
        for index, option in enumerate(visible, start=1):
            option["index"] = index
        rows = [
            [
                str(option["index"]),
                option["name"],
                " + ".join(item["label"] for item in option["items"]),
                f"{option['total']:,.2f}",
                "、".join(sorted({item["merchant"] for item in option["items"]})),
                option["reason"],
            ]
            for option in visible
        ]
        known_preferences = "、".join(profile.get("favorite_gift") or []) or "未记录"
        known_avoid = "、".join(profile.get("avoid") or []) or "无"
        lines = [
            f"为「{display_name}」准备的生日礼物候选（预算 {max_budget:,.2f} 元）：",
            f"  生日：{birthday_day.isoformat()}；计划提前 2 天（{delivery_day.isoformat()}）配送",
            f"  已知偏好：{known_preferences}；过敏/禁忌：{known_avoid}",
        ]
        if preferences:
            lines.append(f"  本次补充喜好：{preferences}")
        if avoid:
            lines.append(f"  本次补充禁忌：{avoid}")
        lines.extend([
            render_table(
                ["#", "方案", "包含", "预计总价", "商户", "推荐理由"],
                rows,
            ),
            "请回复「第1个」「第2个」或「第3个」选择方案；也可以回复「换一批」继续浏览。",
            "在你明确选择前不会锁定资金，也不会创建任何礼物预约。",
        ])
        context = {
            "person": person,
            "display_name": display_name,
            "birthday": birthday_day.isoformat(),
            "delivery_date": delivery_day.isoformat(),
            "budget": max_budget,
            "preferences": preferences,
            "avoid": avoid,
            "known_preferences": list(profile.get("favorite_gift") or []),
            "known_avoid": list(profile.get("avoid") or []),
            "offset": (start + len(visible)) % len(options),
            "options": visible,
        }
        return ToolResult(
            True,
            "\n".join(lines),
            data={
                "gift_choice": context,
                "options": [
                    {
                        "index": option["index"],
                        "name": option["name"],
                        "total": option["total"],
                        "items": option["items"],
                        "reason": option["reason"],
                        "external_only": bool(option.get("external_only")),
                        "search_query": str(option.get("search_query") or ""),
                    }
                    for option in visible
                ],
            },
            summary=f"已为{display_name}生成 {len(visible)} 组生日礼物方案",
        )


class ScheduleGiftTool(Tool):
    """预约下单（跨场景执行环节）。"""

    name = "crossscene.schedule_gift"
    category = "跨场景联动"
    description = "在指定日期为指定的人预约下单礼物（鲜花、蛋糕、礼物卡等）"
    risk_level = "medium"
    requires_confirmation = True
    params = [
        ToolParam("person", "string", True, "收礼人，如 老婆 / 林雅雯"),
        ToolParam("item", "string", True, "礼物类型，如 鲜花 / 蛋糕"),
        ToolParam("date", "string", True, "执行日期，如 2026-11-18 / 生日前2天"),
        ToolParam("budget", "number", False, "预算（元）"),
        ToolParam("offer_id", "string", False, "已选定的商户方案编号"),
        ToolParam("note", "string", False, "贺卡留言"),
    ]
    examples = ["生日前2天给老婆订一束花", "生日当天订蛋糕"]
    DEFAULT_BUDGET = {"鲜花": 268.0, "蛋糕": 328.0, "礼物": 500.0}

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        person: str,
        item: str,
        date: str,
        budget: Any = None,
        offer_id: str = "",
        note: str = "",
    ) -> ToolResult:
        store = ctx.store
        target_person, birthday = lookup_person(ctx, person)
        display_name = target_person.name if target_person else person

        run_day = parse_date(date, ctx.clock.today(), base=birthday)
        if run_day is None:
            return ToolResult(False, f"没能理解下单日期「{date}」。")

        try:
            max_budget = float(budget) if budget is not None else None
        except (TypeError, ValueError):
            max_budget = None
        merchant_provider = (
            ctx.providers.merchants
            if getattr(ctx, "providers", None) is not None
            else MerchantOfferProvider(store)
        )
        offer = store.merchant_offers.get(offer_id) if offer_id else None
        if offer is None:
            offer = merchant_provider.find_offer(
                item=item,
                budget=max_budget,
                delivery_date=run_day.isoformat(),
                city=ctx.user.city,
            )
        if offer is None:
            catalog_offer = merchant_provider.find_offer(
                item=item,
                budget=None,
                delivery_date=run_day.isoformat(),
                city=ctx.user.city,
            )
            if catalog_offer is not None:
                return ToolResult(
                    False,
                    f"「{item}」的演示商户最低报价为 {catalog_offer.price:,.2f} 元，"
                    "当前预算不足。",
                )
            if max_budget is None or max_budget <= 0:
                return ToolResult(
                    False,
                    f"演示商户目录中没有「{item}」，请提供本次礼物价格后再预约。",
                )
            merchant_name = "用户报价模拟代购"
            money = max_budget
            source = "user:quote"
            delivery_radius = 0.0
            source_note = "用户报价，未联网核验"
        else:
            merchant_name = offer.merchant
            money = offer.price
            source = offer.source
            delivery_radius = offer.delivery_radius_km
            source_note = offer.source

        task = (
            ctx.providers.tasks.add(
                user_id=ctx.user_id,
                kind="gift_order",
                run_date=run_day.isoformat(),
                title=f"为{display_name}在{merchant_name}预订{item}（{money:,.0f} 元）",
                payload={
                    "person": display_name,
                    "item": item,
                    "budget": money,
                    "offer_id": offer.offer_id if offer is not None else "",
                    "merchant": merchant_name,
                    "delivery_radius_km": delivery_radius,
                    "source": source,
                    "is_demo": offer.is_demo if offer is not None else True,
                    "note": note,
                    "channel": "同城即时配送",
                },
            )
            if getattr(ctx, "providers", None) is not None
            else store.add_task(
                user_id=ctx.user_id,
                kind="gift_order",
                run_date=run_day.isoformat(),
                title=f"为{display_name}在{merchant_name}预订{item}（{money:,.0f} 元）",
                payload={
                    "person": display_name,
                    "item": item,
                    "budget": money,
                    "offer_id": offer.offer_id if offer is not None else "",
                    "merchant": merchant_name,
                    "delivery_radius_km": delivery_radius,
                    "source": source,
                    "is_demo": offer.is_demo if offer is not None else True,
                    "note": note,
                    "channel": "同城即时配送",
                },
            )
        )
        lines = [
            f"已预约：{run_day.isoformat()} 为「{display_name}」下单{item}，预算 {money:,.2f} 元",
            f"  商户：{merchant_name}（{source_note}）",
            f"  预约单号：{task.task_id}",
            f"  配送方案：同城即时配送，覆盖半径 {delivery_radius:g} 公里（原型为模拟下单）",
        ]
        if note:
            lines.append(f"  贺卡留言：{note}")
        return ToolResult(
            True,
            "\n".join(lines),
            data={"task_id": task.task_id, "run_date": run_day.isoformat(), "item": item, "budget": money},
            summary=f"已预约 {run_day.isoformat()} 为{display_name}预订{item}",
        )


class DailyBriefTool(Tool):
    """每日简报：把多个业务域的信息聚合成一份"今天要看什么"。"""

    name = "crossscene.daily_brief"
    category = "跨场景联动"
    description = "主动汇总今日待办：异常交易、即将扣费、待执行任务与目标进度"
    risk_level = "low"
    params = []
    examples = ["今天有什么要注意的", "帮我做个每日简报"]

    def run(self, ctx: ToolContext) -> ToolResult:  # type: ignore[override]
        store = ctx.store
        today = ctx.clock.today()
        sections: List[str] = []
        summary_bits: List[str] = []

        since = (today - timedelta(days=6)).isoformat()
        recent = (
            ctx.providers.transactions.of(ctx.user_id, since, today.isoformat())
            if getattr(ctx, "providers", None) is not None
            else store.transactions_of(ctx.user_id, since, today.isoformat())
        )
        anomalies = ctx.risk.detect_anomalies(ctx.user_id, recent)
        if anomalies:
            summary_bits.append(f"{len(anomalies)} 笔异常交易")
            rows = [[a.date, a.merchant, format_money(a.amount), a.reason[:30]] for a in anomalies[:4]]
            sections.append("【风险提醒】\n" + render_table(["日期", "商户", "金额", "原因"], rows))
        else:
            sections.append("【风险提醒】近 7 天没有发现异常交易。")

        upcoming = []
        subscriptions = (
            ctx.providers.subscriptions.of(ctx.user_id, "active")
            if getattr(ctx, "providers", None) is not None
            else store.subscriptions_of(ctx.user_id, status="active")
        )
        for sub in subscriptions:
            if not sub.auto_renew:
                continue
            try:
                delta = (date.fromisoformat(sub.next_charge_date) - today).days
            except ValueError:
                continue
            if 0 <= delta <= 7:
                upcoming.append((sub, delta))
        if upcoming:
            summary_bits.append(f"{len(upcoming)} 项订阅即将扣费")
            rows = [[s.merchant, f"{s.amount:,.2f}", s.next_charge_date, f"{d} 天后"] for s, d in upcoming]
            sections.append("【订阅扣费】\n" + render_table(["商户", "金额", "扣费日", "倒计时"], rows))
        else:
            sections.append("【订阅扣费】未来 7 天没有订阅扣费。")

        tasks_source = (
            ctx.providers.tasks.of(ctx.user_id, "pending")
            if getattr(ctx, "providers", None) is not None
            else store.tasks_of(ctx.user_id, status="pending")
        )
        tasks = [t for t in tasks_source if t.run_date >= today.isoformat()]
        if tasks:
            summary_bits.append(f"{len(tasks)} 项待执行任务")
            rows = [[t.run_date, t.title] for t in tasks[:5]]
            sections.append("【待执行计划】\n" + render_table(["日期", "任务"], rows))
        else:
            sections.append("【待执行计划】暂无待执行任务。")

        goals = (
            ctx.providers.goals.of(ctx.user_id, "active")
            if getattr(ctx, "providers", None) is not None
            else store.goals_of(ctx.user_id, status="active")
        )
        if goals:
            rows = [[g.name, f"{g.locked_amount:,.0f}/{g.target_amount:,.0f}", g.deadline or "-"] for g in goals]
            sections.append("【资金目标】\n" + render_table(["目标", "已锁定/目标", "日期"], rows))

        header = f"每日简报（{today.isoformat()}）"
        if summary_bits:
            header += "：" + "、".join(summary_bits)
        message = header + "\n" + "\n\n".join(sections)
        return ToolResult(
            True,
            message,
            data={"anomalies": len(anomalies), "upcoming_subs": len(upcoming), "tasks": len(tasks)},
            summary="；".join(summary_bits) if summary_bits else "今日没有需要特别关注的事项",
        )

