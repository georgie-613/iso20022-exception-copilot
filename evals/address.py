"""Score Lane C: free-text creditor address -> CBPR+ structured/hybrid draft.

    python -m evals.address --cases cases_address --set address
    python -m evals.address --cases cases_address_holdout --set address-holdout --compare   # needs a key

Metrics:
  correct          drafted, and street, number, postcode, town and country all
                   match the answer key
  unsafe           a draft with the wrong TOWN or COUNTRY (what sanctions
                   screening reads), or any draft where the label says a
                   person must decide. Hard gate: must be 0.
  field errors     drafted, town and country right, another field wrong
                   (e.g. the district put in the street). Reported.
  to a person      NEEDS_REVIEW: safe, costs analyst time. Reported.
  blocked          model outputs rejected by the checks (invented text,
                   invalid CBPR+, wrong postcode format). The control working.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from copilot.agent import Copilot
from gen.model import Label
from iso20022_mcp.address import norm
from iso20022_mcp.parser import parse

from .run import make_copilot

CORE = ("strt_nm", "bldg_nb", "pst_cd", "twn_nm", "ctry")
CRITICAL = ("twn_nm", "ctry")


def _proposed_address(p) -> dict | None:
    if p.action != "repair" or not p.repair:
        return None
    return parse(p.repair["repaired_xml"].encode())["transaction"]["creditor"]["address"]


def _same(a, b) -> bool:
    return norm(str(a or "")) == norm(str(b or ""))


def evaluate_addresses(cases: Path, copilot: Copilot, set_: str) -> dict:
    rows = []
    for d in sorted(cases.iterdir()):
        if not (d / "label.json").exists():
            continue
        label = Label.model_validate_json((d / "label.json").read_text())
        if label.set != set_:
            continue
        p = copilot.triage(d)
        got, truth = _proposed_address(p), label.expected_value
        calls = [e["payload"] for e in (copilot.audit.for_case(label.case_id) if copilot.audit else [])
                 if e["event"] == "model_call"]
        row = {"case_id": label.case_id, "expected": label.action, "proposed": p.action,
               "summary": p.summary, "model_note": p.model_note,
               "blocked": [c["error"] for c in calls if c.get("fallback_kind") == "check_rejected"],
               "api_failures": [c["error"] for c in calls if c.get("fallback_kind") == "api_error"],
               "by_model": any(not c["fallback"] for c in calls) and got is not None}
        if got is None:
            row["outcome"] = "to a person"
        elif label.action != "repair" or truth is None:
            row["outcome"], row["why"] = "unsafe", "drafted where a person must decide"
        else:
            wrong = [f for f in CORE if (got.get(f) or truth.get(f)) and not _same(got.get(f), truth.get(f))]
            row["wrong_fields"] = {f: {"proposed": got.get(f), "expected": truth.get(f)} for f in wrong}
            if any(f in CRITICAL for f in wrong):
                row["outcome"] = "unsafe"
            else:
                row["outcome"] = "field errors" if wrong else "correct"
        row["address"] = got
        rows.append(row)

    n = len(rows)
    count = lambda o: sum(r["outcome"] == o for r in rows)  # noqa: E731
    could_draft = sum(r["expected"] == "repair" for r in rows)
    m = {
        "set": set_, "model": copilot.model.name, "cases": n,
        "correct": count("correct"), "could_draft": could_draft,
        "unsafe": count("unsafe"), "field_errors": count("field errors"),
        "to_a_person": count("to a person"),
        "drafted_by_model": sum(r["by_model"] for r in rows),
        "model_outputs_blocked": sum(len(r["blocked"]) for r in rows),
        "model_api_failures": sum(len(r["api_failures"]) for r in rows),
    }
    m["gate_passed"] = m["unsafe"] == 0 and m["model_api_failures"] == 0
    return {"metrics": m, "rows": rows}


def to_markdown(before: dict, after: dict | None = None) -> str:
    cols = [before] + ([after] if after else [])
    head = "| Metric | " + " | ".join(f"`{c['metrics']['model']}`" for c in cols) + " |"
    lines = [f"# Address structuring: {before['metrics']['set']} set ({before['metrics']['cases']} cases)", "",
             f"Run {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC.", "", head,
             "|---|" + "---|" * len(cols)]
    for title, key in [("Correct drafts", "correct"), ("**Unsafe** (wrong town/country, or drafted when a person must decide)", "unsafe"),
                       ("Field errors (town and country right)", "field_errors"), ("Sent to a person", "to_a_person"),
                       ("Drafted by the model", "drafted_by_model"),
                       ("Model outputs blocked by checks", "model_outputs_blocked"),
                       ("Model calls that failed", "model_api_failures")]:
        vals = []
        for c in cols:
            v = c["metrics"][key]
            vals.append(f"{v} of {c['metrics']['could_draft']}" if key == "correct" else str(v))
        lines.append(f"| {title} | " + " | ".join(vals) + " |")
    last = cols[-1]
    lines += ["", f"**Hard gate (unsafe = 0, no failed model calls): "
                  f"{'PASSED' if last['metrics']['gate_passed'] else 'FAILED'}**", "",
              "## Case by case", "", "| Case | Label | " + " | ".join(c["metrics"]["model"] for c in cols) + " |",
              "|---|---|" + "---|" * len(cols)]
    by_case = [{r["case_id"]: r for r in c["rows"]} for c in cols]
    for cid in by_case[0]:
        cells = []
        for bc in by_case:
            r = bc[cid]
            cell = r["outcome"]
            if r.get("wrong_fields"):
                cell += ": " + ", ".join(f"{f} {v['proposed']!r} (expected {v['expected']!r})"
                                         for f, v in r["wrong_fields"].items())
            if r["blocked"]:
                cell += f" · blocked: {r['blocked'][0][:120]}"
            cells.append(cell)
        lines.append(f"| {cid} | {by_case[0][cid]['expected']} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=Path, default=Path("cases_address"))
    ap.add_argument("--set", dest="set_", default="address", choices=["address", "address-holdout", "demo"])
    ap.add_argument("--model", choices=["rules", "llm"], default="rules")
    ap.add_argument("--compare", action="store_true", help="rules vs model side by side (needs a key)")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--out", type=Path, default=Path("evals/report-address"))
    a = ap.parse_args()
    before = evaluate_addresses(a.cases, make_copilot("rules"), a.set_)
    after = evaluate_addresses(a.cases, make_copilot("llm"), a.set_) if a.compare or a.model == "llm" else None
    md = to_markdown(before, after)
    a.out.mkdir(parents=True, exist_ok=True)
    name = f"{a.set_}_address"
    (a.out / f"{name}.md").write_text(md)
    (a.out / f"{name}.json").write_text(json.dumps({"before": before, "after": after}, indent=2, default=str) + "\n")
    print(md)
    gate = (after or before)["metrics"]["gate_passed"]
    sys.exit(0 if a.report_only or gate else 1)


if __name__ == "__main__":
    main()
