"""Bank data boundary used by the demo and future Open Banking adapters.

The rest of the application talks to this narrow read interface instead of
assuming that every account or transaction came from the in-memory seed. A
real deployment can replace ``DemoBankAdapter`` with an Open Banking adapter
without changing the tool layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol

from finagent.data.models import Account, Card, Holding, Subscription, Transaction
from finagent.data.store import DataStore


class BankAdapter(Protocol):
    provider_name: str
    is_sandbox: bool

    def accounts_of(self, user_id: str, kind: Optional[str] = None) -> List[Account]:
        ...

    def transactions_of(
        self,
        user_id: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> List[Transaction]:
        ...

    def cards_of(self, user_id: str, status: Optional[str] = None) -> List[Card]:
        ...

    def subscriptions_of(self, user_id: str, status: Optional[str] = None) -> List[Subscription]:
        ...

    def holdings_of(self, user_id: str, status: Optional[str] = None) -> List[Holding]:
        ...


@dataclass
class DemoBankAdapter(BankAdapter):
    """Sandbox adapter backed by the deterministic demo data store."""

    store: DataStore
    provider_name: str = "demo-bank-sandbox"
    is_sandbox: bool = True

    def accounts_of(self, user_id: str, kind: Optional[str] = None) -> List[Account]:
        return self.store.accounts_of(user_id, kind)

    def transactions_of(
        self,
        user_id: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> List[Transaction]:
        return self.store.transactions_of(user_id, start, end)

    def cards_of(self, user_id: str, status: Optional[str] = None) -> List[Card]:
        return self.store.cards_of(user_id, status)

    def subscriptions_of(self, user_id: str, status: Optional[str] = None) -> List[Subscription]:
        return self.store.subscriptions_of(user_id, status)

    def holdings_of(self, user_id: str, status: Optional[str] = None) -> List[Holding]:
        return self.store.holdings_of(user_id, status)

    def metadata(self) -> dict[str, object]:
        return {
            "provider": self.provider_name,
            "sandbox": self.is_sandbox,
            "source": "agent:simulation",
            "is_demo": True,
        }
