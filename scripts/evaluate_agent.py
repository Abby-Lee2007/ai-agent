"""Run the offline Agent evaluation suite."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from finagent.evaluation import EvaluationRunner, load_cases  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cases",
        default=str(ROOT / "evals" / "agent_cases.json"),
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = EvaluationRunner(load_cases(args.cases)).run()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"Agent evaluation: {report['passed']}/{report['total']} passed "
            f"({report['accuracy'] * 100:.1f}%)"
        )
        for row in report["cases"]:
            mark = "PASS" if row["passed"] else "FAIL"
            print(f"[{mark}] {row['id']} · {row['text']}")
            for failure in row["failures"]:
                print(f"       {failure}")
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
