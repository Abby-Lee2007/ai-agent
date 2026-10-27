# Membership Recharge and Cycle Change Design

Date: 2026-10-27

## Summary

Add batch membership purchases for arbitrary platforms and 1-36 month terms,
with per-item auto-renew choices and user-supplied prices. Add a next-cycle
change flow for existing subscriptions so the current term remains unchanged
until its expiration.

All items in one purchase are confirmed together, paid with one account debit,
and fulfilled individually. Prices are user quotes and are not represented as
online-verified platform prices.

## Goals

- Purchase multiple platforms or multiple terms in one instruction.
- Support arbitrary integer terms from 1 to 36 months.
- Keep 1, 3, and 12 months as quick choices.
- Configure auto-renew independently for every item.
- Allow the current purchase price and future renewal price to differ.
- Merge all current purchase prices into one debit.
- Change an existing subscription's next term without changing the current
  term, amount, or expiration date.
- Apply a pending cycle change on the current expiration date.
- Execute auto-renew at expiration using the effective term and renewal price.
- Preserve an auditable record of each order and line item.

## Non-Goals

- Fetching live prices from third-party platforms.
- Verifying that user-provided prices match official platform prices.
- Real payment-network integration.
- Refunds or partial refunds.
- Editing an existing paid order.

## Domain Model

### MembershipOrder

Fields:

- `order_id`
- `user_id`
- `account_id`
- `total_amount`
- `status`
- `created_at`
- `confirmed_at`
- `executed_at`
- `transaction_id`
- `failure_reason`
- `source`
- `updated_at`
- `is_live`
- `is_demo`

Order statuses:

- `completed`: the debit succeeded and every line item was fulfilled.
- `partial_failed`: the debit succeeded but one or more line items failed.
- `failed`: the combined debit failed, so no line item or balance change was
  persisted.
- `cancelled`: reserved for an administrative cancellation after creation.

### MembershipOrderItem

Fields:

- `item_id`
- `order_id`
- `user_id`
- `platform`
- `term_months`: integer from 1 through 36.
- `purchase_price`: amount charged now for this item.
- `auto_renew`: whether this item renews at expiration.
- `renewal_price`: amount charged at each future renewal. Required when
  `auto_renew` is true; zero otherwise.
- `status`
- `subscription_id`
- `result`
- `source`
- `updated_at`
- `is_live`
- `is_demo`

Item statuses:

- `pending`
- `fulfilled`
- `failed`

### Subscription Additions

Add:

- `term_months`: active term length, normalized from the legacy `cycle` field
  for older snapshots.
- `pending_term_months`
- `pending_amount`
- `pending_auto_renew`
- `pending_effective_at`

The existing `cycle` field remains for compatibility and display. New records
set it to `monthly` for 1 month, `yearly` for 12 months, and `custom` for other
terms. `term_months` is the source of truth for date arithmetic.

All user-supplied prices use `source="user:quote"` on the order and items.

## Purchase Interaction

### Intent

`membership.purchase`

Examples:

- "帮我充腾讯视频3个月60元、Keep12个月199元"
- "给米游社充6个月98元，自动续费，续费价每次88元"

### Parsing and Validation

Each line item requires:

- A non-empty platform.
- An integer term from 1 to 36 months.
- A positive current purchase price.
- An explicit auto-renew choice.
- A positive renewal price when auto-renew is enabled.

If required fields are missing, the Agent asks for them. Multiple items are
shown as a numbered draft so the user can supply per-item values.

Duplicate platforms within one order are rejected. Users must combine them into
a single line item.

### Confirmation

The review contains:

- Every platform, term, current price, auto-renew choice, and renewal price.
- The combined total.
- The default debit account and its available balance.
- A statement that prices are user quotes and were not online-verified.

No balance or subscription state changes before confirmation.

### Fulfillment

After one confirmation:

1. Validate the combined total against the debit account.
2. Create one order.
3. Debit the total once and create one transaction with category
   `订阅服务`.
4. Fulfill each line item.
5. Mark the order `completed` or `partial_failed`.

For a new platform, create an active subscription. For an existing platform,
extend from the later of today and the current expiration date. The purchased
term and per-item auto-renew settings replace the active subscription's current
settings. A completed purchase also clears any pending cycle change for the same
platform, because the purchase establishes the current active plan.

If auto-renew is off, the subscription remains active through the purchased
term with `auto_renew=false`. At expiration it becomes expired and does not
renew.

## Cycle Change Interaction

### Intent

`subscription.change_cycle`

Examples:

- "把腾讯视频改成12个月续费，续费价288元"
- "Keep下个周期改成6个月，续费价120元，继续自动续费"

### Rules

- The target term must be an integer from 1 through 36 months.
- The new renewal price must be positive.
- The subscription must currently be active.
- The current term, current price, and current expiration date do not change.
- Confirmation stores the target term, renewal price, auto-renew flag, and
  effective date.
- The effective date defaults to the current expiration date.
- Cancelling the subscription clears any pending cycle change.

The subscription list shows the active plan plus:

`下期：N 个月 / 续费价 / 到期后生效`

## Renewal Lifecycle

The same lifecycle processor runs before handling user messages and when due
scheduled tasks run.

At the subscription expiration date:

1. Apply any pending cycle change.
2. If auto-renew is off, mark the subscription `expired`.
3. If auto-renew is on, attempt to debit one effective term's renewal price.
4. On success, create a subscription transaction, update the last charge date,
   advance expiration by `term_months`, and keep the subscription active.
5. On insufficient balance, do not debit or extend service. Mark the
   subscription `renewal_failed`, disable auto-renew, and create an alert or
   reminder explaining the failure.

Renewal is idempotent because a successful renewal advances the expiration date
and a failed renewal disables automatic retry.

## Persistence

Add `membership_orders` and `membership_order_items` to:

- `DataStore.TABLES`
- `DataStore.MODEL_MAP`
- `DataStore.ID_PREFIX`
- `SQLiteRepository.PROJECTION_TABLES`

Older subscription snapshots must derive `term_months` from `cycle`:

- `yearly` -> 12
- `monthly` -> 1
- `weekly` -> retain the legacy weekly cycle until the user explicitly changes
  it; do not silently convert a weekly plan into a monthly plan
- unknown values -> 1 month

Missing pending-plan fields default to no pending change.

## Testing

Python tests must cover:

- Batch purchase across multiple platforms and terms.
- One combined debit and one order.
- Per-item auto-renew and renewal prices.
- No mutation before confirmation.
- Existing subscriptions extended by the purchased term.
- One-time purchases expiring without renewal.
- Next-cycle changes leaving the current plan untouched.
- Pending cycle changes applying at expiration.
- Auto-renew success using the changed term and price.
- Insufficient balance renewal failure.
- Duplicate-platform validation.
- Order and pending-plan persistence.

UI tests must cover:

- Pending next-cycle details in the subscription table.
- Multiple-item confirmation readability.
- Recovery actions remaining available after expiration or renewal failure.

