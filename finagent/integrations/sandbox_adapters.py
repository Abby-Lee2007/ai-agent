"""Unified sandbox boundary for bank, payment and brokerage operations.

The adapters deliberately share one execution and callback contract. Replacing
any sandbox implementation with a certified provider should not require changing
the banking tools or the HTTP callback handler.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from finagent.data.card_ledger import sync_cards_for_account
from finagent.data.models import SandboxExecution
from finagent.data.store import Clock, DataStore

PROVIDERS = {"bank", "payment", "broker"}
ALLOWED_TRANSITIONS: Dict[str, set[str]] = {
    "submitted": {"processing", "completed", "failed", "cancelled"},
    "processing": {"completed", "failed", "returned", "cancelled"},
    "completed": {"returned", "reversed"},
    "failed": {"processing", "completed", "cancelled"},
    "returned": set(),
    "reversed": set(),
    "cancelled": set(),
}


class SandboxTransitionError(ValueError):
    """Raised when a provider callback attempts an invalid lifecycle move."""


class UnifiedSandboxGateway:
    """Persist and validate bank/payment/broker execution lifecycles."""

    def __init__(self, store: DataStore, clock: Optional[Clock] = None) -> None:
        self.store = store
        self.clock = clock or store.clock

    def submit(
        self,
        *,
        user_id: str,
        provider: str,
        operation: str,
        reference_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SandboxExecution:
        provider = str(provider or "").strip().lower()
        if provider not in PROVIDERS:
            raise ValueError(f"不支持的沙箱提供方：{provider}")
        if not str(operation or "").strip() or not str(reference_id or "").strip():
            raise ValueError("operation 和 reference_id 不能为空")
        now = self.clock.iso()
        execution = SandboxExecution(
            execution_id=self.store.next_id("sandbox_executions"),
            user_id=user_id,
            provider=provider,
            operation=str(operation).strip(),
            reference_id=str(reference_id).strip(),
            status="submitted",
            metadata=dict(metadata or {}),
            history=[{
                "status": "submitted",
                "at": now,
                "source": "application",
                "reason": "request accepted by sandbox adapter",
            }],
            created_at=now,
            updated_at=now,
        )
        self.store.sandbox_executions[execution.execution_id] = execution
        self.store.log(
            "sandbox.submitted",
            execution_id=execution.execution_id,
            provider=provider,
            operation=execution.operation,
            reference_id=execution.reference_id,
        )
        return execution

    def transition(
        self,
        execution_id: str,
        status: str,
        *,
        provider_reference: str = "",
        reason: str = "",
        payload: Optional[Dict[str, Any]] = None,
    ) -> SandboxExecution:
        execution = self.store.sandbox_executions.get(execution_id)
        if execution is None:
            raise ValueError(f"沙箱执行不存在：{execution_id}")
        status = str(status or "").strip().lower()
        if status == execution.status:
            return execution
        allowed = ALLOWED_TRANSITIONS.get(execution.status, set())
        if status not in allowed:
            raise SandboxTransitionError(
                f"不允许从 {execution.status} 转为 {status}"
            )
        previous = execution.status
        execution.status = status
        execution.provider_reference = (
            provider_reference or execution.provider_reference
        )
        execution.metadata.update(payload or {})
        if status == "failed":
            execution.failure_reason = str(reason or "provider rejected request")
        execution.updated_at = self.clock.iso()
        execution.history.append({
            "status": status,
            "at": execution.updated_at,
            "source": "callback",
            "reason": str(reason or ""),
        })
        self._apply_to_reference(execution, previous)
        self.store.log(
            "sandbox.callback",
            execution_id=execution.execution_id,
            provider=execution.provider,
            operation=execution.operation,
            previous=previous,
            status=status,
            reference_id=execution.reference_id,
        )
        return execution

    def callback(
        self,
        execution_id: str,
        *,
        status: str,
        provider_reference: str = "",
        reason: str = "",
        payload: Optional[Dict[str, Any]] = None,
    ) -> SandboxExecution:
        return self.transition(
            execution_id,
            status,
            provider_reference=provider_reference,
            reason=reason,
            payload=payload,
        )

    def complete_immediately(
        self,
        execution: SandboxExecution,
        *,
        provider_reference: str = "",
    ) -> SandboxExecution:
        self.transition(
            execution.execution_id,
            "processing",
            reason="sandbox accepted the request",
        )
        return self.transition(
            execution.execution_id,
            "completed",
            provider_reference=provider_reference,
            reason="sandbox simulated a successful callback",
        )

    def _apply_to_reference(
        self,
        execution: SandboxExecution,
        previous_status: str,
    ) -> None:
        txn = self.store.transactions.get(execution.reference_id)
        if txn is not None and txn.user_id == execution.user_id:
            txn.lifecycle_status = execution.status
            txn.metadata = dict(txn.metadata or {})
            txn.metadata["sandbox"] = execution_payload(execution)
            if execution.status == "completed":
                txn.status = "success"
                txn.completed_at = execution.updated_at
                txn.failure_reason = ""
            elif execution.status in {"failed", "returned", "reversed"}:
                txn.status = "failed"
                txn.failure_reason = execution.failure_reason or execution.status
            if (
                execution.status in {"returned", "reversed"}
                and previous_status not in {"returned", "reversed"}
                and txn.amount < 0
            ):
                account = self.store.accounts.get(txn.account_id)
                if account is not None:
                    account.balance = round(account.balance + abs(txn.amount), 2)
                    self.store.touch(account)
                    sync_cards_for_account(self.store, account)
            self.store.touch(txn)
            return

        holding = self.store.holdings.get(execution.reference_id)
        if holding is not None and holding.user_id == execution.user_id:
            holding_metadata = dict(getattr(holding, "metadata", {}) or {})
            holding_metadata["sandbox"] = execution_payload(execution)
            setattr(holding, "metadata", holding_metadata)
            self.store.touch(holding)

    def list_for_user(self, user_id: str) -> List[SandboxExecution]:
        return sorted(
            (
                item for item in self.store.sandbox_executions.values()
                if item.user_id == user_id
            ),
            key=lambda item: item.created_at,
            reverse=True,
        )


class ProviderSandboxAdapter:
    provider = ""

    def __init__(self, gateway: UnifiedSandboxGateway, user_id: str) -> None:
        self.gateway = gateway
        self.user_id = user_id

    def submit(
        self,
        operation: str,
        reference_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SandboxExecution:
        return self.gateway.submit(
            user_id=self.user_id,
            provider=self.provider,
            operation=operation,
            reference_id=reference_id,
            metadata=metadata,
        )


class BankSandboxAdapter(ProviderSandboxAdapter):
    provider = "bank"


class PaymentSandboxAdapter(ProviderSandboxAdapter):
    provider = "payment"


class BrokerSandboxAdapter(ProviderSandboxAdapter):
    """Simulated trading desk that never sends a real brokerage order."""

    provider = "broker"

    def submit_order(
        self,
        reference_id: str,
        *,
        side: str,
        product_id: str,
        amount: float,
        symbol: str = "",
        limit_price: Optional[float] = None,
        quantity: float = 0.0,
        fees: float = 0.0,
    ) -> SandboxExecution:
        side = str(side or "").strip().lower()
        if side not in {"subscribe", "redeem", "buy", "sell"}:
            raise ValueError(f"不支持的模拟委托方向：{side}")
        return self.submit(
            side,
            reference_id,
            {
                "product_id": product_id,
                "amount": round(float(amount), 2),
                "symbol": str(symbol or "").upper(),
                "limit_price": limit_price,
                "quantity": round(float(quantity), 4),
                "fees": round(float(fees), 2),
                "venue": "simulated-broker-desk",
            },
        )


@dataclass
class SandboxAdapterRegistry:
    gateway: UnifiedSandboxGateway
    bank: BankSandboxAdapter
    payment: PaymentSandboxAdapter
    broker: BrokerSandboxAdapter

    def status(self) -> Dict[str, Any]:
        return {
            "mode": "sandbox",
            "providers": sorted(PROVIDERS),
            "broker_mode": "simulated-trading-desk",
            "callback_transitions": {
                status: sorted(targets)
                for status, targets in ALLOWED_TRANSITIONS.items()
            },
        }


def build_sandbox_registry(
    store: DataStore,
    user_id: str,
    clock: Optional[Clock] = None,
) -> SandboxAdapterRegistry:
    gateway = UnifiedSandboxGateway(store, clock)
    return SandboxAdapterRegistry(
        gateway=gateway,
        bank=BankSandboxAdapter(gateway, user_id),
        payment=PaymentSandboxAdapter(gateway, user_id),
        broker=BrokerSandboxAdapter(gateway, user_id),
    )


def execution_payload(execution: SandboxExecution) -> Dict[str, Any]:
    return {
        "execution_id": execution.execution_id,
        "provider": execution.provider,
        "operation": execution.operation,
        "reference_id": execution.reference_id,
        "status": execution.status,
        "provider_reference": execution.provider_reference,
        "failure_reason": execution.failure_reason,
        "history": list(execution.history),
        "metadata": dict(execution.metadata),
        "is_demo": execution.is_demo,
        "source": execution.source,
    }
