"""PR 8 harness tests. The fakes here test the plumbing (table, failure analysis,
gates, fallback accounting). They say nothing about how well Claude performs;
only the live-eval workflow does."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from copilot.agent import Copilot
from copilot.audit import AuditLog
from copilot.llm import ClaudeModel, RecallClassification
from evals.compare import compare
from evals.run import evaluate, make_copilot
from gen.holdout import RECALLS, build_holdout


@pytest.fixture(scope="module")
def holdout(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("holdout")
    build_holdout(42, out)
    return out


def _fake(responder, name="claude-fake") -> ClaudeModel:
    class M:
        def parse(self, **kw):
            return SimpleNamespace(parsed_output=responder(kw))
    return ClaudeModel(model=name, client=SimpleNamespace(messages=M()))


def _oracle(kw):
    """Answers from the held-out answer key: a stand-in for a perfect model."""
    text = kw["messages"][0]["content"]
    for _, reason, _, template in RECALLS:
        if template.split("{e2e}")[0].strip()[:30] in text:
            return RecallClassification(reason=reason, confidence=0.95, rationale="oracle",
                                        fraud_indicators=["stated in request"] if reason == "FRAD" else [])
    raise AssertionError("unknown request")


def _run(holdout, model, tmp_path):
    return evaluate(holdout, Copilot(model=model, audit=AuditLog(tmp_path / "a.jsonl")), set_="holdout")


def test_llm_eval_refuses_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="refusing"):
        make_copilot("llm")


def test_comparison_table_and_gate(holdout, tmp_path):
    before = evaluate(holdout, make_copilot("rules"), set_="holdout")
    after = _run(holdout, _fake(_oracle), tmp_path)
    md = compare(before, after)
    assert "| Fraud miss rate (FRAD not escalated) | 4 of 5 | 0 of 5 |" in md
    assert "| Recall reason accuracy | 25% | 100% |" in md
    assert "| False repairs | 0 | 0 |" in md
    assert "PASSED" in md and "None on this set." in md


def test_failure_analysis_shows_model_rationale(holdout, tmp_path):
    def naive(kw):
        return RecallClassification(reason="CUST", fraud_indicators=[], confidence=0.9,
                                    rationale="client asks to cancel")
    before = evaluate(holdout, make_copilot("rules"), set_="holdout")
    after = _run(holdout, _fake(naive), tmp_path)
    md = compare(before, after)
    assert "FAILED" in md
    assert "| H-B-FRAD-CALLBACK | escalate | recall | CUST | 0.9 | - | client asks to cancel |" in md


def test_fallbacks_are_counted_and_fail_the_run(holdout, tmp_path):
    def down(kw):
        raise ConnectionError("network down")
    after = _run(holdout, _fake(down), tmp_path)
    assert after["metrics"]["model_calls_fell_back"] == 12      # every Lane B call
    md = compare(evaluate(holdout, make_copilot("rules"), set_="holdout"), after)
    assert "FAILED" in md   # a run that fell back is not a model result
    assert "## Fallbacks: why the model's answer wasn't used" in md
    assert "classify_recall: ConnectionError: network down" in md


def test_guardrail_rejection_is_reported_not_failed(tmp_path):
    """A model output blocked by a check is the control working: shown, not a failed run."""
    from gen.build import build
    from copilot.llm import OutreachDraft
    cases = tmp_path / "cases"
    build(42, cases)

    def write(kw):
        if "end_to_end_id" in kw["messages"][0]["content"]:
            return OutreachDraft(message="We will never ask for your password.", confidence=0.9)
        raise AssertionError("unexpected call")

    def classify(kw):
        return RecallClassification(reason="FRAD", fraud_indicators=["x"], confidence=0.9, rationale="r")

    def responder(kw):
        return write(kw) if "Facts:" in kw["messages"][0]["content"] else classify(kw)

    after = evaluate(cases, Copilot(model=_fake(responder), audit=AuditLog(tmp_path / "a.jsonl")))
    m = after["metrics"]
    assert m["model_api_failures"] == 0 and m["model_outputs_rejected"] > 0
    md = compare(evaluate(cases, make_copilot("rules")), after)
    assert "## Outputs blocked by guardrails" in md
    assert "We will never ask for your password." in md
    assert "no failed model calls): PASSED" in md


def test_rejected_output_and_prompt_hash_are_audited(tmp_path):
    from gen.build import build
    from copilot.llm import OutreachDraft
    cases = tmp_path / "cases"
    build(42, cases)
    log = AuditLog(tmp_path / "a.jsonl")
    m = _fake(lambda kw: OutreachDraft(message="Reply with your PIN.", confidence=0.9))
    Copilot(model=m, audit=log).triage(cases / "A-AC04-01")
    [e] = [e["payload"] for e in log.events() if e["event"] == "model_call"]
    assert e["fallback_kind"] == "check_rejected"
    assert e["rejected_output"]["message"] == "Reply with your PIN."
    assert e["prompt_sha256"] and len(e["prompt_sha256"]) == 64   # kept even though the output was blocked
