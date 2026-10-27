# Membership Recharge and Cycle Change Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add batch membership purchasing with per-item auto-renew pricing and next-cycle subscription changes that take effect after the current term expires.

**Architecture:** Persist one order with multiple line items and debit the combined total once. Extend subscriptions with explicit month terms and pending next-cycle plans. A shared lifecycle processor applies pending plans and executes automatic renewals at expiration.

**Tech Stack:** Python 3 standard library, dataclasses, unittest, SQLite JSON projections, vanilla HTML/CSS/JavaScript, Chrome CDP UI smoke tests.

**Spec:** `docs/superpowers/specs/2026-10-27-membership-recharge-and-cycle-change-design.md`

## Global Constraints

- Support membership terms from 1 through 36 months.
- Keep 1, 3, and 12 months as quick choices.
- Every purchase item has an independent auto-renew setting.
- Current purchase price and future renewal price are user-provided values.
- Prices are marked `user:quote` and are not described as online-verified.
- One confirmed purchase creates one order and one combined account debit.
- Cycle changes never alter the current term, amount, or expiration date.
- Pending cycle changes take effect at expiration.
- A failed automatic renewal does not debit funds or extend service.
- Existing snapshots and SQLite data remain readable.
- Use `python -m unittest` for Python verification.
- Use `node scripts/ui_smoke.mjs` for browser verification.
- Test snippets below show test-method bodies. Put each snippet inside a
  `unittest.TestCase` subclass with a `setUp` that creates the required
  `agent` or `store`.

---

### Task 1: Add Order and Subscription Domain State

**Files:**
- Modify: `finagent/data/models.py`
- Modify: `finagent/data/store.py`
- Modify: `finagent/data/sqlite.py`
- Modify: `finagent/data/__init__.py`
- Create: `tests/test_membership_models.py`

**Interfaces:**
- Consumes: existing `DataStore`, `Subscription`, `build`, `to_dict`.
- Produces:
  - `MembershipOrder`
  - `MembershipOrderItem`
  - `Subscription.term_months: int`
  - `Subscription.pending_term_months: int`
  - `Subscription.pending_amount: float`
  - `Subscription.pending_auto_renew: bool`
  - `Subscription.pending_effective_at: str`
  - `DataStore.add_membership_order(...) -> MembershipOrder`
  - `DataStore.add_membership_order_item(...) -> MembershipOrderItem`
  - `DataStore.membership_orders_of(user_id) -> List[MembershipOrder]`
  - `DataStore.membership_order_items_of(order_id) -> List[MembershipOrderItem]`

- [ ] **Step 1: Write the failing persistence test**

```python
from tests.support import make_store


def test_membership_order_round_trips_through_snapshot():
    store = make_store()
    order = store.add_membership_order(
        user_id="u_001",
        account_id="ACC000001",
        total_amount=159.0,
        status="completed",
    )
    item = store.add_membership_order_item(
        order_id=order.order_id,
        user_id="u_001",
        platform="米游社",
        term_months=6,
        purchase_price=159.0,
        auto_renew=False,
        renewal_price=0.0,
        status="fulfilled",
    )

    restored = make_store()
    restored.load_snapshot(store.snapshot())

    assert restored.membership_orders[order.order_id].total_amount == 159.0
    assert restored.membership_order_items[item.item_id].term_months == 6
    assert restored.membership_order_items[item.item_id].platform == "米游社"
```

- [ ] **Step 2: Run and verify RED**

Run: `python -m unittest tests.test_membership_models -v`

Expected: FAIL because `MembershipOrder` and `DataStore.add_membership_order` do not exist.

- [ ] **Step 3: Add dataclasses and storage methods**

Define `MembershipOrder` and `MembershipOrderItem` in `models.py` with the fields in the spec. Add `term_months=1` and the four pending-plan fields to `Subscription`.

Add `membership_orders` and `membership_order_items` to `TABLES`, `MODEL_MAP`, and `ID_PREFIX` in `store.py`. Add methods:

