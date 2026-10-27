"""Expiration processing for membership subscriptions."""
from __future__ import annotations

from datetime import date
from typing import List

from finagent.data.store import Clock, DataStore
from finagent.tools.membership_tools import add_months, cycle_for_term


def process_subscription_lifecycle(
    store: DataStore,
    clock: Clock,
    user_id: str,
) -> List[str]:
    """Apply pending plans and renew expired subscriptions once."""
    messages: List[str] = []
    today = clock.today()
    for subscription in store.subscriptions_of(user_id, status="active"):
        try:
            expiry = date.fromisoformat(
                subscription.expires_at or subscription.next_charge_date
            )
        except ValueError:
            continue
        if expiry > today:
            continue

        try:
            pending_at = date.fromisoformat(subscription.pending_effective_at)
        except ValueError:
            pending_at = None
        if (
            subscription.pending_term_months > 0
            and (pending_at is None or pending_at <= today)
        ):
            subscription.term_months = subscription.pending_term_months
            subscription.cycle = cycle_for_term(subscription.term_months)
            subscription.amount = subscription.pending_amount
            subscription.auto_renew = subscription.pending_auto_renew
            subscription.pending_term_months = 0
            subscription.pending_amount = 0.0
            subscription.pending_auto_renew = False
            subscription.pending_effective_at = ""
            messages.append(
                f"{subscription.merchant} 已启用下期 "
                f"{subscription.term_months} 个月服务"
            )

        if not subscription.auto_renew:
            subscription.status = "expired"
            store.touch(subscription)
            messages.append(f"{subscription.merchant} 已到期，未开启自动续费")
            continue

        account = store.primary_account(user_id)
        renewal_amount = round(float(subscription.amount), 2)
        if account is None or account.available < renewal_amount:
            subscription.status = "renewal_failed"
            subscription.auto_renew = False
            store.touch(subscription)
            store.add_alert(
                user_id=user_id,
                level="warning",
                title=f"{subscription.merchant} 自动续费失败",
                detail=(
                    f"需要 {renewal_amount:,.2f} 元，"
                    f"可用余额 {account.available if account else 0:,.2f} 元"
                ),
                source="subscription:renewal",
            )
            messages.append(
                f"{subscription.merchant} 自动续费失败，余额不足，服务未延长"
            )
            continue

        account.balance = round(account.balance - renewal_amount, 2)
        store.touch(account)
        transaction = store.add_transaction(
            user_id=user_id,
            account_id=account.account_id,
            amount=-renewal_amount,
            merchant=subscription.merchant,
            category="订阅服务",
            channel="代扣",
            note=f"{subscription.term_months} 个月自动续费",
        )
        term_months = subscription.term_months or (
            12 if subscription.cycle == "yearly" else 1
        )
        next_expiry = add_months(expiry, term_months)
        subscription.expires_at = next_expiry.isoformat()
        subscription.next_charge_date = next_expiry.isoformat()
        subscription.last_charge_date = today.isoformat()
        subscription.status = "active"
        store.touch(subscription)
        messages.append(
            f"{subscription.merchant} 自动续费 {renewal_amount:,.2f} 元，"
            f"有效期至 {subscription.expires_at}"
        )
    return messages
