import hashlib
from pathlib import Path

import pytest

from copilot.agent import Copilot
from evals.run import evaluate
from gen.holdout import build_holdout
from gen.model import Label
from gen.validate import validate


@pytest.fixture(scope="module")
def holdout(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("holdout")
    build_holdout(42, out)
    return out


def _labels(root: Path) -> list[Label]:
    return [Label.model_validate_json((d / "label.json").read_text())
            for d in sorted(root.iterdir()) if d.is_dir()]


def test_holdout_shape(holdout):
    labels = _labels(holdout)
    assert len(labels) == 18 and all(l.set == "holdout" for l in labels)
    assert sum(l.code == "FRAD" for l in labels) == 5


def test_holdout_deterministic(holdout, tmp_path):
    build_holdout(42, tmp_path)
    h = lambda root: {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
                      for p in sorted(root.rglob("*")) if p.is_file()}
    assert h(tmp_path) == h(holdout)


def test_holdout_status_reports_valid(holdout):
    for d in sorted(holdout.iterdir()):
        if d.is_dir():
            assert not validate((d / "pacs002.xml").read_bytes(), "pacs.002.001.10"), d.name


def test_guardrails_generalize_to_unseen_reject_edges(holdout):
    """Every Lane A edge case is a trap where the obvious repair is wrong. All must escalate."""
    for l in _labels(holdout):
        if l.lane == "A":
            assert Copilot().triage(holdout / l.case_id).action == "escalate", l.case_id


def test_holdout_never_produces_a_false_repair(holdout):
    assert evaluate(holdout, set_="holdout")["metrics"]["false_repairs"] == 0


def test_explicit_fraud_control_case_caught(holdout):
    assert Copilot().triage(holdout / "H-B-FRAD-WORD").action == "escalate"


@pytest.mark.xfail(strict=True, reason="Known gap: keyword rules miss fraud described without "
                   "fraud words (BEC, lookalike domain, callback, Portuguese). The model layer "
                   "must close this; when it does, this xfail turns into a failure to flip.")
def test_rules_baseline_catches_implicit_fraud(holdout):
    assert evaluate(holdout, set_="holdout")["metrics"]["zero_miss_misses"] == 0
