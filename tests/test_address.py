"""Lane C: free-text creditor address -> CBPR+ structured/hybrid draft. No network, no key."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from copilot.agent import Copilot
from copilot.audit import AuditLog
from copilot.llm import AddressProposal, ClaudeModel
from evals.address import evaluate_addresses
from gen.addresses import build_addresses
from gen.build import build
from iso20022_mcp.address import cbpr_issues, check_proposal, classify, draft_address, structure
from iso20022_mcp.parser import parse
from iso20022_mcp.validator import validate_message
from ui.app import create_app


@pytest.fixture(scope="module")
def addr_cases(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("addr")
    build_addresses(42, out)
    return out


@pytest.fixture(scope="module")
def addr_holdout(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("addr-ho")
    build_addresses(42, out, holdout=True)
    return out


def _p8(cases, cid) -> bytes:
    return (cases / cid / "pacs008.xml").read_bytes()


def fake(responder) -> ClaudeModel:
    class M:
        def __init__(self):
            self.calls = []

        def parse(self, **kw):
            self.calls.append(kw)
            return SimpleNamespace(parsed_output=responder(kw))
    return ClaudeModel(model="claude-test", client=SimpleNamespace(messages=M()))


def proposal(**kw) -> AddressProposal:
    base = dict(strt_nm=None, bldg_nb=None, pst_cd=None, twn_nm=None, dstrct_nm=None,
                ctry_sub_dvsn=None, adr_lines=[], confidence=0.9, rationale="test")
    return AddressProposal(**{**base, **kw})


# ---------- CBPR+ rules ----------

def test_classify():
    assert classify(None) == "missing"
    assert classify({"adr_lines": ["1 Main St", "Town"]}) == "unstructured"
    assert classify({"twn_nm": "Lima", "ctry": "PE", "adr_lines": ["Of. 5"]}) == "hybrid"
    assert classify({"twn_nm": "Lima", "ctry": "PE", "strt_nm": "Av. Larco"}) == "structured"


def test_cbpr_issues():
    assert cbpr_issues({"twn_nm": "Lima", "ctry": "PE"}) == []
    assert "Town Name missing" in cbpr_issues({"ctry": "PE"})
    assert any("3 address lines" in i for i in cbpr_issues({"twn_nm": "L", "ctry": "PE", "adr_lines": ["a", "b", "c"]}))
    assert any("71 characters" in i for i in cbpr_issues({"twn_nm": "L", "ctry": "PE", "adr_lines": ["x" * 71]}))
    assert any("repeated" in i for i in cbpr_issues({"twn_nm": "Lima", "ctry": "PE", "adr_lines": ["Lima 15074"]}))


def test_invented_text_is_rejected():
    lines = ["Av. Larco 1301", "Miraflores", "Lima 15074", "PERU"]
    assert check_proposal({"strt_nm": "Av. Larco", "twn_nm": "Lima", "pst_cd": "15074", "ctry": "PE"}, lines, "PE") == []
    issues = check_proposal({"strt_nm": "Avenida Larco", "twn_nm": "Lima", "ctry": "PE"}, lines, "PE")
    assert any("invented" in i for i in issues)          # expanded abbreviation = invented
    assert any("does not match" in i for i in check_proposal({"twn_nm": "Lima", "ctry": "CL"}, lines, "PE"))
    assert any("postcode" in i for i in check_proposal({"twn_nm": "Lima", "pst_cd": "1301", "ctry": "PE"}, lines, "PE"))


def test_country_conflict_never_drafted():
    r = structure(["Calle 93 # 11-26", "Bogota, Cundinamarca", "110221", "COLOMBIA"], agent_bic="BMXFMXM0")
    assert r.status == "NEEDS_REVIEW" and "disagree" in r.reason


# ---------- the rules on the generated set ----------

def test_rules_on_address_set(addr_cases):
    m = evaluate_addresses(addr_cases, Copilot(), "address")["metrics"]
    assert m["unsafe"] == 0 and m["field_errors"] == 0
    assert m["correct"] == 21 and m["to_a_person"] == 3       # PE has no rules; conflict; no town


def test_rules_never_unsafe_on_holdout(addr_holdout):
    m = evaluate_addresses(addr_holdout, Copilot(), "address-holdout")["metrics"]
    assert m["unsafe"] == 0


def test_draft_is_valid_and_touches_only_the_address(addr_cases):
    r = draft_address(_p8(addr_cases, "C-MX-HYB"))
    assert r["status"] == "HYBRID" and r["format"] == "hybrid"
    before, after = parse(_p8(addr_cases, "C-MX-HYB"))["transaction"], parse(r["repaired_xml"].encode())["transaction"]
    assert after["creditor"]["address"]["twn_nm"] == "Ciudad de Mexico"
    assert after["creditor"]["address"]["adr_lines"] == ["Torre Azul Piso 12"]
    assert {**before, "creditor": None} == {**after, "creditor": None}
    assert validate_message(r["repaired_xml"])["schema_valid"]


def test_validator_flags_unstructured(addr_cases):
    rules = [f["rule"] for f in validate_message(_p8(addr_cases, "C-DE-1"))["findings"]]
    assert "cbpr_address" in rules


# ---------- the model fallback ----------

def test_model_structures_what_rules_cannot(addr_cases, tmp_path):
    m = fake(lambda kw: proposal(strt_nm="Av. Larco", bldg_nb="1301", dstrct_nm="Miraflores",
                                 twn_nm="Lima", pst_cd="15074"))
    p = Copilot(model=m, audit=AuditLog(tmp_path / "a.jsonl")).triage(addr_cases / "C-PE-NORULES")
    assert p.action == "repair" and "every value checked" in p.summary
    prompt = m._client.messages.calls[0]["messages"][0]["content"]
    assert "Country: PE" in prompt and "<address>" in prompt


def test_model_inventing_text_is_blocked(addr_cases, tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    m = fake(lambda kw: proposal(strt_nm="Avenida Larco", twn_nm="Lima"))   # expanded: not in the original
    p = Copilot(model=m, audit=log).triage(addr_cases / "C-PE-NORULES")
    assert p.action == "escalate" and "invented" in p.model_note
    [call] = [e["payload"] for e in log.events() if e["event"] == "model_call"]
    assert call["fallback_kind"] == "check_rejected" and call["rejected_output"]["strt_nm"] == "Avenida Larco"


def test_model_never_asked_when_country_conflicts(addr_cases, tmp_path):
    m = fake(lambda kw: pytest.fail("model must not be called on a country conflict"))
    p = Copilot(model=m, audit=AuditLog(tmp_path / "a.jsonl")).triage(addr_cases / "C-CONFLICT")
    assert p.action == "escalate"


def test_model_cannot_supply_a_missing_town(addr_cases, tmp_path):
    m = fake(lambda kw: proposal(strt_nm="Baker Street", bldg_nb="221B", twn_nm="London"))
    p = Copilot(model=m, audit=AuditLog(tmp_path / "a.jsonl")).triage(addr_cases / "C-NO-TOWN")
    assert p.action == "escalate" and "invented" in p.model_note


def test_low_confidence_goes_to_a_person(addr_cases, tmp_path):
    m = fake(lambda kw: proposal(strt_nm="Av. Larco", twn_nm="Lima", confidence=0.4))
    p = Copilot(model=m, audit=AuditLog(tmp_path / "a.jsonl")).triage(addr_cases / "C-PE-NORULES")
    assert p.action == "escalate" and "low confidence" in p.model_note


def test_rules_answer_first_model_not_called(addr_cases, tmp_path):
    m = fake(lambda kw: pytest.fail("rules handle MX; the model must not be called"))
    p = Copilot(model=m, audit=AuditLog(tmp_path / "a.jsonl")).triage(addr_cases / "C-MX-1")
    assert p.action == "repair" and "Country rules" in p.summary


# ---------- approval screen ----------

def test_demo_address_case_renders_and_approves(tmp_path):
    cases = tmp_path / "cases"
    build(42, cases)
    c = TestClient(create_app(cases, "demo", tmp_path / "audit.jsonl"))
    page = c.get("/case/DEMO-12-ADDR").text
    assert "Cdtr.PstlAdr.TwnNm" in page and "Sao Paulo" in page and "Why this repair" in page
    r = c.post("/case/DEMO-12-ADDR/decide", data={"reviewer": "ana", "decision": "approve"}, follow_redirects=False)
    assert r.status_code in (200, 303)
    assert "escalate" in c.get("/case/DEMO-13-ADDR").text.lower() or "person" in c.get("/case/DEMO-13-ADDR").text
