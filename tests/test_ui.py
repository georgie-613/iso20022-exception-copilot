from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gen.build import build
from ui.app import create_app


@pytest.fixture(scope="module")
def cases(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("cases")
    build(42, out)
    return out


@pytest.fixture
def client(cases, tmp_path):
    app = create_app(cases, "demo", tmp_path / "audit.jsonl")
    return TestClient(app), app


def test_queue_lists_demo_cases(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert "DEMO-01-RC01" in r.text and "11 awaiting review" in r.text
    assert "verified" in r.text


def test_case_page_shows_diff_and_form(client):
    c, _ = client
    r = c.get("/case/DEMO-01-RC01")
    assert "Proposed change" in r.text and "CdtrAgt.FinInstnId.BICFI" in r.text
    assert 'value="approve"' in r.text


def test_outreach_and_recall_render(client):
    c, _ = client
    assert "Draft message to client" in c.get("/case/DEMO-05-AC01").text
    assert "camt.056" in c.get("/case/DEMO-09-DUPL").text
    assert "Escalated to a person" in c.get("/case/DEMO-10-FRAD").text


def test_approve_flow_writes_audit(client):
    c, app = client
    r = c.post("/case/DEMO-01-RC01/decide", data={"reviewer": "jorge", "decision": "approve"},
               follow_redirects=False)
    assert r.status_code == 303
    page = c.get("/case/DEMO-01-RC01").text
    assert "Decision recorded: approved" in page and "jorge" in page
    events = [e["event"] for e in app.state.audit.for_case("DEMO-01-RC01")]
    assert events == ["proposed", "approved"]
    assert app.state.audit.verify()[0]


def test_copilot_cannot_approve_itself(client):
    c, _ = client
    r = c.post("/case/DEMO-01-RC01/decide", data={"reviewer": "copilot", "decision": "approve"})
    assert r.status_code == 400 and "maker-checker" in r.text


def test_reject_requires_note(client):
    c, _ = client
    r = c.post("/case/DEMO-02-CH16/decide", data={"reviewer": "jorge", "decision": "reject"})
    assert r.status_code == 400 and "reason" in r.text


def test_decisions_survive_restart(cases, tmp_path):
    log = tmp_path / "audit.jsonl"
    c1 = TestClient(create_app(cases, "demo", log))
    c1.post("/case/DEMO-04-DT01/decide", data={"reviewer": "ana", "decision": "approve"})
    app2 = create_app(cases, "demo", log)
    c2 = TestClient(app2)
    assert "Decision recorded: approved" in c2.get("/case/DEMO-04-DT01").text
    assert "10 awaiting review" in c2.get("/").text
    proposed = [e for e in app2.state.audit.events() if e["event"] == "proposed"]
    assert len(proposed) == 11  # restart did not re-log proposals


def test_output_is_escaped(cases, tmp_path):
    """Client free text and names must never render as HTML."""
    import shutil
    root = tmp_path / "c"
    shutil.copytree(cases / "DEMO-05-AC01", root / "DEMO-05-AC01")
    xml = (root / "DEMO-05-AC01" / "pacs008.xml").read_text()
    name = xml.split("<Dbtr>")[1].split("<Nm>")[1].split("</Nm>")[0]
    (root / "DEMO-05-AC01" / "pacs008.xml").write_text(xml.replace(name, "&lt;script&gt;x&lt;/script&gt;"))
    c = TestClient(create_app(root, "demo", tmp_path / "a.jsonl"))
    page = c.get("/case/DEMO-05-AC01").text
    assert "&lt;script&gt;x" in page          # the name is shown, escaped
    assert "<script>x" not in page


def test_unknown_case_404(client):
    c, _ = client
    assert c.get("/case/NOPE").status_code == 404


def test_audit_page(client):
    c, _ = client
    r = c.get("/audit")
    assert r.status_code == 200 and "Hash chain: verified" in r.text


def test_case_page_shows_client_request_and_repair_reason(client):
    c, _ = client
    fraud = c.get("/case/DEMO-11-FRAD").text
    assert "Client request" in fraud and "new mobile number" in fraud
    assert "Why this repair" in c.get("/case/DEMO-01-RC01").text
