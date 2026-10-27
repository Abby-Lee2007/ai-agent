"""数据存储层。

设计目标：
1. 用一张"内存字典表"承载全部业务数据，读写是 O(1) 级别；
2. 提供面向业务的查询方法（按时间取流水、按人名找收款人……），
   业务工具层不需要关心底层结构；
3. 支持整体快照落盘 / 加载，方便演示"关机再打开数据还在"；
4. 通过 `SQLiteRepository` 把账户、持仓、任务和审计日志投影到查询表。

注意：领域数据仍在进程内运行，SQLite 负责可恢复的持久化投影；
真实银行系统必须另做并发、事务、加密和 Open Banking 对接设计。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from finagent.data import seed as seed_module
from finagent.data.subscription_alias import deduplicate_subscriptions
from finagent.data.models import (
    Account,
    AaSheet,
    Alert,
    Card,
    Goal,
    Holding,
    MembershipOrder,
    MembershipOrderItem,
    MerchantOffer,
    Payee,
    ScheduledTask,
    Subscription,
    Transaction,
    UploadBatch,
    User,
    WealthProduct,
    build,
    to_dict,
)


class Clock:
    """可控时钟。

    演示"定时转账""生日前 2 天下单"这类未来动作时，
    可以用 `set()` / `advance()` 把时间快进，不需要真的等到那一天。
    """

    def __init__(self, fixed: Optional[datetime] = None) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed if self._fixed is not None else datetime.now()

    def today(self) -> date:
        return self.now().date()

    def set(self, moment: datetime) -> None:
        self._fixed = moment

    def advance(self, days: int = 0, hours: int = 0, minutes: int = 0) -> datetime:
        self._fixed = self.now() + timedelta(days=days, hours=hours, minutes=minutes)
        return self._fixed

    def reset(self) -> None:
        self._fixed = None

    def iso(self) -> str:
        return self.now().strftime("%Y-%m-%dT%H:%M:%S")

    def date_str(self) -> str:
        return self.today().isoformat()


class DataStore:
    """内存数据仓库（可选 JSON 落盘）。"""

    TABLES = (
        "users",
        "accounts",
        "transactions",
        "payees",
        "cards",
        "subscriptions",
        "membership_orders",
        "membership_order_items",
        "products",
        "holdings",
        "merchant_offers",
        "upload_batches",
        "tasks",
        "goals",
        "aa_sheets",
        "alerts",
    )

    MODEL_MAP = {
        "users": User,
        "accounts": Account,
        "transactions": Transaction,
        "payees": Payee,
        "cards": Card,
        "subscriptions": Subscription,
        "membership_orders": MembershipOrder,
        "membership_order_items": MembershipOrderItem,
        "products": WealthProduct,
        "holdings": Holding,
        "merchant_offers": MerchantOffer,
        "upload_batches": UploadBatch,
        "tasks": ScheduledTask,
        "goals": Goal,
        "aa_sheets": AaSheet,
        "alerts": Alert,
    }

    ID_PREFIX = {
        "accounts": "ACC",
        "transactions": "TXN",
        "payees": "PAY",
        "cards": "CARD",
        "subscriptions": "SUB",
        "membership_orders": "MORD",
        "membership_order_items": "MITEM",
        "holdings": "HLD",
        "merchant_offers": "OFFER",
        "upload_batches": "UPL",
        "tasks": "TASK",
        "goals": "GOAL",
        "aa_sheets": "AA",
        "alerts": "ALERT",
    }

    def __init__(self, clock: Optional[Clock] = None, path: Optional[Path] = None) -> None:
        self.clock = clock or Clock()
        self.path = Path(path) if path else None
        self.audit: List[Dict[str, Any]] = []
        self.counters: Dict[str, int] = {}
        for table in self.TABLES:
            setattr(self, table, {})

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------
    def next_id(self, table: str) -> str:
        """生成形如 TXN000123 的业务主键。"""
        prefix = self.ID_PREFIX.get(table, table[:3].upper())
        self.counters[table] = self.counters.get(table, 0) + 1
        return f"{prefix}{self.counters[table]:06d}"

    def log(self, event: str, **detail: Any) -> Dict[str, Any]:
        """写审计日志。所有工具调用都会留痕，方便演示"Agent 到底做了什么"。"""
        record = {"at": self.clock.iso(), "event": event, **detail}
        self.audit.append(record)
        return record

    def touch(self, *records: Any, source: str = "") -> None:
        """Refresh provenance metadata after a business mutation."""
        for record in records:
            if record is None:
                continue
            if hasattr(record, "updated_at"):
                record.updated_at = self.clock.iso()
            if source and hasattr(record, "source"):
                record.source = source

    # ------------------------------------------------------------------
    # 用户 / 账户
    # ------------------------------------------------------------------
    def get_user(self, user_id: str) -> Optional[User]:
        return self.users.get(user_id)

    def accounts_of(self, user_id: str, kind: Optional[str] = None) -> List[Account]:
        items = [a for a in self.accounts.values() if a.user_id == user_id]
        if kind:
            items = [a for a in items if a.kind == kind]
        return sorted(items, key=lambda a: a.account_id)

    def get_account(self, account_id: str) -> Optional[Account]:
        return self.accounts.get(account_id)

    def primary_account(self, user_id: str) -> Optional[Account]:
        """默认扣款账户：优先活期，其次电子钱包。"""
        for kind in ("current", "wallet", "savings"):
            found = self.accounts_of(user_id, kind)
            if found:
                return found[0]
        accounts = self.accounts_of(user_id)
        return accounts[0] if accounts else None

    @staticmethod
    def total_assets(accounts: Iterable[Account]) -> float:
        total = 0.0
        for acc in accounts:
            if acc.kind == "credit":
                continue
            total += acc.balance
        return total

    # ------------------------------------------------------------------
    # 流水
    # ------------------------------------------------------------------
    def transactions_of(
        self,
        user_id: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> List[Transaction]:
        """按时间倒序返回流水。start/end 为 'YYYY-MM-DD' 或 ISO 时间字符串（含）。"""
        rows = [t for t in self.transactions.values() if t.user_id == user_id]
        if start:
            rows = [t for t in rows if t.timestamp >= start]
        if end:
            # 让 end='2026-09-30' 也能包含当天全部时间
            end_key = end if "T" in end else f"{end}T23:59:59"
            rows = [t for t in rows if t.timestamp <= end_key]
        return sorted(rows, key=lambda t: t.timestamp, reverse=True)

    def add_transaction(
        self,
        user_id: str,
        account_id: str,
        amount: float,
        merchant: str,
        category: str,
        channel: str = "APP",
        counterparty: str = "",
        note: str = "",
        timestamp: Optional[str] = None,
        is_subscription: bool = False,
        subscription_id: str = "",
        tags: Optional[List[str]] = None,
        status: str = "success",
        source: str = "agent:simulation",
        updated_at: str = "",
        is_live: bool = False,
        is_demo: bool = True,
    ) -> Transaction:
        txn = Transaction(
            txn_id=self.next_id("transactions"),
            user_id=user_id,
            account_id=account_id,
            timestamp=timestamp or self.clock.iso(),
            amount=round(amount, 2),
            merchant=merchant,
            category=category,
            channel=channel,
            counterparty=counterparty,
            note=note,
            status=status,
            is_subscription=is_subscription,
            subscription_id=subscription_id,
            tags=list(tags or []),
            source=source,
            updated_at=updated_at or self.clock.iso(),
            is_live=is_live,
            is_demo=is_demo,
        )
        self.transactions[txn.txn_id] = txn
        return txn

    # ------------------------------------------------------------------
    # 收款人
    # ------------------------------------------------------------------
    def payees_of(self, user_id: str) -> List[Payee]:
        return sorted(
            [p for p in self.payees.values() if p.user_id == user_id],
            key=lambda p: p.last_used or "",
            reverse=True,
        )

    def find_payee(self, user_id: str, keyword: str) -> Optional[Payee]:
        """按 姓名 / 昵称 / 手机号 / 卡号后四位 模糊匹配收款人。"""
        if not keyword:
            return None
        key = keyword.strip()
        digits = "".join(ch for ch in key if ch.isdigit())
        candidates = [p for p in self.payees.values() if p.user_id == user_id]
        # 1) 精确匹配优先
        for p in candidates:
            if key in {p.name, p.alias, p.phone}:
                return p
        # 2) 手机号后 4 位 / 卡号后 4 位
        if len(digits) >= 4:
            tail = digits[-4:]
            for p in candidates:
                if p.phone.endswith(tail) or p.tail == tail:
                    return p
        # 3) 包含匹配
        for p in candidates:
            if key in p.name or key in p.alias or (p.phone and key in p.phone):
                return p
        return None

    def remember_payee(
        self,
        user_id: str,
        name: str,
        phone: str = "",
        alias: str = "",
        relation: str = "",
        bank: str = "微众银行",
    ) -> Payee:
        """转过的收款人自动加入常用联系人（下次转账可直接按备注/昵称找）。"""
        existing = self.find_payee(user_id, name) or (self.find_payee(user_id, phone) if phone else None)
        if existing:
            existing.last_used = self.clock.iso()
            existing.updated_at = self.clock.iso()
            if phone and not existing.phone:
                existing.phone = phone
            if alias and not existing.alias:
                existing.alias = alias
            return existing
        payee = Payee(
            payee_id=self.next_id("payees"),
            user_id=user_id,
            name=name,
            phone=phone,
            bank=bank,
            tail=phone[-4:] if phone else "",
            alias=alias,
            relation=relation,
            last_used=self.clock.iso(),
        )
        payee.updated_at = self.clock.iso()
        self.payees[payee.payee_id] = payee
        return payee

    # ------------------------------------------------------------------
    # 卡片
    # ------------------------------------------------------------------
    def cards_of(self, user_id: str, status: Optional[str] = None) -> List[Card]:
        rows = [c for c in self.cards.values() if c.user_id == user_id]
        if status:
            rows = [c for c in rows if c.status == status]
        return sorted(rows, key=lambda c: c.card_id)

    def find_card(self, user_id: str, keyword: str = "") -> Optional[Card]:
        """按 后四位 / 卡种别名 找卡；不传关键词时返回默认主卡。"""
        cards = self.cards_of(user_id)
        if not cards:
            return None
        key = (keyword or "").strip()
        if not key:
            for c in cards:
                if c.kind == "credit" and c.status == "active":
                    return c
            return cards[0]
        digits = "".join(ch for ch in key if ch.isdigit())
        alias = {
            "信用卡": "credit",
            "信用": "credit",
            "借记卡": "debit",
            "储蓄卡": "debit",
            "虚拟卡": "virtual",
            "附属卡": "supplementary",
        }
        for cn, kind in alias.items():
            if cn in key:
                for c in cards:
                    if c.kind == kind:
                        return c
        if digits:
            for c in cards:
                if c.tail == digits[-4:]:
                    return c
        for c in cards:
            if key in c.note:
                return c
        return None

    # ------------------------------------------------------------------
    # 订阅
    # ------------------------------------------------------------------
    def subscriptions_of(self, user_id: str, status: Optional[str] = None) -> List[Subscription]:
        rows = [s for s in self.subscriptions.values() if s.user_id == user_id]
        if status:
            rows = [s for s in rows if s.status == status]
        return sorted(rows, key=lambda s: s.next_charge_date or "")

    def monthly_subscription_total(self, user_id: str) -> float:
        total = 0.0
        for sub in self.subscriptions_of(user_id, status="active"):
            if sub.cycle == "monthly":
                total += sub.amount
            elif sub.cycle == "yearly":
                total += sub.amount / 12
            elif sub.cycle == "weekly":
                total += sub.amount * 4.33
        return round(total, 2)

    # ------------------------------------------------------------------
    # 会员订单
    # ------------------------------------------------------------------
    def membership_orders_of(self, user_id: str) -> List[MembershipOrder]:
        rows = [o for o in self.membership_orders.values() if o.user_id == user_id]
        return sorted(rows, key=lambda o: o.created_at, reverse=True)

    def membership_order_items_of(self, order_id: str) -> List[MembershipOrderItem]:
        rows = [i for i in self.membership_order_items.values() if i.order_id == order_id]
        return sorted(rows, key=lambda i: i.item_id)

    def add_membership_order(
        self,
        user_id: str,
        account_id: str,
        total_amount: float,
        status: str = "completed",
        transaction_id: str = "",
    ) -> MembershipOrder:
        now = self.clock.iso()
        order = MembershipOrder(
            order_id=self.next_id("membership_orders"),
            user_id=user_id,
            account_id=account_id,
            total_amount=round(total_amount, 2),
            status=status,
            created_at=now,
            confirmed_at=now,
            executed_at=now,
            transaction_id=transaction_id,
            updated_at=now,
        )
        self.membership_orders[order.order_id] = order
        return order

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
        item = MembershipOrderItem(
            item_id=self.next_id("membership_order_items"),
            order_id=order_id,
            user_id=user_id,
            platform=platform,
            term_months=int(term_months),
            purchase_price=round(float(purchase_price), 2),
            auto_renew=bool(auto_renew),
            renewal_price=round(float(renewal_price), 2),
            status=status,
            updated_at=self.clock.iso(),
        )
        self.membership_order_items[item.item_id] = item
        return item

    # ------------------------------------------------------------------
    # 理财
    # ------------------------------------------------------------------
    def products_by_risk(self, max_risk: int) -> List[WealthProduct]:
        return sorted(
            [p for p in self.products.values() if p.risk_level <= max_risk],
            key=lambda p: (-p.expected_return, p.risk_level),
        )

    def find_product(self, keyword: str) -> Optional[WealthProduct]:
        if not keyword:
            return None
        key = keyword.strip()
        for p in self.products.values():
            if key == p.product_id or key == p.name:
                return p
        for p in self.products.values():
            if key in p.name or key in p.category:
                return p
        return None

    def holdings_of(self, user_id: str, status: Optional[str] = None) -> List[Holding]:
        rows = [h for h in self.holdings.values() if h.user_id == user_id]
        if status:
            rows = [h for h in rows if h.status == status]
        return sorted(rows, key=lambda h: h.purchase_date, reverse=True)

    # ------------------------------------------------------------------
    # 任务 / 目标 / AA / 提醒
    # ------------------------------------------------------------------
    def tasks_of(self, user_id: str, status: Optional[str] = None) -> List[ScheduledTask]:
        rows = [t for t in self.tasks.values() if t.user_id == user_id]
        if status:
            rows = [t for t in rows if t.status == status]
        return sorted(rows, key=lambda t: t.run_date)

    def add_task(
        self,
        user_id: str,
        kind: str,
        run_date: str,
        title: str,
        payload: Optional[Dict[str, Any]] = None,
        repeat: str = "once",
    ) -> ScheduledTask:
        task = ScheduledTask(
            task_id=self.next_id("tasks"),
            user_id=user_id,
            kind=kind,
            run_date=run_date,
            title=title,
            payload=dict(payload or {}),
            repeat=repeat,
            created_at=self.clock.iso(),
        )
        task.updated_at = self.clock.iso()
        self.tasks[task.task_id] = task
        return task

    def due_tasks(self, user_id: str, on_date: Optional[str] = None) -> List[ScheduledTask]:
        target = on_date or self.clock.date_str()
        return [
            t
            for t in self.tasks_of(user_id, status="pending")
            if t.run_date <= target
        ]

    def goals_of(self, user_id: str, status: Optional[str] = None) -> List[Goal]:
        rows = [g for g in self.goals.values() if g.user_id == user_id]
        if status:
            rows = [g for g in rows if g.status == status]
        return sorted(rows, key=lambda g: g.deadline or "9999")

    def add_goal(
        self,
        user_id: str,
        name: str,
        target_amount: float,
        deadline: str = "",
        scenario: str = "",
        account_id: str = "",
        note: str = "",
    ) -> Goal:
        goal = Goal(
            goal_id=self.next_id("goals"),
            user_id=user_id,
            name=name,
            target_amount=round(target_amount, 2),
            account_id=account_id,
            deadline=deadline,
            scenario=scenario,
            note=note,
            created_at=self.clock.iso(),
        )
        goal.updated_at = self.clock.iso()
        self.goals[goal.goal_id] = goal
        return goal

    def add_aa_sheet(
        self,
        user_id: str,
        title: str,
        total_amount: float,
        participants: List[Dict[str, Any]],
        note: str = "",
    ) -> AaSheet:
        sheet = AaSheet(
            sheet_id=self.next_id("aa_sheets"),
            user_id=user_id,
            title=title,
            total_amount=round(total_amount, 2),
            participants=participants,
            note=note,
            created_at=self.clock.iso(),
        )
        sheet.updated_at = self.clock.iso()
        self.aa_sheets[sheet.sheet_id] = sheet
        return sheet

    def add_alert(
        self,
        user_id: str,
        level: str,
        title: str,
        detail: str = "",
        source: str = "",
    ) -> Alert:
        alert = Alert(
            alert_id=self.next_id("alerts"),
            user_id=user_id,
            level=level,
            title=title,
            detail=detail,
            source=source,
            created_at=self.clock.iso(),
        )
        alert.updated_at = self.clock.iso()
        self.alerts[alert.alert_id] = alert
        return alert

    # ------------------------------------------------------------------
    # 用户上传批次
    # ------------------------------------------------------------------
    def upload_batches_of(self, user_id: str) -> List[UploadBatch]:
        rows = [batch for batch in self.upload_batches.values() if batch.user_id == user_id]
        return sorted(rows, key=lambda batch: batch.imported_at, reverse=True)

    def add_upload_batch(
        self,
        user_id: str,
        filename: str,
        record_count: int,
        total_expense: float,
        backend: str,
        transaction_ids: List[str],
    ) -> UploadBatch:
        batch = UploadBatch(
            batch_id=self.next_id("upload_batches"),
            user_id=user_id,
            filename=filename,
            imported_at=self.clock.iso(),
            record_count=record_count,
            total_expense=round(total_expense, 2),
            backend=backend,
            transaction_ids=list(transaction_ids),
        )
        self.upload_batches[batch.batch_id] = batch
        return batch

    def delete_upload_batch(self, user_id: str, batch_id: str) -> Dict[str, Any]:
        batch = self.upload_batches.get(batch_id)
        if batch is None or batch.user_id != user_id:
            raise ValueError(f"上传批次不存在：{batch_id}")
        transaction_ids = set(batch.transaction_ids)
        deleted_transactions = 0
        for txn_id in list(self.transactions):
            txn = self.transactions[txn_id]
            if txn.user_id == user_id and (
                txn.import_batch_id == batch_id
                or txn_id in transaction_ids
            ):
                self._reverse_import_effect(txn)
                del self.transactions[txn_id]
                deleted_transactions += 1
        del self.upload_batches[batch_id]
        return {
            "batch_id": batch_id,
            "deleted_transactions": deleted_transactions,
            "remaining_batches": len(self.upload_batches_of(user_id)),
        }

    def delete_legacy_uploads(self, user_id: str) -> Dict[str, Any]:
        targets = [
            txn for txn in self.transactions_of(user_id)
            if "receipt_import" in txn.tags and not txn.import_batch_id
        ]
        for txn in targets:
            self._reverse_import_effect(txn)
            del self.transactions[txn.txn_id]
        return {
            "batch_id": "legacy-imports",
            "deleted_transactions": len(targets),
            "remaining_batches": len(self.upload_batches_of(user_id)),
        }

    def migrate_upload_ledger(self, user_id: str, ledger: Account) -> int:
        """Backfill old receipt imports into the virtual statement-balance account."""
        imported = [
            txn for txn in self.transactions_of(user_id)
            if "receipt_import" in txn.tags
        ]
        if not imported:
            return 0
        expected_balance = round(sum(txn.amount for txn in imported), 2)
        needs_update = any(
            txn.account_id != ledger.account_id for txn in imported
        ) or abs(ledger.balance - expected_balance) > 0.005
        if not needs_update:
            return 0
        for txn in imported:
            txn.account_id = ledger.account_id
            txn.source = "upload:user"
            txn.is_demo = False
        ledger.balance = expected_balance
        self.touch(ledger)
        self.log(
            "upload_ledger.backfilled",
            user_id=user_id,
            transaction_count=len(imported),
            balance=expected_balance,
        )
        return len(imported)

    def _reverse_import_effect(self, txn: Transaction) -> None:
        account = self.accounts.get(txn.account_id)
        if account is None:
            return
        if account.kind == "credit":
            account.balance = round(account.balance + txn.amount, 2)
        else:
            account.balance = round(account.balance - txn.amount, 2)
        self.touch(account)

    def alerts_of(self, user_id: str, level: Optional[str] = None) -> List[Alert]:
        rows = [a for a in self.alerts.values() if a.user_id == user_id]
        if level:
            rows = [a for a in rows if a.level == level]
        return sorted(rows, key=lambda a: a.created_at, reverse=True)

    # ------------------------------------------------------------------
    # 种子数据 / 持久化
    # ------------------------------------------------------------------
    def seed(self) -> "DataStore":
        """装载整套模拟数据。"""
        seed_module.build_seed(self)
        self.ensure_card_ledger()
        return self

    def ensure_card_ledger(self) -> None:
        """Backfill card/account links for old snapshots without losing balances."""
        from finagent.data.card_ledger import (
            ensure_card_account,
            repair_card_ledger,
            sync_card_from_account,
        )

        repair_card_ledger(self)
        for card in self.cards.values():
            ensure_card_account(self, card)
            sync_card_from_account(self, card)

    def snapshot(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"counters": self.counters, "audit": self.audit[-200:]}
        for table in self.TABLES:
            data[table] = {k: to_dict(v) for k, v in getattr(self, table).items()}
        data["clock"] = self.clock.iso()
        return data

    def load_snapshot(self, data: Dict[str, Any]) -> "DataStore":
        for table in self.TABLES:
            model = self.MODEL_MAP[table]
            raw_rows = data.get(table, {}) or {}
            records = {key: build(model, value) for key, value in raw_rows.items()}
            if table == "subscriptions":
                for key, subscription in records.items():
                    raw = raw_rows.get(key) or {}
                    if not subscription.expires_at:
                        subscription.expires_at = subscription.next_charge_date
                    if "auto_renew" not in raw:
                        subscription.auto_renew = subscription.status not in {
                            "cancelled",
                            "paused",
                            "expired",
                        }
                    if "term_months" not in raw:
                        subscription.term_months = {
                            "yearly": 12,
                            "monthly": 1,
                        }.get(
                            subscription.cycle,
                            0 if subscription.cycle == "weekly" else 1,
                        )
                records = deduplicate_subscriptions(records)
            setattr(self, table, records)
        self.counters = data.get("counters", {}) or {}
        self.audit = data.get("audit", []) or []
        self.ensure_card_ledger()
        return self

    def save(self, path: Optional[Path] = None) -> Path:
        target = Path(path) if path else self.path
        if target is None:
            raise ValueError("未指定存储路径")
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.snapshot(), ensure_ascii=False, indent=2)
        target.write_text(payload, encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: Path, clock: Optional[Clock] = None) -> "DataStore":
        target = Path(path)
        store = cls(clock=clock, path=target)
        data = json.loads(target.read_text(encoding="utf-8"))
        store.load_snapshot(data)
        return store

