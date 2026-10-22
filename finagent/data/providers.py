"""Unified read interfaces for domain data.

Business tools receive this registry instead of reaching into unrelated store
collections directly. Demo and live providers can therefore be swapped without
changing the tool contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

from finagent.data.bank_adapter import BankAdapter, DemoBankAdapter
from finagent.data.models import (
    Account,
    Card,
    Goal,
    Holding,
    MerchantOffer,
    Payee,
    ScheduledTask,
    Subscription,
    UploadBatch,
    WealthProduct,
)
from finagent.data.store import DataStore


class AccountProvider:
    def __init__(self, store: DataStore, bank: Optional[BankAdapter] = None) -> None:
        self.store = store
        self.bank = bank

    def of(self, user_id: str, kind: Optional[str] = None) -> List[Account]:
        if self.bank is not None:
            return self.bank.accounts_of(user_id, kind)
        return self.store.accounts_of(user_id, kind)

    def resolve(self, user_id: str, keyword: str = ""):
        key = (keyword or "").strip()
        if not key:
            return self.default_for_payment(user_id)
        mapping = {
            "信用": "credit", "信用卡": "credit",
            "活期": "current", "储蓄": "savings", "定期": "savings",
            "钱包": "wallet", "零钱": "wallet",
        }
        for label, kind in mapping.items():
            if label in key:
                found = self.of(user_id, kind)
                if found:
                    return found[0]
        digits = "".join(ch for ch in key if ch.isdigit())
        for account in self.of(user_id):
            if key in account.name or (digits and account.tail == digits[-4:]):
                return account
        return None

    def get(self, user_id: str, account_id: str) -> Optional[Account]:
        return next(
            (account for account in self.of(user_id) if account.account_id == account_id),
            None,
        )

    def default_for_payment(self, user_id: str) -> Optional[Account]:
        """The only place where the demo's default payment account is chosen."""
        for kind in ("current", "wallet", "savings"):
            found = [
                account for account in self.of(user_id, kind)
                if account.source != "agent:upload-ledger"
            ]
            if found:
                return found[0]
        accounts = [
            account for account in self.of(user_id)
            if account.source != "agent:upload-ledger"
        ]
        return accounts[0] if accounts else None

    def upload_balance(self, user_id: str) -> Account:
        """Return the virtual ledger account used for unspecified bill payments."""
        for account in self.of(user_id):
            if account.name == "上传账单余额":
                return account
        account = Account(
            account_id="ACC_UPLOAD_LEDGER",
            user_id=user_id,
            kind="wallet",
            name="上传账单余额",
            tail="BILL",
            balance=0.0,
            source="agent:upload-ledger",
            is_demo=False,
        )
        account.updated_at = self.store.clock.iso()
        self.store.accounts[account.account_id] = account
        return account


class TransactionProvider:
    def __init__(self, store: DataStore, bank: Optional[BankAdapter] = None) -> None:
        self.store = store
        self.bank = bank

    def of(self, user_id: str, start: str = "", end: str = ""):
        if self.bank is not None:
            return self.bank.transactions_of(user_id, start or None, end or None)
        return self.store.transactions_of(user_id, start or None, end or None)

    def add(self, **kwargs: Any):
        """Write a simulated transaction through the repository boundary."""
        return self.store.add_transaction(**kwargs)


class PayeeProvider:
    def __init__(self, store: DataStore) -> None:
        self.store = store

    def of(self, user_id: str) -> List[Payee]:
        self._sync_care_people(user_id)
        return self.store.payees_of(user_id)

    def find(self, user_id: str, keyword: str) -> Optional[Payee]:
        self._sync_care_people(user_id)
        return self.store.find_payee(user_id, keyword)

    def remember(self, user_id: str, **kwargs: Any) -> Payee:
        return self.store.remember_payee(user_id, **kwargs)

    def _sync_care_people(self, user_id: str) -> None:
        """Project profile care people into transfer payees."""
        user = self.store.get_user(user_id)
        if user is None:
            return
        care_people = (user.preferences or {}).get("care_people") or []
        if not isinstance(care_people, list):
            return
        for index, item in enumerate(care_people):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            person_id = str(item.get("person_id") or f"CARE{index + 1:04d}")
            phone = "".join(ch for ch in str(item.get("phone") or "") if ch.isdigit())
            bank_card = "".join(
                ch for ch in str(item.get("bank_card") or "") if ch.isdigit()
            )
            wechat = str(item.get("wechat") or "").strip()[:100]
            alipay = str(item.get("alipay") or "").strip()[:100]
            payee = next(
                (
                    candidate for candidate in self.store.payees_of(user_id)
                    if candidate.profile_person_id == person_id
                ),
                None,
            )
            if payee is None:
                payee = next(
                    (
                        candidate for candidate in self.store.payees_of(user_id)
                        if candidate.name == name
                    ),
                    None,
                )
            if payee is None:
                payee = Payee(
                    payee_id=self.store.next_id("payees"),
                    user_id=user_id,
                    name=name,
                )
                payee.updated_at = self.store.clock.iso()
                self.store.payees[payee.payee_id] = payee

            payee.name = name[:40]
            payee.relation = str(item.get("relation") or payee.relation or "关心的人")[:20]
            payee.profile_person_id = person_id
            if phone:
                payee.phone = phone
                payee.wallet_phone = phone
            if wechat:
                payee.wechat = wechat
            if alipay:
                payee.alipay = alipay
            if bank_card:
                payee.tail = bank_card[-4:]
            payee.source = "user:profile"
            payee.is_demo = False
            payee.updated_at = self.store.clock.iso()
            self.store.touch(payee)


