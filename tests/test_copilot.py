import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from copilot.agent import Copilot, RulesBaseline
from copilot.approval import ApprovalError, ApprovalQueue
from copilot.audit import AuditLog
from evals.run import evaluate, to_markdown
from gen.build import build


@pytest.fixture(scope="module")
def cases(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("cases")
    build(42, out)
    return out


def _clock():
    t = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    while True:
        yield t
        t += timedelta(seconds=1)


@pytest.fixture
def audit(tmp_path) -> AuditLog:
    g = _clock()
    return AuditLog(tmp_path / "audit.jsonl", clock=lambda: next(g))


# ---------- copilot ----------

def test_repair_proposal_carries_draft(cases):
    p = Copilot().triage(cases / "A-RC01-01")
    assert p.action == "repair" and p.repair["status"] == "DRAFT_PENDING_APPROVAL"


def test_outreach_proposal_has_message(cases):
    p = Copilot().triage(cases / "A-AC01-01")
    assert p.action == "outreach" and "account number" in p.outreach_message
    assert "not resubmitted" in p.outreach_message


def test_recall_proposal_has_camt056_fields(cases):
    p = Copilot().triage(cases / "B-DUPL-01")
    assert p.action == "recall" and p.recall["cancellation_reason"] == "DUPL"
    assert p.recall["orgnl_uetr"] and p.recall["message"] == "camt.056.001.08"


class _GullibleModel(RulesBaseline):
    """A model that misreads a fraud report as a routine cancellation."""
    name = "gullible"

    def classify_recall(self, request_text: str) -> str:
        return "CUST"


@pytest.mark.parametrize("text", [
    "Please recall wire E2E-1; our vendor's email account was hacked and the bank details changed.",
    "Recall E2E-1 - this was an unauthorized payment.",
    "We think we got phished, cancel E2E-1.",
])
def test_fraud_signal_overrides_model(tmp_path, cases, text):
    d = tmp_path / "case"
    d.mkdir()
    for f in ("pacs008.xml", "pacs002.xml"):
        (d / f).write_bytes((cases / "B-CUST-01" / f).read_bytes())
    (d / "request.txt").write_text(text)
    p = Copilot(model=_GullibleModel()).triage(d)
    assert p.action == "escalate" and p.recall is None


# ---------- audit log ----------

def test_audit_chain_verifies_and_detects_tampering(audit):
    for i in range(3):
        audit.append(f"C{i}", "copilot", "proposed", {"i": i})
    assert audit.verify() == (True, "ok")

    lines = audit.path.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["payload"]["i"] = 99                       # quietly edit history
    lines[1] = json.dumps(rec, sort_keys=True)
    audit.path.write_text("\n".join(lines) + "\n")
    ok, why = audit.verify()
    assert not ok and "seq 2" in why


def test_audit_detects_deleted_line(audit):
    for i in range(3):
        audit.append(f"C{i}", "copilot", "proposed")
    lines = audit.path.read_text().splitlines()
    audit.path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert not audit.verify()[0]


# ---------- approval gate ----------

def test_maker_checker(cases, audit):
    q = ApprovalQueue(audit)
    q.submit(Copilot().triage(cases / "A-RC01-01"))
    with pytest.raises(ApprovalError):
        q.decide("A-RC01-01", "copilot", "approve")
    with pytest.raises(ApprovalError):
        q.decide("A-RC01-01", "", "approve")
    q.decide("A-RC01-01", "jorge", "approve")
    events = [(e["actor"], e["event"]) for e in audit.for_case("A-RC01-01")]
    assert events == [("copilot", "proposed"), ("jorge", "approved")]
    assert audit.verify()[0]


def test_rejection_needs_reason_and_is_final(cases, audit):
    q = ApprovalQueue(audit)
    q.submit(Copilot().triage(cases / "A-DT01-01"))
    with pytest.raises(ApprovalError):
        q.decide("A-DT01-01", "jorge", "reject")
    q.decide("A-DT01-01", "jorge", "reject", note="client asked to hold")
    with pytest.raises(ApprovalError):
        q.decide("A-DT01-01", "jorge", "approve")
    with pytest.raises(ApprovalError):
        q.submit(Copilot().triage(cases / "A-DT01-01"))


def test_proposed_event_pins_the_draft(cases, audit):
    q = ApprovalQueue(audit)
    q.submit(Copilot().triage(cases / "A-RR02-01"))
    payload = audit.for_case("A-RR02-01")[0]["payload"]
    assert len(payload["draft_sha256"]) == 64 and payload["changes"][0]["field"] == "Dbtr.PstlAdr"


# ---------- eval ----------

def test_eval_gate_passes_on_baseline(cases):
    r = evaluate(cases)
    m = r["metrics"]
    assert m["cases"] == 50
    assert m["false_repairs"] == 0 and m["zero_miss_misses"] == 0 and m["gate_passed"]
    assert "Gate: PASSED" in to_markdown(r)


class _ReckessModel(RulesBaseline):
    name = "reckless"

    def classify_recall(self, request_text: str) -> str:
        return "DUPL"


def test_eval_gate_stays_up_with_bad_model(cases):
    """Even a model that labels everything DUPL can't make a fraud case skip a human."""
    m = evaluate(cases, Copilot(model=_ReckessModel()))["metrics"]
    assert m["zero_miss_misses"] == 0
    assert m["recall_reason_acc"] < 1.0
