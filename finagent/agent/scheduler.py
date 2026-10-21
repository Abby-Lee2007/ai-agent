"""定时任务执行器。

负责把"未来的动作"真正执行掉：定时转账、生日礼物预约下单、续费提醒。
演示时可以用 `/advance 2` 把时间快进 2 天，立刻看到任务被执行。
"""
from __future__ import annotations

from datetime import date
from typing import List, Optional

from finagent.agent.registry import ToolRegistry
from finagent.data.models import ScheduledTask
from finagent.data.providers import DataProviderRegistry, build_data_providers
from finagent.data.store import Clock, DataStore
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

    def due(self, user_id: str) -> List[ScheduledTask]:
        return self.store.due_tasks(user_id)

    def run_due(self, user_id: str) -> List[str]:
        """执行全部到期任务，返回给用户看的执行记录。"""
        messages: List[str] = []
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
        try:
            if task.kind == "scheduled_transfer":
                tool = self.registry.get("transfer.send")
                if tool is None:
                    return ""
                payload = task.payload
                result = tool.run(
                    ctx,
                    payee=payload.get("payee", ""),
                    amount=payload.get("amount", 0),
                    note=payload.get("note", task.title),
                )
                task.status = "done" if result.ok else "failed"
                task.executed_at = self.clock.iso()
                task.result = result.summary
                self.store.touch(task)
                return f"[定时任务] {task.title}\n  {result.summary}"

            if task.kind == "gift_order":
                payload = task.payload
                task.status = "done"
                task.executed_at = self.clock.iso()
                task.result = "已下单"
                self.store.touch(task)
                return (
                    f"[预约下单] {task.title}\n"
                    f"  已为 {payload.get('person', '')} 下单 {payload.get('item', '')}，"
                    f"预算 {payload.get('budget', 0):,.2f} 元，"
                    f"配送方式：{payload.get('channel', '同城即时配送')}（模拟）"
                )

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
            task.status = "failed"
            task.executed_at = self.clock.iso()
            task.result = f"执行失败：{exc}"
            self.store.touch(task)
            return f"[任务失败] {task.title}：{exc}"

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

