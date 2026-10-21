import asyncio
import json
from pathlib import Path

import pytest

from gen.build import build
from iso20022_mcp import triage
from iso20022_mcp.parser import parse
from iso20022_mcp.server import server
from iso20022_mcp.triage import draft_repair, explain_reject
from iso20022_mcp.validator import validate_message


@pytest.fixture(scope="module")
def cases(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("cases")
    build(42, out)
    return out


def _each(cases):
    for d in sorted(cases.iterdir()):
        if d.is_dir() and (d / "pacs002.xml").exists():   # Lane C is pre-send
            yield d, json.loads((d / "label.json").read_text()), \
                (d / "pacs008.xml").read_bytes(), (d / "pacs002.xml").read_bytes()


def _swap_code(pacs002: bytes, old: str, new: str) -> bytes:
    return pacs002.replace(f"<Cd>{old}</Cd>".encode(), f"<Cd>{new}</Cd>".encode())


# ---------- scored against the golden set ----------

def test_explain_matches_label_on_every_case(cases):
    for d, label, p8, p2 in _each(cases):
        e = explain_reject(p2, p8)
        expected = label["action"] if label["lane"] == "A" else "none"
        assert e["recommended_action"] == expected, d.name


def test_draft_repair_matches_answer_key(cases):
    for d, label, p8, p2 in _each(cases):
        r = draft_repair(p8, p2)
        if label["action"] != "repair":
            assert r["status"] == "REFUSED", d.name
            continue
        assert r["status"] == "DRAFT_PENDING_APPROVAL", (d.name, r)
        assert [c["field"] for c in r["changes"]] == label["allowed_fields"], d.name
        assert r["changes"][0]["after"] == label["expected_value"], d.name
        assert r["post_repair_validation"]["valid"], d.name
        assert r["requires_human_approval"] is True


def test_repair_keeps_identity(cases):
    """A repair must never touch the payment's identity or amount."""
    for d, label, p8, p2 in _each(cases):
        if label["action"] != "repair":
            continue
        before = parse(p8)["transaction"]
        after = parse(draft_repair(p8, p2)["repaired_xml"])["transaction"]
        for k in ("uetr", "end_to_end_id", "instr_id", "amount", "creditor_account", "creditor"):
            assert before[k] == after[k], (d.name, k)


# ---------- guardrails ----------

def test_never_repairs_outreach_or_escalate_codes(cases):
    for d, label, p8, p2 in _each(cases):
        if label["lane"] == "A" and label["action"] in ("outreach", "escalate"):
            r = draft_repair(p8, p2)
            assert r["status"] == "REFUSED" and r["route_to"] == label["action"], d.name


def test_repairable_code_without_evidence_escalates(cases):
    """An RC01 reject on a message whose BIC is fine: don't 'fix' what isn't broken."""
    p8 = (cases / "A-AC04-01" / "pacs008.xml").read_bytes()
    p2 = _swap_code((cases / "A-AC04-01" / "pacs002.xml").read_bytes(), "AC04", "RC01")
    assert explain_reject(p2, p8)["recommended_action"] == "escalate"
    assert draft_repair(p8, p2)["status"] == "REFUSED"


def test_unknown_code_escalates(cases):
    p2 = _swap_code((cases / "A-AC04-01" / "pacs002.xml").read_bytes(), "AC04", "ZZ99")
    assert explain_reject(p2)["recommended_action"] == "escalate"


def test_mismatched_messages_refused(cases):
    p8 = (cases / "A-RC01-01" / "pacs008.xml").read_bytes()
    p2 = (cases / "A-RC01-02" / "pacs002.xml").read_bytes()
    assert "error" in explain_reject(p2, p8)
    assert draft_repair(p8, p2)["status"] == "REFUSED"


def test_allowlist_violation_discards_draft(cases, monkeypatch):
    """If a fixer ever changes a field outside its allowlist, the draft is thrown away."""
    def rogue_fix(root, ns):
        root.find(".//n:CdtTrfTxInf/n:IntrBkSttlmAmt", ns).text = "1.00"
    monkeypatch.setitem(triage._FIXERS, "DT01", rogue_fix)
    r = draft_repair((cases / "A-DT01-01" / "pacs008.xml").read_bytes(),
                     (cases / "A-DT01-01" / "pacs002.xml").read_bytes())
    assert r["status"] == "REFUSED" and "allowlist" in r["reason"]


def test_failed_post_validation_discards_draft(cases, monkeypatch):
    monkeypatch.setitem(triage._FIXERS, "DT01", lambda root, ns: None)  # "fix" that fixes nothing
    r = draft_repair((cases / "A-DT01-01" / "pacs008.xml").read_bytes(),
                     (cases / "A-DT01-01" / "pacs002.xml").read_bytes())
    assert r["status"] == "REFUSED" and "fails validation" in r["reason"]


def test_rc01_refused_without_agent_name(cases):
    p8 = (cases / "A-RC01-01" / "pacs008.xml").read_bytes()
    p8 = p8.replace(b"<Nm>" + parse(p8)["transaction"]["creditor_agent"]["name"].encode() + b"</Nm>", b"")
    r = draft_repair(p8, (cases / "A-RC01-01" / "pacs002.xml").read_bytes())
    assert r["status"] == "REFUSED" and r["route_to"] == "escalate"


def test_settled_payment_not_actionable(cases):
    e = explain_reject((cases / "B-DUPL-01" / "pacs002.xml").read_bytes(),
                       (cases / "B-DUPL-01" / "pacs008.xml").read_bytes())
    assert e["status"] == "ACSC" and e["recommended_action"] == "none"


# ---------- exposed over MCP ----------

def test_five_tools_registered():
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {"parse_message", "validate_message", "explain_reject", "draft_repair",
                     "structure_address"}


def test_draft_repair_over_mcp(cases):
    args = {"pacs008_xml": (cases / "A-RC01-01" / "pacs008.xml").read_text(),
            "pacs002_xml": (cases / "A-RC01-01" / "pacs002.xml").read_text()}
    r = asyncio.run(server.call_tool("draft_repair", args))
    assert r.structured_content["status"] == "DRAFT_PENDING_APPROVAL"


# ---------- RC01: repair keying errors only (feedback from a payments ops reviewer) ----------

from iso20022_mcp.triage import _keying_distance  # noqa: E402


def test_keying_distance():
    assert _keying_distance("PERUPEL0", "PERUPEL0") == 0
    assert _keying_distance("PREUPEL0", "PERUPEL0") == 1   # adjacent transposition
    assert _keying_distance("PXRUPEL0", "PERUPEL0") == 1   # one wrong letter
    assert _keying_distance("AAGEPEC0", "PERUPEL0") > 2    # a different bank


def test_rc01_typo_repaired_with_reason(cases):
    p8, p2 = (cases / "A-RC01-01" / "pacs008.xml").read_bytes(), (cases / "A-RC01-01" / "pacs002.xml").read_bytes()
    r = draft_repair(p8, p2)
    assert r["status"] == "DRAFT_PENDING_APPROVAL"
    assert "keying error" in r["why"] and "not in the BIC directory" in r["why"]


def test_rc01_wrong_bank_goes_to_client_not_repaired(cases):
    p8, p2 = (cases / "A-RC01-03" / "pacs008.xml").read_bytes(), (cases / "A-RC01-03" / "pacs002.xml").read_bytes()
    assert explain_reject(p2, p8)["recommended_action"] == "outreach"
    r = draft_repair(p8, p2)
    assert r["status"] == "REFUSED" and r["route_to"] == "outreach" and "not a typo" in r["reason"]
