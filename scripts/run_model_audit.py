"""Generate the P2 financial decision-model audit artifacts."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from finagent.data.providers import build_data_providers  # noqa: E402
from finagent.data.store import Clock, DataStore  # noqa: E402
from finagent.evaluation import run_model_audit, write_model_audit_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default="u_001")
    parser.add_argument("--simulations", type=int, default=120)
    parser.add_argument(
        "--json",
        default=str(ROOT / "evidence" / "results" / "model_audit.json"),
    )
    parser.add_argument(
        "--markdown",
        default=str(ROOT / "evidence" / "report" / "model_audit.md"),
    )
    args = parser.parse_args()
    store = DataStore(clock=Clock()).seed()
    providers = build_data_providers(store)
    report = run_model_audit(
        store,
        providers,
        store.clock,
        args.user,
        simulations=args.simulations,
    )
    json_path = Path(args.json)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown_path = write_model_audit_report(report, args.markdown)
    print(f"Model audit written to {json_path} and {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