```python
def add_membership_order(
    self,
    user_id: str,
    account_id: str,
    total_amount: float,
    status: str = "completed",
    transaction_id: str = "",
) -> MembershipOrder:
    ...

def add_membership_order_item(
    self,
    order_id: str,
    user_id: str,
    platform: str,
    term_months: int,
    purchase_price: float,
    auto_renew: bool,
    renewal_price: float,
    status: str = "pending",
) -> MembershipOrderItem:
    ...
```

Set `source="user:quote"` on both objects.

- [ ] **Step 4: Normalize legacy subscriptions**

In `DataStore.load_snapshot`, derive `term_months` when absent:

```python
if not subscription.term_months:
    subscription.term_months = {
        "yearly": 12,
        "monthly": 1,
    }.get(subscription.cycle, 0 if subscription.cycle == "weekly" else 1)
```

Do not silently convert legacy weekly subscriptions.

- [ ] **Step 5: Add SQLite projections**

Add:

```python
"membership_orders": ("order_id", "membership_orders"),
"membership_order_items": ("item_id", "membership_order_items"),
```

to `PROJECTION_TABLES`.

- [ ] **Step 6: Run tests and commit**

Run: `python -m unittest tests.test_membership_models tests.test_sqlite_persistence -v`

Expected: PASS.

```bash
git add finagent/data/models.py finagent/data/store.py finagent/data/sqlite.py finagent/data/__init__.py tests/test_membership_models.py
git commit -m "feat: add membership order domain state"
```

---

### Task 2: Parse and Validate Batch Membership Input

**Files:**
- Create: `finagent/nlu/membership_parser.py`
- Create: `tests/test_membership_nlu.py`

**Interfaces:**
- Produces:
  - `MembershipDraftItem(platform, term_months, purchase_price, auto_renew, renewal_price)`
  - `parse_membership_items(text: str) -> List[MembershipDraftItem]`
  - `apply_membership_options(items: list[dict], text: str) -> list[dict]`
  - `validate_membership_items(items: list[dict]) -> list[str]`

- [ ] **Step 1: Write the failing parser tests**

```python
from finagent.nlu.membership_parser import parse_membership_items


def test_parse_multiple_membership_items():
    items = parse_membership_items(
        "帮我充腾讯视频3个月60元、Keep12个月199元"
    )
    assert [(item.platform, item.term_months, item.purchase_price) for item in items] == [
        ("腾讯视频", 3, 60.0),
        ("Keep", 12, 199.0),
    ]


def test_parse_six_month_custom_platform_with_renewal_price():
    items = parse_membership_items(
        "给米游社充6个月98元，自动续费，续费价88元"
    )
    assert items[0].term_months == 6
    assert items[0].auto_renew is True
    assert items[0].renewal_price == 88.0
```

- [ ] **Step 2: Run and verify RED**

Run: `python -m unittest tests.test_membership_nlu -v`

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement the parser**

Split item text on semicolons and commas following `元`. Parse:

```python
TERM_RE = re.compile(r"(\d+)\s*(个月|月|年)")
PRICE_RE = re.compile(r"(?:本次|价格|价格是)?\s*(\d+(?:\.\d{1,2})?)\s*元")
RENEWAL_RE = re.compile(r"续费价(?:格)?\s*(?:每次|每月|每年)?\s*(\d+(?:\.\d{1,2})?)\s*元")
```

Convert years to 12 months. Require the first non-renewal amount as `purchase_price`. Detect `不自动续费` before `自动续费`.

- [ ] **Step 4: Implement numbered option updates**

`apply_membership_options` accepts:

```text
1自动续费，续费价25元；2不自动续费
都自动续费，续费价同本次价格
都不自动续费
```

Update each item's `auto_renew` and `renewal_price`. When the response says “同本次价格”, set renewal price equal to purchase price.

