"""Deterministic Agent evaluation runner."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from finagent.agent.core import BankingAgent
from finagent.data.store import Clock, DataStore


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    text: str
    expected_intent: str
    expected_kind: Optional[str] = None
    expected_text: tuple[str, ...] = ()
    forbidden_text: tuple[str, ...] = ()


def load_cases(path: str | Path) -> List[EvaluationCase]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        EvaluationCase(
            case_id=str(item["id"]),
            text=str(item["text"]),
            expected_intent=str(item["expected_intent"]),
            expected_kind=item.get("expected_kind"),
            expected_text=tuple(item.get("expected_text") or []),
            forbidden_text=tuple(item.get("forbidden_text") or []),
        )
        for item in raw
    ]


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
                reply = agent.handle(case.text)
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
                rows.append({
                    "id": case.case_id,
                    "text": case.text,
                    "passed": not failures,
                    "failures": failures,
                    "intent": reply.intent,
                    "kind": reply.kind,
                    "workflow_status": (reply.data.get("workflow") or {}).get("status"),
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
