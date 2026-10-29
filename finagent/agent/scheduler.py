"""定时任务执行器。

负责把"未来的动作"真正执行掉：定时转账、生日礼物预约下单、续费提醒。
演示时可以用 `/advance 2` 把时间快进 2 天，立刻看到任务被执行。
"""
from __future__ import annotations

import threading
from datetime import date
from typing import List, Optional

from finagent.agent.registry import ToolRegistry
from finagent.agent.subscription_lifecycle import process_subscription_lifecycle
from finagent.data.card_ledger import sync_cards_for_account
from finagent.data.models import ScheduledTask
from finagent.data.providers import DataProviderRegistry, build_data_providers
from finagent.data.store import Clock, DataStore
from finagent.data.transaction_lifecycle import enrich_transaction
from finagent.risk.engine import RiskEngine
from finagent.tools.base import ToolContext


class Scheduler:
    """按日期执行到期的计划任务。"""

    def __init__(
        self,
        store: DataStore,
        clock: Clock,
        risk: RiskEngine,
        registry: ToolRegistry,
        providers: Optional[DataProviderRegistry] = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.risk = risk
        self.registry = registry
        self.providers = providers or build_data_providers(store)
        self._lock = threading.RLock()

    def due(self, user_id: str) -> List[ScheduledTask]:
        return self.store.due_tasks(user_id)

    def run_due(self, user_id: str) -> List[str]:
        """执行全部到期任务，返回给用户看的执行记录。"""
        with self._lock:
            messages: List[str] = process_subscription_lifecycle(
                self.store,
                self.clock,
                user_id,
            )
            ctx = ToolContext(
                store=self.store,
                clock=self.clock,
                risk=self.risk,
                user_id=user_id,
                providers=self.providers,
            )
            for task in self.due(user_id):
                message = self._execute(ctx, task)
                if message:
                    messages.append(message)
                self._reschedule(task)
            return messages

    def _execute(self, ctx: ToolContext, task: ScheduledTask) -> str:
        with self._lock:
            return self._execute_locked(ctx, task)

    def _execute_locked(self, ctx: ToolContext, task: ScheduledTask) -> str:
        if task.status != "pending" or task.executed_at:
            return ""
        task_id = task.task_id
        task.status = "processing"
        self.store.touch(task)
        checkpoint = self.store.checkpoint()
        try:
            if task.kind == "scheduled_transfer":
                tool = self.registry.get("transfer.send")
                if tool is None:
                    raise RuntimeError("转账工具不可用")
                payload = task.payload
                result = tool.run(
                    ctx,
                    payee=payload.get("payee", ""),
                    amount=payload.get("amount", 0),
                    note=payload.get("note", task.title),
                    channel=payload.get("channel", "phone"),
                    account=payload.get("account", ""),
                    payment_verified=bool(payload.get("payment_verified", False)),
                    idempotency_key=f"task:{task.task_id}:{task.run_date}",
                )
                if not result.ok:
                    self.store.restore_checkpoint(checkpoint)
                    task = self.store.tasks[task_id]
                    task.status = "failed"
                    task.executed_at = self.clock.iso()
                    task.result = result.summary
                    self.store.touch(task)
                    return f"[定时任务] {task.title}\n  {result.summary}"
                task.status = "done"
                task.executed_at = self.clock.iso()
                task.result = result.summary
                self.store.touch(task)
                return f"[定时任务] {task.title}\n  {result.summary}"

            if task.kind == "gift_order":
                return self._settle_gift_order(ctx, task)

            if task.kind == "reminder":
                task.status = "done"
                task.executed_at = self.clock.iso()
                task.result = "已提醒"
                self.store.touch(task)
                return f"[提醒] {task.title}"

            task.status = "done"
            task.executed_at = self.clock.iso()
            self.store.touch(task)
            return f"[任务] {task.title}"
        except Exception as exc:  # 原型阶段不让单个任务失败拖垮整个循环
            self.store.restore_checkpoint(checkpoint)
            task = self.store.tasks.get(task_id, task)
            task.status = "failed"
            task.executed_at = self.clock.iso()
            task.result = f"执行失败：{exc}"
            self.store.touch(task)
            return f"[任务失败] {task.title}：{exc}"

    def _settle_gift_order(self, ctx: ToolContext, task: ScheduledTask) -> str:
        """Charge a gift order and release the matching locked goal amount."""
        payload = dict(task.payload)
        existing_txn = next(
            (
                txn for txn in self.store.transactions_of(task.user_id)
                if "gift_order" in txn.tags
                and str((txn.metadata or {}).get("task_id") or "") == task.task_id
                and txn.status == "success"
            ),
            None,
        )
        if existing_txn is not None:
            task.status = "done"
            task.executed_at = existing_txn.timestamp
            task.result = f"已扣款流水已存在：{existing_txn.txn_id}"
            self.store.touch(task)
            return (
                f"[预约下单] {task.title}\n"
                f"  检测到原扣款流水 {existing_txn.txn_id}，本次未重复扣款。"
            )
        try:
            amount = round(float(payload.get("budget") or 0), 2)
        except (TypeError, ValueError):
            amount = 0.0
        if amount <= 0:
            raise ValueError("礼物预约没有有效金额，无法扣款")

        goals = self.providers.goals.of(task.user_id, "active")
        goal_name = str(payload.get("goal_name") or "")
        goal = next(
            (item for item in goals if item.name == goal_name),
            None,
        )
        if goal is None:
            person = str(payload.get("person") or "")
            goal = next(
                (
                    item for item in goals
                    if item.scenario == "birthday"
                    and person
                    and person in item.name
                ),
                None,
            )
        account = (
            self.store.accounts.get(goal.account_id)
            if goal is not None and goal.account_id
            else self.providers.accounts.default_for_payment(task.user_id)
        )
        if account is None or account.user_id != task.user_id:
            raise ValueError("没有找到礼物付款账户")
        payment_card = next(
            (
                card for card in self.store.cards_of(task.user_id)
                if card.account_id == account.account_id
            ),
            None,
        )
        if payment_card is None or payment_card.status != "active":
            raise ValueError("礼物付款卡当前不可用或未激活")

        reserved_for_goal = min(
            max(account.locked, 0.0),
            max(goal.locked_amount, 0.0) if goal is not None else 0.0,
        )
        if amount > account.available + reserved_for_goal:
            raise ValueError(
                f"付款账户可用余额不足：可用 {account.available:,.2f} 元，"
                f"目标锁定可取 {reserved_for_goal:,.2f} 元，需要 {amount:,.2f} 元"
            )

        account.balance = round(account.balance - amount, 2)
        released = min(amount, reserved_for_goal)
        account.locked = round(max(0.0, account.locked - released), 2)
        if goal is not None:
            goal.locked_amount = round(
                max(0.0, goal.locked_amount - min(amount, goal.locked_amount)),
                2,
            )
            if goal.locked_amount <= 0.01:
                goal.locked_amount = 0.0
                goal.status = "achieved"
            self.store.touch(goal)

        txn = self.store.add_transaction(
            user_id=task.user_id,
            account_id=account.account_id,
            amount=-amount,
            merchant=str(payload.get("merchant") or "生日礼物预约"),
            category="跨场景",
            channel="预约下单",
            counterparty=str(payload.get("person") or ""),
            note=str(payload.get("item") or task.title),
            tags=["gift_order", "birthday"],
        )
        enrich_transaction(
            txn,
            self.clock,
            metadata={
                "task_id": task.task_id,
                "goal_id": goal.goal_id if goal is not None else "",
                "person": payload.get("person", ""),
                "item": payload.get("item", ""),
                "amount": amount,
            },
        )
        sync_cards_for_account(self.store, account)
        self.store.touch(account)

        payload.update({
            "account_id": account.account_id,
            "payment_txn_id": txn.txn_id,
            "charged_amount": amount,
            "remaining_locked": account.locked,
            "goal_locked_remaining": goal.locked_amount if goal is not None else 0.0,
        })
        task.payload = payload
        task.status = "done"
        task.executed_at = self.clock.iso()
        task.result = f"已扣款 {amount:,.2f} 元，流水 {txn.txn_id}"
        self.store.touch(task)
        self.store.log(
            "gift_order.settled",
            user_id=task.user_id,
            task_id=task.task_id,
            goal_id=goal.goal_id if goal is not None else "",
            account_id=account.account_id,
            amount=amount,
            txn_id=txn.txn_id,
        )
        return (
            f"[预约下单] {task.title}\n"
            f"  已为 {payload.get('person', '')} 下单 {payload.get('item', '')}，"
            f"扣款 {amount:,.2f} 元\n"
            f"  付款卡余额：{account.balance:,.2f} 元；"
            f"剩余锁定：{account.locked:,.2f} 元\n"
            f"  购买流水：{txn.txn_id}"
        )

    def _reschedule(self, task: ScheduledTask) -> None:
        """周期性任务自动排下一次。"""
        if task.status != "done" or task.repeat == "once":
            return
        try:
            current = date.fromisoformat(task.run_date)
        except ValueError:
            return
        if task.repeat == "monthly":
            year, month = current.year, current.month + 1
            if month > 12:
                year, month = year + 1, 1
            day = min(current.day, 28)
            next_date = date(year, month, day)
        elif task.repeat == "weekly":
            from datetime import timedelta
            next_date = current + timedelta(days=7)
        else:
            return
        self.store.add_task(
            user_id=task.user_id,
            kind=task.kind,
            run_date=next_date.isoformat(),
            title=task.title,
            payload=task.payload,
            repeat=task.repeat,
        )

