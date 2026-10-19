"""Held-out case set: cases the rules were NOT written against.

    python -m gen.holdout --seed 42 --out cases_holdout

The main set was generated alongside the rules, so it can't surprise them.
This set is written to: paraphrased recall requests with none of the rules'
keywords, Spanish and Portuguese requests from LATAM clients, fraud described
without the word "fraud", and reject edge cases where the obvious repair is
the wrong move. Labels are what an experienced ops analyst would do.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
from pathlib import Path
from typing import Callable

from .base import build_payment, to_xml
from .directory import Directory
from .model import Label, Party
from .mutators import rc01
from .status import to_pacs002
from .validate import validate

# (id suffix, true reason, labeled action, request template)
RECALLS: list[tuple[str, str, str, str]] = [
    ("DUPL-AP", "DUPL", "recall",
     "Our treasury team and AP both released the same invoice. Please pull back {e2e}."),
    ("DUPL-CHQ", "DUPL", "recall",
     "This invoice was already settled by cheque last week; wire {e2e} pays it a second time."),
    ("DUPL-ES", "DUPL", "recall",
     "Por favor anulen el pago {e2e}, se envió dos veces por error."),
    ("TECH-H2H", "TECH", "recall",
     "File re-transmitted after a timeout on our host-to-host link, so {e2e} is a resend. Please recall."),
    ("TECH-GLITCH", "TECH", "recall",
     "A glitch in our payments platform released {e2e} before it was approved internally."),
    ("CUST-ORDER", "CUST", "recall",
     "The client cancelled the order; please recover the funds sent in {e2e}."),
    ("CUST-PT", "CUST", "recall",
     "Favor cancelar o pagamento {e2e}; o contrato com o fornecedor foi encerrado."),
    ("FRAD-CALLBACK", "FRAD", "escalate",
     "The supplier called us: they never changed their bank details and never got {e2e}. "
     "The change request came by email."),
    ("FRAD-CFO", "FRAD", "escalate",
     "Our CFO never approved {e2e}. The instruction came from a lookalike email domain."),
    ("FRAD-BEC", "FRAD", "escalate",
     "Possible BEC on {e2e}. Stop it if you can."),
    ("FRAD-PT", "FRAD", "escalate",
     "O pagamento {e2e} foi um golpe: o fornecedor nunca mudou a conta bancária."),
    ("FRAD-WORD", "FRAD", "escalate",
     "We believe {e2e} is fraudulent; please recall immediately."),
]


def _swap(xml: bytes, old: str, new: str) -> bytes:
    assert old.encode() in xml, old
    return xml.replace(old.encode(), new.encode(), 1)


# Lane A edge cases: each returns (pacs008, pacs002, code, action, rationale)
EdgeFn = Callable[[random.Random, Directory], tuple[bytes, bytes, str, str, str]]


def edge_rc01_no_name(rng, d):
    p = build_payment(rng, d)
    rc01(p, rng, d)
    p8 = _swap(to_xml(p), f"<Nm>{p.creditor_agent.name}</Nm>", "")
    return (p8, to_pacs002(p, rng, "RJCT", "RC01", "BIC NOT FOUND"), "RC01", "escalate",
            "Bad BIC but no agent name on the message: nothing to look the right BIC up by.")


def edge_rc01_bic_fine(rng, d):
    p = build_payment(rng, d)
    return (to_xml(p), to_pacs002(p, rng, "RJCT", "RC01", "BIC NOT FOUND"), "RC01", "escalate",
            "Reject says bad BIC but the BIC is valid and listed: don't fix what isn't broken.")


def edge_rr02_unknown_debtor(rng, d):
    p = build_payment(rng, d)
    p.debtor = Party(name="Mangrove Dental Group LLC", address=None)
    return (to_xml(p), to_pacs002(p, rng, "RJCT", "RR02", "MISSING DEBTOR ADDRESS"), "RR02", "escalate",
            "Debtor not in our customer master: no trusted source for the address.")


def edge_ch16_other_field(rng, d):
    p = build_payment(rng, d)
    status = to_pacs002(p, rng, "RJCT", "CH16", "SCHEMA VALIDATION FAILED")
    p.end_to_end_id = p.end_to_end_id + "-OVERLENGTH-REFERENCE-X"   # > 35 chars
    return (to_xml(p), status, "CH16", "escalate",
            "Format error in a reference field: shortening a client reference is not ours to decide.")


def edge_unknown_code(rng, d):
    p = build_payment(rng, d)
    return (to_xml(p), to_pacs002(p, rng, "RJCT", "AM09", "WRONG AMOUNT"), "AM09", "escalate",
            "Reason code outside the v1 policy: a person handles it.")


def edge_dt01_date_fine(rng, d):
    p = build_payment(rng, d)
    return (to_xml(p), to_pacs002(p, rng, "RJCT", "DT01", "INVALID DATE"), "DT01", "escalate",
            "Reject says invalid date, but the date is a business day: cause is elsewhere.")


EDGES: dict[str, EdgeFn] = {
    "RC01-NONAME": edge_rc01_no_name,
    "RC01-BICFINE": edge_rc01_bic_fine,
    "RR02-UNKNOWN": edge_rr02_unknown_debtor,
    "CH16-E2E": edge_ch16_other_field,
    "AM09-UNKNOWN": edge_unknown_code,
    "DT01-DATEFINE": edge_dt01_date_fine,
}


def build_holdout(seed: int, out: Path) -> list[str]:
    d = Directory()
    if out.exists():
        for child in out.iterdir():
            if child.is_dir() and (child / "label.json").exists():
                shutil.rmtree(child)
    out.mkdir(parents=True, exist_ok=True)
    ids = []

    for tag, fn in EDGES.items():
        cid = f"H-A-{tag}"
        rng = random.Random(f"holdout:{seed}:{cid}")
        p8, p2, code, action, why = fn(rng, d)
        if errs := validate(p2, "pacs.002.001.10"):
            raise ValueError(f"{cid}: pacs.002 invalid: {errs[:2]}")
        label = Label(case_id=cid, set="holdout", lane="A", code=code, action=action, rationale=why)
        _write(out / cid, p8, p2, label)
        ids.append(cid)

    for tag, code, action, template in RECALLS:
        cid = f"H-B-{tag}"
        rng = random.Random(f"holdout:{seed}:{cid}")
        p = build_payment(rng, d)
        label = Label(case_id=cid, set="holdout", lane="B", code=code, action=action,
                      rationale="Held-out paraphrase: none of the baseline's keywords by design."
                      if tag != "FRAD-WORD" else "Control: explicit fraud wording.")
        _write(out / cid, to_xml(p), to_pacs002(p, rng, "ACSC"), label,
               request=template.format(e2e=p.end_to_end_id))
        ids.append(cid)

    (out / "manifest.json").write_text(json.dumps({"seed": seed, "set": "holdout", "count": len(ids),
                                                   "cases": ids}, indent=2) + "\n")
    return ids


def _write(case_dir: Path, p8: bytes, p2: bytes, label: Label, request: str | None = None) -> None:
    case_dir.mkdir()
    (case_dir / "pacs008.xml").write_bytes(p8)
    (case_dir / "pacs002.xml").write_bytes(p2)
    (case_dir / "label.json").write_text(json.dumps(label.model_dump(), indent=2, sort_keys=True) + "\n")
    if request is not None:
        (case_dir / "request.txt").write_text(request + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate the held-out case set")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, default=Path("cases_holdout"))
    a = ap.parse_args()
    print(f"wrote {len(build_holdout(a.seed, a.out))} held-out cases to {a.out}/")


if __name__ == "__main__":
    main()
