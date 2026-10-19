"""Score the copilot against the golden set.

    python -m evals.run --cases cases --out evals/report

Metrics (eval set only; demo cases are never scored):
  action_accuracy    proposal action == labeled action
  false_repairs      repairs proposed where the label is not repair, or with
                     the wrong value. Target 0.
  routing_precision  of cases sent to a person (outreach/escalate), how many
                     the label also sends to a person
  zero_miss          RR04, MS03, FRAD must always escalate. Target 0 misses.
  recall_reason_acc  Lane B: classified cancellation reason == label code

CI gate: exit 1 if false_repairs > 0 or zero_miss misses > 0. Accuracy is
reported, not gated: on 50 cases one case is 2 points, too noisy to gate on.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from copilot.agent import Copilot
from gen.model import Label

ZERO_MISS = {"RR04", "MS03", "FRAD"}
HUMAN = {"outreach", "escalate"}


def evaluate(cases: Path, copilot: Copilot | None = None, set_: str = "eval") -> dict:
    copilot = copilot or Copilot()
    rows = []
    for d in sorted(cases.iterdir()):
        if not (d / "label.json").exists():
            continue
        label = Label.model_validate_json((d / "label.json").read_text())
        if label.set != set_:
            continue
        p = copilot.triage(d)
        repair_ok = None
        if p.action == "repair":
            after = p.repair["changes"][0]["after"] if p.repair and p.repair["changes"] else None
            repair_ok = label.action == "repair" and after == label.expected_value
        rows.append({
            "case_id": label.case_id, "lane": label.lane, "code": label.code,
            "expected": label.action, "proposed": p.action,
            "correct": p.action == label.action,
            "false_repair": p.action == "repair" and not repair_ok,
            "reason_proposed": p.reason_code,
        })

    n = len(rows)
    to_human = [r for r in rows if r["proposed"] in HUMAN]
    zm = [r for r in rows if r["code"] in ZERO_MISS]
    lane_b = [r for r in rows if r["lane"] == "B"]
    metrics = {
        "set": set_,
        "cases": n,
        "model": copilot.model.name,
        "action_accuracy": sum(r["correct"] for r in rows) / n,
        "action_correct": sum(r["correct"] for r in rows),
        "false_repairs": sum(r["false_repair"] for r in rows),
        "routing_precision": (sum(r["expected"] in HUMAN for r in to_human) / len(to_human)) if to_human else None,
        "routed_to_human": len(to_human),
        "zero_miss_cases": len(zm),
        "zero_miss_misses": sum(r["proposed"] != "escalate" for r in zm),
        "recall_reason_acc": (sum(r["reason_proposed"] == r["code"] for r in lane_b) / len(lane_b)) if lane_b else None,
    }
    metrics["gate_passed"] = metrics["false_repairs"] == 0 and metrics["zero_miss_misses"] == 0
    return {"metrics": metrics, "failures": [r for r in rows if not r["correct"]], "rows": rows}


def to_markdown(result: dict) -> str:
    m = result["metrics"]
    pct = lambda x: "n/a" if x is None else f"{x:.0%}"  # noqa: E731
    lines = [
        f"# Eval report: {m['set']} set ({m['model']})", "",
        "| Metric | Result | Target |", "|---|---|---|",
        f"| Action accuracy | {pct(m['action_accuracy'])} ({m['action_correct']}/{m['cases']}) | report only |",
        f"| False repairs | {m['false_repairs']} | 0 (gated) |",
        f"| Routing precision | {pct(m['routing_precision'])} ({m['routed_to_human']} routed) | report only |",
        f"| Zero-miss (RR04, MS03, FRAD) | {m['zero_miss_misses']} missed of {m['zero_miss_cases']} | 0 (gated) |",
        f"| Recall reason accuracy | {pct(m['recall_reason_acc'])} | report only |",
        "", f"**Gate: {'PASSED' if m['gate_passed'] else 'FAILED'}**", "",
    ]
    if result["failures"]:
        lines += ["## Misses", "", "| Case | Expected | Proposed |", "|---|---|---|"]
        lines += [f"| {r['case_id']} | {r['expected']} | {r['proposed']} |" for r in result["failures"]]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=Path, default=Path("cases"))
    ap.add_argument("--out", type=Path, default=Path("evals/report"))
    ap.add_argument("--set", dest="set_", default="eval", choices=["eval", "holdout"])
    ap.add_argument("--report-only", action="store_true", help="never fail; for tracking known gaps")
    args = ap.parse_args()
    result = evaluate(args.cases, set_=args.set_)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(json.dumps(result, indent=2) + "\n")
    (args.out / "report.md").write_text(to_markdown(result))
    print(to_markdown(result))
    sys.exit(0 if args.report_only or result["metrics"]["gate_passed"] else 1)


if __name__ == "__main__":
    main()
