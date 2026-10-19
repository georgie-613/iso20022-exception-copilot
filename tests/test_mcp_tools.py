import asyncio
import json
import sys
from pathlib import Path

import pytest

from gen.build import build
from iso20022_mcp.parser import parse
from iso20022_mcp.server import server
from iso20022_mcp.validator import validate_message

# Codes whose defect is visible in the message itself, and the hint expected.
DETECTABLE = {"RC01": "RC01", "CH16": "CH16", "DT01": "DT01", "RR02": "RR02", "RR03": "RR03"}
# Causes that live outside the message: validation must come back clean.
EXTERNAL = {"AC04", "AC06", "AG01", "AM04", "AM05", "BE01", "RR04", "MS03",
            "DUPL", "CUST", "TECH", "FRAD"}


@pytest.fixture(scope="module")
def cases(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("cases")
    build(42, out)
    return out


def _each(cases):
    for d in sorted(cases.iterdir()):
        if d.is_dir():
            yield d, json.loads((d / "label.json").read_text())


# ---------- parse_message ----------

def test_parse_every_message(cases):
    for d, label in _each(cases):
        p8 = parse((d / "pacs008.xml").read_bytes())
        p2 = parse((d / "pacs002.xml").read_bytes())
        assert p8["message_type"] == "pacs.008.001.08"
        assert p2["message_type"] == "pacs.002.001.10"
        # the status report must point back at the original
        assert p2["original"]["uetr"] == p8["transaction"]["uetr"]
        assert p2["original"]["end_to_end_id"] == p8["transaction"]["end_to_end_id"]
        if label["lane"] == "A":
            assert p2["status"] == "RJCT" and p2["reason_code"] == label["code"]
        else:
            assert p2["status"] == "ACSC" and p2["reason_code"] is None


def test_parse_schema_invalid_message(cases):
    p = parse((cases / "A-CH16-01" / "pacs008.xml").read_bytes())
    assert p["transaction"]["currency"] == "usd"


def test_parse_missing_address_is_none(cases):
    assert parse((cases / "A-RR02-01" / "pacs008.xml").read_bytes())["transaction"]["debtor"]["address"] is None


# ---------- validate_message ----------

def test_detectable_defects_hint_the_reject_code(cases):
    for d, label in _each(cases):
        if label["code"] in DETECTABLE:
            v = validate_message((d / "pacs008.xml").read_bytes())
            hints = {f["reason_code_hint"] for f in v["findings"]}
            assert DETECTABLE[label["code"]] in hints, (d.name, v)
            assert not v["valid"]


def test_external_causes_validate_clean(cases):
    for d, label in _each(cases):
        if label["code"] in EXTERNAL:
            v = validate_message((d / "pacs008.xml").read_bytes())
            assert v["valid"] and v["findings"] == [], (d.name, v)


def test_ac01_caught_only_when_iban(cases):
    for d, label in _each(cases):
        if label["code"] != "AC01":
            continue
        v = validate_message((d / "pacs008.xml").read_bytes())
        has_iban = parse((d / "pacs008.xml").read_bytes())["transaction"]["creditor_account"]["iban"]
        hints = {f["reason_code_hint"] for f in v["findings"]}
        assert ("AC01" in hints) == bool(has_iban), d.name


def test_schema_valid_flag(cases):
    assert validate_message((cases / "A-CH16-01" / "pacs008.xml").read_bytes())["schema_valid"] is False
    assert validate_message((cases / "A-RC01-01" / "pacs008.xml").read_bytes())["schema_valid"] is True


def test_status_reports_validate(cases):
    for d, _ in _each(cases):
        assert validate_message((d / "pacs002.xml").read_bytes())["valid"], d.name


@pytest.mark.parametrize("bad", [b"", b"not xml", b"<x/>",
                                 b'<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"/>'])
def test_bad_input_never_raises(bad):
    v = validate_message(bad)
    assert v["valid"] is False and v["findings"][0]["rule"] == "parse"


def test_no_external_entities():
    xxe = b"""<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.002.001.10"><FIToFIPmtStsRpt><GrpHdr><MsgId>&x;</MsgId></GrpHdr></FIToFIPmtStsRpt></Document>"""
    p = parse(xxe)
    assert "root:" not in (p["group_header"]["msg_id"] or "")


# ---------- MCP server ----------

def test_tools_registered_read_only():
    tools = asyncio.run(server.list_tools())
    assert {"parse_message", "validate_message"} <= {t.name for t in tools}
    for t in tools:  # every tool on this server is read-only, including draft_repair
        assert t.annotations.read_only_hint and not t.annotations.destructive_hint


def test_call_tool_in_process(cases):
    xml = (cases / "A-RC01-01" / "pacs008.xml").read_text()
    r = asyncio.run(server.call_tool("validate_message", {"xml": xml}))
    assert not r.is_error
    assert r.structured_content["findings"][0]["reason_code_hint"] == "RC01"


def test_stdio_round_trip(cases):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    xml = (cases / "A-DT01-01" / "pacs008.xml").read_text()

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "iso20022_mcp.server"],
                                       cwd=str(Path(__file__).resolve().parent.parent))
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool("validate_message", {"xml": xml})
                return res.structured_content

    out = asyncio.run(asyncio.wait_for(run(), timeout=30))
    assert out["findings"][0]["reason_code_hint"] == "DT01"
