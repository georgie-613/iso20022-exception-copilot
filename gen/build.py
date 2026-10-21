"""Generate the case set.

    python -m gen.build --seed 42 --out cases

Writes cases/<case_id>/{pacs008.xml, pacs002.xml, label.json[, request.txt]}
plus cases/manifest.json. Fully deterministic from the seed.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
from pathlib import Path

from .base import build_payment, to_xml
from .directory import Directory
from .model import Label
from .mutators import LANE_A, SCHEMA_INVALID_CODES, rc01_wrong_bank
from .recall import recall_request
from .status import to_pacs002
from .validate import validate

# Eval set: 40 Lane A (3 per code, 2 for AC06/AG01) + 10 Lane B
EVAL_PLAN: list[tuple[str, str, int]] = (
    [("A", c, 2 if c in ("AC06", "AG01") else 3) for c in LANE_A]
    + [("B", "DUPL", 3), ("B", "CUST", 3), ("B", "TECH", 2), ("B", "FRAD", 2)]
)
# Demo set: 11 cases for walkthroughs, never scored. The 3-minute story uses
# DEMO-01-RC01 (repair), DEMO-05-AC01 (outreach) and DEMO-11-FRAD (implicit
# fraud, written without the rules' keywords; see recall.IMPLICIT_FRAD).
DEMO_PLAN: list[tuple[str, str]] = [
    ("A", "RC01"), ("A", "CH16"), ("A", "RR02"), ("A", "DT01"), ("A", "AC01"),
    ("A", "RR03"), ("A", "RR04"), ("A", "MS03"), ("B", "DUPL"), ("B", "FRAD"),
    ("B", "FRAD"),
]
IMPLICIT_DEMO = {"DEMO-11-FRAD"}
# One RC01 eval case is a wrong bank, not a typo: it must go to the client, not be repaired.
WRONG_BANK = {"A-RC01-03"}


def plan() -> list[tuple[str, str, str, str]]:
    """(case_id, set, lane, code) for every case, in a stable order."""
    out = []
    for lane, code, n in EVAL_PLAN:
        for i in range(1, n + 1):
            out.append((f"{lane}-{code}-{i:02d}", "eval", lane, code))
    for i, (lane, code) in enumerate(DEMO_PLAN, 1):
        out.append((f"DEMO-{i:02d}-{code}", "demo", lane, code))
    return out


def build_case(seed: int, case_id: str, set_: str, lane: str, code: str,
               directory: Directory) -> dict[str, bytes]:
    rng = random.Random(f"{seed}:{case_id}")
    payment = build_payment(rng, directory)
    files: dict[str, bytes] = {}

    if lane == "A":
        mutate = rc01_wrong_bank if case_id in WRONG_BANK else LANE_A[code]
        m = mutate(payment, rng, directory)
        pacs008 = to_xml(payment)
        pacs002 = to_pacs002(payment, rng, "RJCT", code, m.addtl_inf)
        label = Label(case_id=case_id, set=set_, lane="A", code=code, action=m.action,
                      allowed_fields=m.allowed_fields, expected_value=m.expected_value,
                      rationale=m.rationale)
    else:
        pacs008 = to_xml(payment)
        pacs002 = to_pacs002(payment, rng, "ACSC")
        text, action, rationale = recall_request(code, payment, rng, implicit=case_id in IMPLICIT_DEMO)
        files["request.txt"] = (text + "\n").encode()
        label = Label(case_id=case_id, set=set_, lane="B", code=code, action=action,
                      rationale=rationale)

    errors = validate(pacs008, "pacs.008.001.08")
    if code in SCHEMA_INVALID_CODES:
        if not errors:
            raise ValueError(f"{case_id}: expected an XSD failure but message is valid")
    elif errors:
        raise ValueError(f"{case_id}: pacs.008 fails XSD: {errors[:3]}")
    if errs := validate(pacs002, "pacs.002.001.10"):
        raise ValueError(f"{case_id}: pacs.002 fails XSD: {errs[:3]}")

    files["pacs008.xml"] = pacs008
    files["pacs002.xml"] = pacs002
    files["label.json"] = (json.dumps(label.model_dump(), indent=2, sort_keys=True) + "\n").encode()
    return files


def build(seed: int, out: Path) -> list[str]:
    directory = Directory()
    cases = plan()
    if out.exists():
        for child in out.iterdir():
            if child.is_dir() and (child / "label.json").exists():
                shutil.rmtree(child)
    out.mkdir(parents=True, exist_ok=True)

    for case_id, set_, lane, code in cases:
        case_dir = out / case_id
        case_dir.mkdir()
        for name, data in build_case(seed, case_id, set_, lane, code, directory).items():
            (case_dir / name).write_bytes(data)

    # Lane C demo cases: free-text creditor addresses to structure before sending
    from .addresses import DEMO_ADDRESSES, build_case as build_address_case
    for spec in DEMO_ADDRESSES:
        (out / spec[0]).mkdir()
        for name, data in build_address_case(seed, spec[0], "demo", spec, directory).items():
            (out / spec[0] / name).write_bytes(data)
        cases.append((spec[0], "demo", "C", "ADDR"))

    manifest = {
        "seed": seed,
        "count": len(cases),
        "cases": [{"id": c, "set": s, "lane": l, "code": k} for c, s, l, k in cases],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return [c[0] for c in cases]


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate synthetic ISO 20022 exception cases")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, default=Path("cases"))
    args = ap.parse_args()
    ids = build(args.seed, args.out)
    print(f"wrote {len(ids)} cases to {args.out}/")


if __name__ == "__main__":
    main()
