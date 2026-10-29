"""Run the six-case deterministic fairness and sensitivity audit."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from finagent.analytics.fairness import run_fairness_audit  # noqa: E402
from finagent.data.store import DataStore  # noqa: E402


def main() -> int:
    report = run_fairness_audit(DataStore().seed())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["repeatable_with_same_environment"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