- [ ] **Step 5: Implement validation**

Reject:

- Blank platform.
- Term outside 1–36.
- Non-positive purchase price.
- Missing auto-renew choice.
- Auto-renew enabled with non-positive renewal price.
- Duplicate normalized platform names in one order.

- [ ] **Step 6: Run tests and commit**

Run: `python -m unittest tests.test_membership_nlu -v`

Expected: PASS.

```bash
git add finagent/nlu/membership_parser.py tests/test_membership_nlu.py
git commit -m "feat: parse batch membership purchases"
```

---

### Task 3: Execute One Combined Membership Purchase

**Files:**
- Create: `finagent/tools/membership_tools.py`
- Modify: `finagent/tools/__init__.py`
- Modify: `finagent/agent/registry.py`
- Create: `tests/test_membership_purchase.py`

**Interfaces:**
- Consumes: `MembershipOrder`, `MembershipOrderItem`, `DataStore.add_membership_order`, `DataStore.add_membership_order_item`.
- Produces: `MembershipPurchaseTool`, tool name `membership.purchase`, accepting `items: list[dict]`.

- [ ] **Step 1: Write the failing purchase test**

```python
def test_batch_purchase_creates_one_debit_and_two_subscriptions():
    agent = make_agent()
    before = len(agent.store.transactions_of("u_001"))

    tool = agent.registry.get("membership.purchase")
    reply = tool.run(
        agent.context(),
        items=[
            {"platform": "米游社", "term_months": 6, "purchase_price": 98,
             "auto_renew": True, "renewal_price": 88},
            {"platform": "Lofter", "term_months": 3, "purchase_price": 30,
             "auto_renew": False, "renewal_price": 0},
        ],
    )

    assert reply.ok
    assert len(agent.store.transactions_of("u_001")) == before + 1
    assert len(agent.store.membership_orders_of("u_001")) == 1
    assert len(agent.store.membership_order_items_of(
        agent.store.membership_orders_of("u_001")[0].order_id
    )) == 2
```

- [ ] **Step 2: Run and verify RED**

Run: `python -m unittest tests.test_membership_purchase -v`

Expected: FAIL because the tool is not registered.

- [ ] **Step 3: Implement the tool**

The tool must:

1. Normalize and validate all items.
2. Sum `purchase_price` values.
3. Resolve the default current account.
4. Reject insufficient balance before mutation.
5. Debit the account once.
6. Add one transaction with category `订阅服务`.
7. Create one order.
8. Fulfill every item.

Expose this result shape:

```python
ToolResult(
    True,
    message,
    data={
        "order_id": order.order_id,
        "transaction_id": transaction.txn_id,
        "total": order.total_amount,
        "items": fulfilled_items,
    },
)
```

- [ ] **Step 4: Apply purchased terms**

For each platform:

- Reuse an existing active subscription.
- Extend from `max(today, current expires_at)`.
- Set `term_months`, `amount`, `auto_renew`, `expires_at`, and
  `next_charge_date`.
- Clear `pending_*` fields because the purchase establishes the current plan.
- Set `cycle` to `monthly`, `yearly`, or `custom`.

- [ ] **Step 5: Run tests and commit**

Run: `python -m unittest tests.test_membership_purchase -v`

Expected: PASS.

```bash
git add finagent/tools/membership_tools.py finagent/tools/__init__.py finagent/agent/registry.py tests/test_membership_purchase.py
git commit -m "feat: execute combined membership purchases"
```

---

### Task 4: Add Next-Cycle Subscription Changes

**Files:**
- Modify: `finagent/tools/membership_tools.py`
- Modify: `finagent/tools/subscription_tools.py`
- Modify: `finagent/tools/__init__.py`
- Modify: `finagent/agent/registry.py`
- Modify: `tests/test_subscription.py`

**Interfaces:**
- Produces: `SubscriptionChangeCycleTool`, tool name `subscription.change_cycle`.
- Params: `merchant`, `term_months`, `renewal_price`, `auto_renew=True`.

