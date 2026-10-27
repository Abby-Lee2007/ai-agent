"""Card/account reconciliation helpers shared by all money-moving tools."""
from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from finagent.data.models import Account, Card

if TYPE_CHECKING:
    from finagent.data.store import DataStore

DEMO_CARD_PASSWORD = "246810"
SEED_CARD_ACCOUNTS = {
    "CARD000001": "ACC000003",
    "CARD000002": "ACC000001",
    "CARD000003": "ACC000004",
    "CARD000004": "ACC000002",
}
SEED_CARD_KINDS = {
    "CARD000001": "credit",
    "CARD000002": "debit",
    "CARD000003": "virtual",
    "CARD000004": "debit",
}
SEED_CARD_TAILS = {
    "CARD000001": "8877",
    "CARD000002": "6222",
    "CARD000003": "3011",
    "CARD000004": "6688",
}


def hash_card_password(password: str) -> str:
    """Create a deterministic demo hash without retaining the clear PIN."""
    return hashlib.sha256(f"finagent-demo:{password}".encode("utf-8")).hexdigest()


def verify_card_password(card: Card, password: str) -> bool:
    candidate = str(password or "").strip()
    if not candidate:
        return False
    if card.payment_password_hash:
        return card.payment_password_hash == hash_card_password(candidate)
    return candidate == DEMO_CARD_PASSWORD


def linked_account(store: DataStore, card: Card) -> Account | None:
    account = store.accounts.get(card.account_id) if card.account_id else None
    if account is not None and account.user_id == card.user_id:
        return account
    for candidate in store.accounts_of(card.user_id):
        if candidate.tail != card.tail:
            continue
        if card.kind == "credit" and candidate.kind != "credit":
            continue
        if card.kind != "credit" and candidate.kind == "credit":
            continue
        card.account_id = candidate.account_id
        return candidate
    preferred_kind = (
        "credit" if card.kind == "credit"
        else "wallet" if card.kind == "virtual"
        else "current"
    )
    fallback = next(
        (
            account for account in store.accounts_of(card.user_id)
            if account.kind == preferred_kind
            and account.source != "agent:upload-ledger"
            and account.tail != "BILL"
        ),
        None,
    )
    if fallback is not None:
        card.account_id = fallback.account_id
    return fallback


def sync_card_from_account(store: DataStore, card: Card) -> Account | None:
    account = linked_account(store, card)
    if account is None:
        return None
    if card.kind == "credit":
        card.used = round(max(account.balance, 0.0), 2)
        if card.credit_limit <= 0:
            card.credit_limit = account.credit_limit
    else:
        card.balance = round(account.balance, 2)
    return account


def update_card_balance(
    store: DataStore,
    card: Card,
    new_balance: float,
) -> None:
    """Update a deposit card and its linked account atomically in the demo store."""
    value = round(max(0.0, float(new_balance)), 2)
    card.balance = value
    account = linked_account(store, card)
    if account is not None:
        account.balance = value
        store.touch(account)
    store.touch(card)


def update_credit_usage(store: DataStore, card: Card, new_used: float) -> None:
    value = round(max(0.0, min(float(new_used), card.credit_limit)), 2)
    card.used = value
    account = linked_account(store, card)
    if account is not None:
        account.balance = value
        account.credit_limit = card.credit_limit
        store.touch(account)
    store.touch(card)


def sync_cards_for_account(store: DataStore, account: Account) -> None:
    """Project a changed account balance onto every card linked to it."""
    for card in store.cards.values():
        if card.account_id != account.account_id:
            continue
        if card.kind == "credit":
            card.used = round(max(account.balance, 0.0), 2)
        else:
            card.balance = round(account.balance, 2)
        store.touch(card)


def ensure_card_account(
    store: DataStore,
    card: Card,
    *,
    account_kind: str = "",
    account_name: str = "",
) -> Account:
    account = linked_account(store, card)
    if account is not None:
        return account
    kind = account_kind or ("credit" if card.kind == "credit" else "current")
    account = Account(
        account_id=store.next_id("accounts"),
        user_id=card.user_id,
        kind=kind,
        name=account_name or f"{card.brand} {card.tail} 账户",
        tail=card.tail,
        balance=card.used if card.kind == "credit" else card.balance,
        credit_limit=card.credit_limit if card.kind == "credit" else 0.0,
        source=card.source,
        is_demo=card.is_demo,
    )
    account.updated_at = store.clock.iso()
    store.accounts[account.account_id] = account
    card.account_id = account.account_id
    store.touch(card)
    return account


def card_asset_total(store: DataStore, user_id: str) -> float:
    total = 0.0
    for card in store.cards_of(user_id):
        if not card.is_asset_card or card.status in {"cancelled", "pending"}:
            continue
        sync_card_from_account(store, card)
        total += max(card.balance, 0.0)
    return round(total, 2)


def repair_card_ledger(store: DataStore) -> None:
    """Repair legacy/profile-edited seed cards and backfill missing account cards."""
    for card_id, account_id in SEED_CARD_ACCOUNTS.items():
        card = store.cards.get(card_id)
        account = store.accounts.get(account_id)
        if card is None or account is None:
            continue
        card.account_id = account_id
        card.kind = SEED_CARD_KINDS[card_id]
        card.tail = SEED_CARD_TAILS[card_id]
        card.balance = 0.0 if card.kind == "credit" else account.balance
        card.credit_limit = account.credit_limit if card.kind == "credit" else 0.0
        card.used = account.balance if card.kind == "credit" else 0.0
        if card.status == "pending":
            card.status = "active"
        if not card.payment_password_hash:
            card.payment_password_hash = hash_card_password(DEMO_CARD_PASSWORD)
        store.touch(card)

    linked_asset_accounts = {
        card.account_id for card in store.cards.values()
        if card.is_asset_card and card.account_id
    }
    for account in store.accounts.values():
        if account.kind not in {"current", "savings", "wallet"}:
            continue
        if account.source == "agent:upload-ledger" or account.account_id in linked_asset_accounts:
            continue
        card = Card(
            card_id=store.next_id("cards"),
            user_id=account.user_id,
            kind="virtual" if account.kind == "wallet" else "debit",
            account_id=account.account_id,
            brand="演示银行",
            tail=account.tail,
            status="active" if account.status == "active" else account.status,
            balance=account.balance,
            payment_password_hash=hash_card_password(DEMO_CARD_PASSWORD),
            issued_at=store.clock.iso(),
            note=account.name,
            source=account.source,
            is_demo=account.is_demo,
        )
        store.cards[card.card_id] = card
        store.touch(card)
