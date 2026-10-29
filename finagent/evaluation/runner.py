"""Deterministic Agent evaluation runner."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

from finagent.agent.core import BankingAgent
from finagent.data.card_ledger import card_asset_total
from finagent.data.store import Clock, DataStore


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    text: str
    expected_intent: str
    expected_kind: Optional[str] = None
    expected_text: tuple[str, ...] = ()
    forbidden_text: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    expected_step_kinds: tuple[str, ...] = ()
    expected_card_deltas: Dict[str, float] = field(default_factory=dict)
    expected_card_fields: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    expected_card_statuses: Dict[str, str] = field(default_factory=dict)
    expected_new_transactions: tuple[Dict[str, Any], ...] = ()
    expected_no_new_transactions: bool = False
    expected_asset_delta: Optional[float] = None
    expected_workflow_status: str = ""


def load_cases(path: Union[str, Path]) -> List[EvaluationCase]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = []
    for item in raw:
        assertions = dict(item.get("assertions") or {})
        cases.append(EvaluationCase(
            case_id=str(item["id"]),
            text=str(item["text"]),
            expected_intent=str(item["expected_intent"]),
            expected_kind=item.get("expected_kind"),
            expected_text=tuple(item.get("expected_text") or []),
            forbidden_text=tuple(item.get("forbidden_text") or []),
            steps=tuple(item.get("steps") or []),
            expected_step_kinds=tuple(assertions.get("step_kinds") or []),
            expected_card_deltas={
                str(key): float(value)
                for key, value in (assertions.get("card_deltas") or {}).items()
            },
            expected_card_fields=dict(assertions.get("card_fields") or {}),
            expected_card_statuses={
                str(key): str(value)
                for key, value in (assertions.get("card_statuses") or {}).items()
            },
            expected_new_transactions=tuple(
                assertions.get("new_transactions") or []
            ),
            expected_no_new_transactions=bool(
                assertions.get("no_new_transactions", False)
            ),
            expected_asset_delta=(
                float(assertions["asset_delta"])
                if assertions.get("asset_delta") is not None else None
            ),
            expected_workflow_status=str(
                assertions.get("workflow_status") or ""
            ),
        ))
    return cases


class EvaluationRunner:
    def __init__(self, cases: Iterable[EvaluationCase]) -> None:
        self.cases = list(cases)

    @staticmethod
    def _new_agent() -> BankingAgent:
        store = DataStore(clock=Clock()).seed()
        agent = BankingAgent(store, "u_001", llm_mode="off")
        agent.market.mode = "off"
        agent.risk_level = 3
        return agent

    def run(self) -> Dict[str, Any]:
        rows = []
        for case in self.cases:
            agent = self._new_agent()
            try:
                before_cards = {
                    card.card_id: card
                    for card in agent.providers.cards.of("u_001")
                }
                before_balances = {
                    card_id: float(card.balance)
                    for card_id, card in before_cards.items()
                }
                before_txn_ids = set(agent.store.transactions)
                before_asset_total = card_asset_total(agent.store, "u_001")
                replies = []
                for step in case.steps or (case.text,):
                    replies.append(agent.handle(step))
                reply = replies[-1]
                failures = []
                if reply.intent != case.expected_intent:
                    failures.append(
                        f"intent={reply.intent}, expected={case.expected_intent}"
                    )
                if case.expected_kind and reply.kind != case.expected_kind:
                    failures.append(
                        f"kind={reply.kind}, expected={case.expected_kind}"
                    )
                for text in case.expected_text:
                    if text not in reply.text:
                        failures.append(f"missing text: {text}")
                for text in case.forbidden_text:
                    if text in reply.text:
                        failures.append(f"forbidden text: {text}")
                if case.expected_step_kinds:
                    actual_kinds = tuple(item.kind for item in replies)
                    if actual_kinds != case.expected_step_kinds:
                        failures.append(
                            f"step kinds={actual_kinds}, "
                            f"expected={case.expected_step_kinds}"
                        )
                for selector, expected_delta in case.expected_card_deltas.items():
                    card = agent.providers.cards.find("u_001", selector)
                    if card is None:
                        failures.append(f"card not found: {selector}")
                        continue
                    actual_delta = round(
                        float(card.balance) - before_balances.get(card.card_id, 0.0),
                        2,
                    )
                    if abs(actual_delta - expected_delta) > 0.01:
                        failures.append(
                            f"{selector} balance delta={actual_delta}, "
                            f"expected={expected_delta}"
                        )
                for selector, expected_fields in case.expected_card_fields.items():
                    card = agent.providers.cards.find("u_001", selector)
                    if card is None:
                        failures.append(f"card not found: {selector}")
                        continue
                    for field_name, expected_value in dict(expected_fields).items():
                        actual_value = getattr(card, field_name, None)
                        if actual_value != expected_value:
                            failures.append(
                                f"{selector}.{field_name}={actual_value}, "
                                f"expected={expected_value}"
                            )
                for selector, expected_status in case.expected_card_statuses.items():
                    card = agent.providers.cards.find("u_001", selector)
                    if card is None:
                        failures.append(f"card not found: {selector}")
                    elif card.status != expected_status:
                        failures.append(
                            f"{selector} status={card.status}, "
                            f"expected={expected_status}"
                        )
                new_transactions = [
                    txn for txn_id, txn in agent.store.transactions.items()
                    if txn_id not in before_txn_ids
                ]
                if (
                    case.expected_no_new_transactions
                    and new_transactions
                ):
                    failures.append(
                        f"new transactions={len(new_transactions)}, expected=0"
                    )
                for expected_txn in case.expected_new_transactions:
                    merchant = str(expected_txn.get("merchant_contains") or "")
                    amount = expected_txn.get("amount")
                    matching = [
                        txn for txn in new_transactions
                        if (not merchant or merchant in txn.merchant)
                        and (
                            amount is None
                            or abs(float(txn.amount) - float(amount)) <= 0.01
                        )
                    ]
                    if not matching:
                        failures.append(
                            f"missing new transaction: {expected_txn}"
                        )
                if case.expected_asset_delta is not None:
                    actual_asset_delta = round(
                        card_asset_total(agent.store, "u_001")
                        - before_asset_total,
                        2,
                    )
                    if abs(actual_asset_delta - case.expected_asset_delta) > 0.01:
                        failures.append(
                            f"asset delta={actual_asset_delta}, "
                            f"expected={case.expected_asset_delta}"
                        )
                workflow_status = (reply.data.get("workflow") or {}).get("status")
                if (
                    case.expected_workflow_status
                    and workflow_status != case.expected_workflow_status
                ):
                    failures.append(
                        f"workflow status={workflow_status}, "
                        f"expected={case.expected_workflow_status}"
                    )
                rows.append({
                    "id": case.case_id,
                    "text": case.text,
                    "passed": not failures,
                    "failures": failures,
                    "intent": reply.intent,
                    "kind": reply.kind,
                    "workflow_status": workflow_status,
                    "step_kinds": [item.kind for item in replies],
                })
            except Exception as exc:
                rows.append({
                    "id": case.case_id,
                    "text": case.text,
                    "passed": False,
                    "failures": [f"exception: {exc}"],
                    "intent": "",
                    "kind": "exception",
                    "workflow_status": "",
                })
        total = len(rows)
        passed = sum(1 for row in rows if row["passed"])
        return {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "accuracy": round(passed / total, 3) if total else 0.0,
            "cases": rows,
        }