- [ ] **Step 1: Write the failing cycle-change test**

```python
def test_change_cycle_only_updates_pending_plan():
    agent = make_agent()
    before = next(
        s for s in agent.store.subscriptions_of("u_001")
        if s.merchant == "腾讯视频VIP"
    )
    current_expiry = before.expires_at
    current_amount = before.amount

    run_to_completion(
        agent,
        agent.handle("把腾讯视频改成12个月续费，续费价288元"),
    )

    after = next(
        s for s in agent.store.subscriptions_of("u_001")
        if s.merchant == "腾讯视频VIP"
    )
    assert after.expires_at == current_expiry
    assert after.amount == current_amount
    assert after.pending_term_months == 12
    assert after.pending_amount == 288
    assert after.pending_auto_renew is True
    assert after.pending_effective_at == current_expiry
```

- [ ] **Step 2: Run and verify RED**

Run: `python -m unittest tests.test_subscription.TestSubscription.test_change_cycle_only_updates_pending_plan -v`

Expected: FAIL because the intent or tool does not exist.

- [ ] **Step 3: Implement the tool**

Find only active subscriptions. Validate term and price. Set:

```python
target.pending_term_months = term_months
target.pending_amount = renewal_price
target.pending_auto_renew = auto_renew
target.pending_effective_at = target.expires_at or target.next_charge_date
```

Do not mutate `term_months`, `amount`, `status`, `expires_at`, or
`next_charge_date`.

- [ ] **Step 4: Clear pending changes on cancellation**

In `SubscriptionCancelTool.run`, clear all `pending_*` fields before saving.

- [ ] **Step 5: Run tests and commit**

Run: `python -m unittest tests.test_subscription -v`

Expected: PASS.

```bash
git add finagent/tools/membership_tools.py finagent/tools/subscription_tools.py finagent/tools/__init__.py finagent/agent/registry.py tests/test_subscription.py
git commit -m "feat: schedule subscription cycle changes"
```

---

### Task 5: Process Expiration and Automatic Renewal

**Files:**
- Create: `finagent/agent/subscription_lifecycle.py`
- Modify: `finagent/agent/core.py`
- Modify: `finagent/agent/scheduler.py`
- Create: `tests/test_subscription_lifecycle.py`

**Interfaces:**
- Produces: `process_subscription_lifecycle(store, clock, user_id) -> list[str]`

- [ ] **Step 1: Write failing lifecycle tests**

```python
def test_pending_cycle_applies_before_auto_renewal():
    agent = make_agent()
    sub = next(
        s for s in agent.store.subscriptions_of("u_001")
        if s.merchant == "Keep会员"
    )
    sub.pending_term_months = 6
    sub.pending_amount = 100.0
    sub.pending_auto_renew = True
    sub.pending_effective_at = sub.expires_at
    agent.clock.advance(days=2)

    agent.handle("我的订阅都有什么")

    refreshed = next(
        s for s in agent.store.subscriptions_of("u_001")
        if s.merchant == "Keep会员"
    )
    assert refreshed.term_months == 6
    assert refreshed.amount == 100.0
    assert refreshed.expires_at > sub.expires_at
```

```python
def test_renewal_failure_does_not_extend_or_debit():
    agent = make_agent()
    account = agent.store.primary_account("u_001")
    sub = agent.store.subscriptions_of("u_001")[0]
    sub.auto_renew = True
    sub.amount = account.available + 1
    sub.expires_at = "2026-10-15"
    sub.next_charge_date = sub.expires_at
    agent.clock.advance(days=2)
    before_balance = account.balance

    agent.handle("我的订阅都有什么")

    assert sub.status == "renewal_failed"
    assert sub.auto_renew is False
    assert account.balance == before_balance
```

- [ ] **Step 2: Run and verify RED**

Run: `python -m unittest tests.test_subscription_lifecycle -v`

