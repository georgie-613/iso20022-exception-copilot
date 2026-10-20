import hashlib
import json
from pathlib import Path

import pytest

from gen.build import build
from gen.customers import debtor_master, iban_is_valid
from gen.directory import Directory
from gen.model import Label
from gen.mutators import LANE_A, SCHEMA_INVALID_CODES
from gen.recall import LANE_B_CODES
from gen.validate import validate

ALL_CODES = set(LANE_A) | set(LANE_B_CODES)


def _tree_hash(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def cases(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("cases")
    build(42, out)
    return out


def _labels(cases: Path) -> list[Label]:
    return [Label.model_validate_json((d / "label.json").read_text())
            for d in sorted(cases.iterdir()) if d.is_dir()]


def test_case_counts(cases):
    labels = _labels(cases)
    assert len(labels) == 61
    assert sum(l.set == "eval" for l in labels) == 50
    assert sum(l.set == "eval" and l.lane == "A" for l in labels) == 40
    assert sum(l.set == "eval" and l.lane == "B" for l in labels) == 10
    assert sum(l.set == "demo" for l in labels) == 11


def test_all_18_codes_present_in_eval(cases):
    codes = {l.code for l in _labels(cases) if l.set == "eval"}
    assert codes == ALL_CODES and len(codes) == 18


def test_byte_identical_rerun(cases, tmp_path):
    build(42, tmp_path)
    assert _tree_hash(tmp_path) == _tree_hash(cases)


def test_different_seed_differs(cases, tmp_path):
    build(7, tmp_path)
    assert _tree_hash(tmp_path) != _tree_hash(cases)


def test_xsd_validity(cases):
    for d in sorted(cases.iterdir()):
        if not d.is_dir():
            continue
        label = Label.model_validate_json((d / "label.json").read_text())
        errs = validate((d / "pacs008.xml").read_bytes(), "pacs.008.001.08")
        if label.code in SCHEMA_INVALID_CODES:
            assert errs, f"{d.name} should fail the XSD"
        else:
            assert not errs, f"{d.name}: {errs}"
        assert not validate((d / "pacs002.xml").read_bytes(), "pacs.002.001.10"), d.name


def test_status_matches_lane(cases):
    for l in _labels(cases):
        xml = (cases / l.case_id / "pacs002.xml").read_text()
        if l.lane == "A":
            assert "<TxSts>RJCT</TxSts>" in xml and f"<Cd>{l.code}</Cd>" in xml
        else:
            assert "<TxSts>ACSC</TxSts>" in xml
            assert (cases / l.case_id / "request.txt").exists()


def test_repair_labels_are_complete(cases):
    for l in _labels(cases):
        if l.action == "repair":
            assert l.allowed_fields and l.expected_value is not None, l.case_id
        else:
            assert not l.allowed_fields and l.expected_value is None, l.case_id


def test_guardrail_codes_never_repairable(cases):
    for l in _labels(cases):
        if l.code in {"RR04", "MS03", "FRAD", "AC06"}:
            assert l.action == "escalate", l.case_id
        if l.code in {"AC01", "RR03", "BE01", "AC04", "AM04"}:
            assert l.action == "outreach", l.case_id


def test_rc01_bic_is_unlisted_and_recoverable(cases):
    d = Directory()
    for l in _labels(cases):
        if l.code != "RC01":
            continue
        xml = (cases / l.case_id / "pacs008.xml").read_text()
        cdtr_agt = xml.split("<CdtrAgt>")[1].split("</CdtrAgt>")[0]
        bad = cdtr_agt.split("<BICFI>")[1].split("</BICFI>")[0]
        name = cdtr_agt.split("<Nm>")[1].split("</Nm>")[0]
        assert bad not in d
        assert d.find_by_name(name)["bic"] == l.expected_value


def test_rr02_expected_address_from_master(cases):
    master = debtor_master()
    for l in _labels(cases):
        if l.code != "RR02":
            continue
        xml = (cases / l.case_id / "pacs008.xml").read_text()
        dbtr = xml.split("<Dbtr>")[1].split("</Dbtr>")[0]
        assert "<PstlAdr>" not in dbtr
        name = dbtr.split("<Nm>")[1].split("</Nm>")[0]
        assert master[name].model_dump() == l.expected_value


def test_all_bics_are_test_bics(cases):
    """Location code 2nd char '0' marks a test BIC: never a live production BIC."""
    import re
    for p in cases.rglob("*.xml"):
        for bic in re.findall(r"<BICFI>([A-Z0-9]+)</BICFI>", p.read_text()):
            assert bic[7] == "0", f"{p}: {bic}"


def test_valid_ibans_except_ac01(cases):
    import re
    for l in _labels(cases):
        xml = (cases / l.case_id / "pacs008.xml").read_text()
        for iban in re.findall(r"<IBAN>([A-Z0-9]+)</IBAN>", xml):
            if l.code != "AC01":
                assert iban_is_valid(iban), f"{l.case_id}: {iban}"


def test_manifest(cases):
    m = json.loads((cases / "manifest.json").read_text())
    assert m["seed"] == 42 and m["count"] == 61


def test_implicit_fraud_demo_case(cases):
    """DEMO-11 is the demo's model story: fraud with none of the rules' keywords."""
    from copilot.agent import FRAUD_SIGNALS
    from gen.holdout import RECALLS
    d = cases / "DEMO-11-FRAD"
    text = (d / "request.txt").read_text()
    label = Label.model_validate_json((d / "label.json").read_text())
    assert label.set == "demo" and label.code == "FRAD" and label.action == "escalate"
    assert not FRAUD_SIGNALS.search(text)
    # not a copy of any held-out request
    assert all(t.split("{e2e}")[0][:25] not in text for _, _, _, t in RECALLS)