class CardProvider:
    def __init__(self, store: DataStore, bank: Optional[BankAdapter] = None) -> None:
        self.store = store
        self.bank = bank

    def of(self, user_id: str, status: Optional[str] = None) -> List[Card]:
        if self.bank is not None:
            return self.bank.cards_of(user_id, status)
        return self.store.cards_of(user_id, status)

    def find(self, user_id: str, keyword: str = "") -> Optional[Card]:
        cards = self.of(user_id)
        key = (keyword or "").strip()
        if not cards:
            return None
        if not key:
            return next(
                (card for card in cards if card.kind == "credit" and card.status == "active"),
                cards[0],
            )
        digits = "".join(ch for ch in key if ch.isdigit())
        aliases = {
            "信用卡": "credit",
            "信用": "credit",
            "借记卡": "debit",
            "储蓄卡": "debit",
            "虚拟卡": "virtual",
            "附属卡": "supplementary",
        }
        for label, kind in aliases.items():
            if label in key:
                return next((card for card in cards if card.kind == kind), None)
        if digits:
            return next((card for card in cards if card.tail == digits[-4:]), None)
        return next((card for card in cards if key in card.note), None)


class WealthProvider:
    def __init__(self, store: DataStore, bank: Optional[BankAdapter] = None) -> None:
        self.store = store
        self.bank = bank

    def products(self) -> List[WealthProduct]:
        return list(self.store.products.values())

    def products_by_risk(self, max_risk: int) -> List[WealthProduct]:
        return self.store.products_by_risk(max_risk)

    def find(self, keyword: str) -> Optional[WealthProduct]:
        return self.store.find_product(keyword)

    def holdings(self, user_id: str, status: Optional[str] = None) -> List[Holding]:
        if self.bank is not None:
            return self.bank.holdings_of(user_id, status)
        return self.store.holdings_of(user_id, status)

    def get(self, product_id: str) -> Optional[WealthProduct]:
        return self.store.products.get(product_id)


class SubscriptionProvider:
    def __init__(self, store: DataStore, bank: Optional[BankAdapter] = None) -> None:
        self.store = store
        self.bank = bank

    def of(self, user_id: str, status: str = "active") -> List[Subscription]:
        if self.bank is not None:
            return self.bank.subscriptions_of(user_id, status or None)
        return self.store.subscriptions_of(user_id, status or None)

    def find(self, user_id: str, keyword: str) -> Optional[Subscription]:
        key = (keyword or "").strip()
        for subscription in self.of(user_id):
            if key and (key in subscription.merchant or subscription.merchant in key):
                return subscription
        return None


class MerchantOfferProvider:
    """Find an available merchant offer by item, budget, city, and date."""

    def __init__(self, store: DataStore) -> None:
        self.store = store

    def find_offer(
        self,
        item: str,
        budget: Optional[float],
        delivery_date: str,
        city: str = "深圳",
    ) -> Optional[MerchantOffer]:
        candidates = []
        for offer in self.store.merchant_offers.values():
            if item not in offer.item and offer.item not in item:
                continue
            if offer.inventory <= 0:
                continue
            if city not in offer.delivery_cities:
                continue
            if offer.available_from and delivery_date < offer.available_from:
                continue
            if offer.available_to and delivery_date > offer.available_to:
                continue
            if budget is not None and offer.price > float(budget):
                continue
            candidates.append(offer)
        return sorted(candidates, key=lambda offer: offer.price)[0] if candidates else None


class TaskProvider:
    def __init__(self, store: DataStore) -> None:
        self.store = store

    def of(self, user_id: str, status: Optional[str] = None) -> List[ScheduledTask]:
        return self.store.tasks_of(user_id, status)

    def due(self, user_id: str, on_date: Optional[str] = None) -> List[ScheduledTask]:
        return self.store.due_tasks(user_id, on_date)

    def add(self, **kwargs: Any) -> ScheduledTask:
        return self.store.add_task(**kwargs)


class GoalProvider:
    def __init__(self, store: DataStore) -> None:
        self.store = store

    def of(self, user_id: str, status: Optional[str] = None) -> List[Goal]:
        return self.store.goals_of(user_id, status)

    def add(self, **kwargs: Any) -> Goal:
        return self.store.add_goal(**kwargs)


class UploadBatchProvider:
    def __init__(self, store: DataStore) -> None:
        self.store = store

    def of(self, user_id: str) -> List[UploadBatch]:
        return self.store.upload_batches_of(user_id)

    def add(self, **kwargs: Any) -> UploadBatch:
        return self.store.add_upload_batch(**kwargs)

    def delete(self, user_id: str, batch_id: str) -> Dict[str, Any]:
        return self.store.delete_upload_batch(user_id, batch_id)


@dataclass
class DataProviderRegistry:
    bank: BankAdapter
    accounts: AccountProvider
    transactions: TransactionProvider
    payees: PayeeProvider
    cards: CardProvider
    wealth: WealthProvider
    subscriptions: SubscriptionProvider
    merchants: MerchantOfferProvider
    tasks: TaskProvider
    goals: GoalProvider
    uploads: UploadBatchProvider
    market: Any = None


def build_data_providers(store: DataStore, market: Any = None) -> DataProviderRegistry:
    bank = DemoBankAdapter(store)
    return DataProviderRegistry(
        bank=bank,
        accounts=AccountProvider(store, bank),
        transactions=TransactionProvider(store, bank),
        payees=PayeeProvider(store),
        cards=CardProvider(store, bank),
        wealth=WealthProvider(store, bank),
        subscriptions=SubscriptionProvider(store, bank),
        merchants=MerchantOfferProvider(store),
        tasks=TaskProvider(store),
        goals=GoalProvider(store),
        uploads=UploadBatchProvider(store),
        market=market,
    )