Expected: FAIL because expiration processing does not exist.

- [ ] **Step 3: Implement lifecycle processing**

For each active subscription whose `expires_at <= today`:

1. Apply pending plan fields when `pending_effective_at <= today`.
2. If auto-renew is false, set `status="expired"`.
3. If auto-renew is true, check the effective renewal amount.
4. On success, debit, create a `订阅服务` transaction, update
   `last_charge_date`, add `term_months` to expiration, and keep active.
5. On failure, set `status="renewal_failed"` and `auto_renew=False`.

- [ ] **Step 4: Integrate lifecycle processing**

At the start of `BankingAgent.handle`, call:

```python
process_subscription_lifecycle(self.store, self.clock, self.user_id)
```

Also call it from `Scheduler.run_due`.

- [ ] **Step 5: Run tests and commit**

Run: `python -m unittest tests.test_subscription_lifecycle tests.test_subscription -v`

Expected: PASS.

```bash
git add finagent/agent/subscription_lifecycle.py finagent/agent/core.py finagent/agent/scheduler.py tests/test_subscription_lifecycle.py
git commit -m "feat: process membership expiration and renewals"
```

---

### Task 6: Wire Batch Purchase and Cycle Change into Dialogue

**Files:**
- Modify: `finagent/nlu/intent_parser.py`
- Modify: `finagent/nlu/intent_schema.py`
- Modify: `finagent/agent/planner.py`
- Modify: `finagent/agent/core.py`
- Modify: `finagent/risk/engine.py`
- Modify: `tests/test_nlu.py`
- Modify: `tests/test_confirmations.py`

**Interfaces:**
- Consumes: `parse_membership_items`, `apply_membership_options`, `validate_membership_items`.
- Produces intents:
  - `membership.purchase`
  - `subscription.change_cycle`

- [ ] **Step 1: Write failing intent and dialogue tests**

```python
def test_membership_purchase_parses_multiple_items():
    intent = IntentParser(store=make_store(), user_id="u_001", mode="off").parse(
        "帮我充腾讯视频3个月60元、Keep12个月199元"
    )
    assert intent.name == "membership.purchase"
    assert len(intent.slots["items"]) == 2
```

```python
def test_membership_purchase_asks_per_item_auto_renew():
    agent = make_agent()
    reply = agent.handle("帮我充腾讯视频3个月60元、Keep12个月199元")
    assert reply.kind == "question"
    assert "自动续费" in reply.text
    assert "1." in reply.text and "2." in reply.text

    reply = agent.handle("1自动续费，续费价25元；2不自动续费")
    assert reply.kind == "confirm"
    assert "合计" in reply.text
    assert "259.00" in reply.text
```

- [ ] **Step 2: Run and verify RED**

Run: `python -m unittest tests.test_nlu tests.test_confirmations -v`

Expected: FAIL because the new intents are not parsed.

- [ ] **Step 3: Add intent and planner mappings**

Add both intents to `INTENTS`, parser rules, `SIMPLE_PLANS`, and
`DATA_CONFIRM_INTENTS`.

Add `MembershipPurchaseTool` and `SubscriptionChangeCycleTool` to the default
registry.

- [ ] **Step 4: Add membership-specific multi-turn handling**

In `BankingAgent`, handle two pending kinds:

- `membership_options`: ask numbered auto-renew choices.
- `membership_renewal_price`: ask missing renewal prices for auto-renew items.

On each response, update `pending.slots["items"]` and rebuild the plan when all
items validate.

- [ ] **Step 5: Add confirmation descriptions**

The batch confirmation must list every item, auto-renew choice, renewal price,
combined total, debit account, and the user-quote disclaimer.

The cycle-change confirmation must show:

- Current plan and expiry.
- New term and renewal price.
- “到期后生效，本周期不扣款”.

- [ ] **Step 6: Run tests and commit**

