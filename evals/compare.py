"""Before/after: rules baseline vs the model, on the same cases.

    python -m evals.compare --cases cases_holdout --set holdout --out evals/results

Writes <out>/<set>_comparison.md and .json: a metric table side by side and a
case-by-case failure analysis of every model miss, with the model's own
rationale and fraud indicators. Exits 1 if the model run breaks a hard gate
(false repairs > 0 or zero-miss > 0) or silently fell back to rules.

Rule for the held-out set: do not tune prompts against it. If a prompt change
is made after seeing these results, write a held-out v2 and report on that.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .run import evaluate, make_copilot

ROWS = [
    ("Fraud miss rate (FRAD not escalated)", "fraud_miss"),
    ("Zero-miss misses (RR04, MS03, FRAD)", "zero_miss_misses"),
    ("False repairs", "false_repairs"),
    ("Action accuracy", "action_accuracy"),
    ("Routing precision", "routing_precision"),
    ("Recall reason accuracy", "recall_reason_acc"),
    ("Cases where the model fell back to rules", "model_calls_fell_back"),
]


def _fraud_miss(result: dict) -> tuple[int, int]:
    fr = [r for r in result["rows"] if r["code"] == "FRAD"]
    return sum(r["proposed"] != "escalate" for r in fr), len(fr)


def _fmt(key: str, m: dict, result: dict) -> str:
    if key == "fraud_miss":
        miss, n = _fraud_miss(result)
        return f"{miss} of {n}"
    v = m.get(key)
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.0%}"
    return str(v)


def compare(before: dict, after: dict) -> str:
    mb, ma = before["metrics"], after["metrics"]
    lines = [
        f"# Before / after: {mb['set']} set ({mb['cases']} cases)", "",
        f"Run {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. Before: `{mb['model']}`. After: `{ma['model']}`.", "",
        "| Metric | Rules baseline | Model |", "|---|---|---|",
    ]
    lines += [f"| {label} | {_fmt(k, mb, before)} | {_fmt(k, ma, after)} |" for label, k in ROWS]
    gate = ma["gate_passed"] and ma.get("model_calls_fell_back", 0) == 0
    lines += ["", f"**Model run hard gates (false repairs = 0, zero-miss = 0, no fallbacks): "
                  f"{'PASSED' if gate else 'FAILED'}**", ""]

    misses = [r for r in after["rows"] if not r["correct"]]
    lines += ["## Model misses: failure analysis", ""]
    if not misses:
        lines += ["None on this set.", ""]
    else:
        lines += ["| Case | Expected | Model proposed | Model reason | Confidence | Fraud indicators | Rationale |",
                  "|---|---|---|---|---|---|---|"]
        for r in misses:
            o = r.get("model_output") or {}
            lines.append(
                f"| {r['case_id']} | {r['expected']} | {r['proposed']} | {o.get('reason', '')} | "
                f"{o.get('confidence', '')} | {'; '.join(o.get('fraud_indicators') or []) or '-'} | "
                f"{(o.get('rationale') or r.get('model_note') or '').replace('|', '/')} |")
        lines.append("")

    fb = [r for r in after["rows"] if r.get("fallback_errors")]
    if fb:
        lines += ["## Fallbacks: why the model's answer wasn't used", "",
                  "| Case | Reason |", "|---|---|"]
        lines += [f"| {r['case_id']} | {'; '.join(r['fallback_errors']).replace('|', '/')[:300]} |" for r in fb]
        lines.append("")

    b_by_id = {r["case_id"]: r for r in before["rows"]}
    lane_b = [r for r in after["rows"] if r["lane"] == "B"]
    if lane_b:
        lines += ["## Recall requests, case by case", "",
                  "| Case | True reason | Rules → action | Model → action |", "|---|---|---|---|"]
        for r in lane_b:
            b = b_by_id[r["case_id"]]
            lines.append(f"| {r['case_id']} | {r['code']} | {b['reason_proposed']} → {b['proposed']} | "
                         f"{r['reason_proposed']} → {r['proposed']} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=Path, required=True)
    ap.add_argument("--set", dest="set_", default="holdout", choices=["eval", "holdout"])
    ap.add_argument("--out", type=Path, default=Path("evals/results"))
    a = ap.parse_args()

    before = evaluate(a.cases, make_copilot("rules"), set_=a.set_)
    after = evaluate(a.cases, make_copilot("llm"), set_=a.set_)
    md = compare(before, after)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / f"{a.set_}_comparison.md").write_text(md)
    (a.out / f"{a.set_}_comparison.json").write_text(
        json.dumps({"before": before, "after": after}, indent=2, default=str) + "\n")
    print(md)
    ma = after["metrics"]
    sys.exit(0 if ma["gate_passed"] and ma.get("model_calls_fell_back", 0) == 0 else 1)


if __name__ == "__main__":
    main()