Run: `python -m unittest tests.test_nlu tests.test_confirmations -v`

Expected: PASS.

```bash
git add finagent/nlu/intent_parser.py finagent/nlu/intent_schema.py finagent/agent/planner.py finagent/agent/core.py finagent/risk/engine.py tests/test_nlu.py tests/test_confirmations.py
git commit -m "feat: add membership purchase dialogue"
```

---

### Task 7: Show Pending Plans and Renewal Failures in the UI

**Files:**
- Modify: `finagent/tools/subscription_tools.py`
- Modify: `web/index.html`
- Modify: `scripts/ui_smoke.mjs`
- Modify: `tests/test_subscription.py`

**Interfaces:**
- Consumes: subscription `pending_*` fields.
- Produces: subscription-list item fields:
  - `pending_term_months`
  - `pending_amount`
  - `pending_auto_renew`
  - `pending_effective_at`

- [ ] **Step 1: Write the failing data test**

```python
def test_subscription_list_exposes_pending_plan():
    agent = make_agent()
    run_to_completion(
        agent,
        agent.handle("把腾讯视频改成12个月续费，续费价288元"),
    )
    reply = agent.handle("我的订阅都有什么")
    row = next(
        item for item in reply.data["items"]
        if item["merchant"] == "腾讯视频VIP"
    )
    assert row["pending_term_months"] == 12
    assert row["pending_amount"] == 288
    assert row["pending_effective_at"] == "2026-11-08"
```

- [ ] **Step 2: Run and verify RED**

Run: `python -m unittest tests.test_subscription.TestSubscription.test_subscription_list_exposes_pending_plan -v`

Expected: FAIL because the list data omits pending fields.

- [ ] **Step 3: Add pending fields to list output**

Include the four pending fields in `SubscriptionListTool` data.
Add a pending-plan note to the text table.

- [ ] **Step 4: Render pending plans in the HTML table**

Under the renewal cell show:

```text
下期：12 个月 / ¥ 288.00
2026-11-08 生效
```

For `renewal_failed`, show a red status and keep a recovery action.

- [ ] **Step 5: Extend UI smoke assertions**

Assert:

- Pending plan text appears in the subscription row.
- It does not overwrite current expiry or current price.
- The mobile table remains free of page-level horizontal overflow.

- [ ] **Step 6: Run tests and commit**

Run:

```bash
python -m unittest tests.test_subscription -v
node scripts/ui_smoke.mjs http://127.0.0.1:8000 ui-artifacts
```

Expected: PASS and screenshots written to `ui-artifacts`.

```bash
git add finagent/tools/subscription_tools.py web/index.html scripts/ui_smoke.mjs tests/test_subscription.py
git commit -m "feat: show pending membership plans"
```

---

### Task 8: Full Regression and Documentation

**Files:**
- Modify: `README.md`
- Modify: `USER_GUIDE.md`
- Verify: all Python tests
- Verify: UI smoke test

**Interfaces:**
- Consumes: completed feature.
- Produces: documented behavior and final verified build.

- [ ] **Step 1: Document the commands**

Add examples:

```text
帮我充腾讯视频3个月60元、Keep12个月199元
把腾讯视频改成12个月续费，续费价288元
```

Document that prices are user quotes and that cycle changes take effect after
the current term.

- [ ] **Step 2: Run the full Python test suite**

Run: `python -m unittest discover -s tests`

Expected: all tests pass; record the exact test count and skip count.

- [ ] **Step 3: Run the UI smoke test**

Run: `node scripts/ui_smoke.mjs http://127.0.0.1:8000 ui-artifacts`

Expected: `"ok": true`.

- [ ] **Step 4: Run static checks**

Run:

```bash
python -m compileall -q finagent tests
git diff --check
```

Expected: exit code 0 for both commands.

- [ ] **Step 5: Commit documentation**

```bash
git add README.md USER_GUIDE.md
git commit -m "docs: document membership recharge flows"
```

