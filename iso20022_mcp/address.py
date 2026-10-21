"""Unstructured postal address -> CBPR+ structured or hybrid address.

Why: Swift is retiring fully unstructured addresses (free-text AdrLine only)
in cross-border payments. A hybrid address needs at least Town Name and Country
as separate elements, with at most 2 address lines of 70 characters; a fully
structured address has no address lines at all. Banks have millions of
beneficiary records typed as free text, in every country's format.

How this module decides, in order:
1. Country: from an explicit country line, else the creditor's IBAN, else the
   creditor agent's BIC. If two sources disagree, a person decides: a wrong
   country breaks sanctions screening.
2. A country-specific parser (MX, BR, CO, AR, GB, DE) pulls out street, building
   number, postcode, district, town and state/province.
3. Whatever the parser can't place goes to address lines (hybrid), if it fits
   in 2 x 70. Otherwise, or if no town is found, a person decides.

Guardrails, enforced in check_proposal() for the rules AND for the model:
- no invented text: every value must appear in the original address
- the result must be a valid CBPR+ hybrid or structured address
- the postcode must match the country's format, where we know it
- the country must match the evidence (country line, IBAN, BIC)
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

FIELDS = ("strt_nm", "bldg_nb", "pst_cd", "twn_nm", "dstrct_nm", "ctry_sub_dvsn", "ctry")
XML_TAG = {"strt_nm": "StrtNm", "bldg_nb": "BldgNb", "pst_cd": "PstCd", "twn_nm": "TwnNm",
           "dstrct_nm": "DstrctNm", "ctry_sub_dvsn": "CtrySubDvsn", "ctry": "Ctry"}
# Schema order inside PostalAddress24 (pacs.008.001.08)
XML_ORDER = ["StrtNm", "BldgNb", "PstCd", "TwnNm", "DstrctNm", "CtrySubDvsn", "Ctry", "AdrLine"]
MAX_LINES, MAX_LINE_LEN = 2, 70

COUNTRY_NAMES = {
    "MX": ["MEXICO"], "BR": ["BRASIL", "BRAZIL"], "CO": ["COLOMBIA"], "AR": ["ARGENTINA"],
    "GB": ["UNITED KINGDOM", "UK", "GREAT BRITAIN", "ENGLAND"], "DE": ["GERMANY", "DEUTSCHLAND"],
    "CL": ["CHILE"], "PE": ["PERU"], "CR": ["COSTA RICA"], "PA": ["PANAMA"],
    "DO": ["DOMINICAN REPUBLIC", "REPUBLICA DOMINICANA"], "ES": ["SPAIN", "ESPANA"],
    "PT": ["PORTUGAL"], "FR": ["FRANCE"], "US": ["UNITED STATES", "USA"],
}
POSTCODE = {
    "MX": r"\d{5}", "BR": r"\d{5}-?\d{3}", "CO": r"\d{6}", "AR": r"[A-Z]\d{4}[A-Z]{3}|\d{4}",
    "GB": r"[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}", "DE": r"\d{5}", "CL": r"\d{7}", "PE": r"\d{5}",
    "CR": r"\d{5}", "ES": r"\d{5}", "PT": r"\d{4}-\d{3}", "FR": r"\d{5}", "US": r"\d{5}(-\d{4})?",
}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().upper()


# ---------- what kind of address is this? ----------

def classify(addr: dict | None) -> str:
    """'missing' | 'unstructured' | 'hybrid' | 'structured' (CBPR+ terms)."""
    if not addr:
        return "missing"
    lines = addr.get("adr_lines") or []
    if not (addr.get("twn_nm") and addr.get("ctry")):
        return "unstructured"
    return "hybrid" if lines else "structured"


def cbpr_issues(addr: dict) -> list[str]:
    """Why an address is not a valid CBPR+ hybrid/structured address (empty = valid)."""
    out = []
    if not addr.get("twn_nm"):
        out.append("Town Name missing")
    if not addr.get("ctry"):
        out.append("Country missing")
    lines = addr.get("adr_lines") or []
    if len(lines) > MAX_LINES:
        out.append(f"{len(lines)} address lines; hybrid allows {MAX_LINES}")
    out += [f"address line {i + 1} is {len(l)} characters; max {MAX_LINE_LEN}"
            for i, l in enumerate(lines) if len(l) > MAX_LINE_LEN]
    for f in ("twn_nm", "pst_cd"):
        v = addr.get(f)
        if v and any(norm(v) in norm(l) for l in lines):
            out.append(f"{XML_TAG[f]} '{v}' repeated in an address line")
    return out


# ---------- country ----------

def country_evidence(lines: list[str], iban: str | None, agent_bic: str | None) -> dict[str, str]:
    ev = {}
    last = norm(lines[-1]) if lines else ""
    for code, names in COUNTRY_NAMES.items():
        if last in names or last == code:
            ev["country line"] = code
    if iban and len(iban) > 2 and iban[:2].isalpha():
        ev["creditor IBAN"] = iban[:2].upper()
    if agent_bic and len(agent_bic) >= 6:
        ev["creditor agent BIC"] = agent_bic[4:6].upper()
    return ev


# ---------- country parsers ----------

@dataclass
class Parsed:
    fields: dict[str, str] = field(default_factory=dict)
    used: list[str] = field(default_factory=list)   # input fragments placed into fields
    rule: str = ""


def _street_number(text: str, number_first: bool = False) -> tuple[str | None, str | None]:
    t = text.strip().rstrip(",")
    m = re.match(r"^(\d+[A-Z]?)\s+(.+)$", t, re.I) if number_first else None
    if m:
        return m.group(2).strip(), m.group(1)
    m = re.match(r"^(.*?\D)[,\s]+(\d+[A-Z]?(?:-\d+)?)$", t, re.I)
    if m:
        return m.group(1).strip().rstrip(","), m.group(2)
    return (t or None), None


def _parse_mx(lines: list[str]) -> Parsed:
    p = Parsed(rule="MX: street + number, Col. district, C.P. 5 digits + town, state")
    for ln in lines:
        m = re.search(r"(?:C\.?\s?P\.?\s*)?(\d{5})\s+([^,]+?)(?:,\s*(.+))?$", ln)
        if m and "pst_cd" not in p.fields:
            p.fields.update(pst_cd=m.group(1), twn_nm=m.group(2).strip())
            if m.group(3):
                p.fields["ctry_sub_dvsn"] = m.group(3).strip()
            p.used.append(ln)
        elif re.match(r"^\s*Col(?:onia|\.)?\s+", ln, re.I):
            p.fields["dstrct_nm"] = re.sub(r"^\s*Col(?:onia|\.)?\s+", "", ln, flags=re.I).strip()
            p.used.append(ln)
        elif "strt_nm" not in p.fields and re.search(r"\d", ln):
            s, n = _street_number(ln)
            p.fields.update({k: v for k, v in (("strt_nm", s), ("bldg_nb", n)) if v})
            p.used.append(ln)
    return p


def _parse_br(lines: list[str]) -> Parsed:
    p = Parsed(rule="BR: street, number - bairro; town - UF; CEP 00000-000")
    for ln in lines:
        if m := re.search(r"(?:CEP:?\s*)?(\d{5}-?\d{3})", ln, re.I):
            p.fields["pst_cd"] = m.group(1)
            p.used.append(ln)
        elif m := re.match(r"^\s*([^,\d]+?)\s*[-/]\s*([A-Z]{2})\s*$", ln):
            p.fields.update(twn_nm=m.group(1).strip(), ctry_sub_dvsn=m.group(2))
            p.used.append(ln)
        elif "strt_nm" not in p.fields and re.search(r"\d", ln):
            street, _, bairro = ln.partition(" - ")
            s, n = _street_number(street)
            p.fields.update({k: v for k, v in (("strt_nm", s), ("bldg_nb", n)) if v})
            if bairro.strip():
                p.fields["dstrct_nm"] = bairro.strip()
            p.used.append(ln)
    return p


def _parse_co(lines: list[str]) -> Parsed:
    p = Parsed(rule="CO: Calle/Carrera N # nn-nn; town, department; 6-digit postcode")
    for ln in lines:
        if m := re.match(r"^\s*(.+?)\s*#\s*([\d\w-]+)\s*$", ln):
            p.fields.update(strt_nm=m.group(1).strip(), bldg_nb=m.group(2))
            p.used.append(ln)
        elif m := re.fullmatch(r"\s*(\d{6})\s*", ln):
            p.fields["pst_cd"] = m.group(1)
            p.used.append(ln)
        elif m := re.match(r"^\s*([^,\d]+?),\s*([^,\d]+?)\s*$", ln):
            p.fields.update(twn_nm=m.group(1).strip(), ctry_sub_dvsn=m.group(2).strip())
            p.used.append(ln)
    return p


def _parse_ar(lines: list[str]) -> Parsed:
    p = Parsed(rule="AR: street number; CPA postcode (A0000AAA) + town; province")
    for ln in lines:
        if m := re.match(r"^\s*([A-Z]\d{4}[A-Z]{3}|\d{4})\s+(.+?)\s*$", ln):
            p.fields.update(pst_cd=m.group(1), twn_nm=m.group(2).strip().rstrip(","))
            p.used.append(ln)
        elif "strt_nm" not in p.fields and re.search(r"\d", ln):
            s, n = _street_number(ln)
            p.fields.update({k: v for k, v in (("strt_nm", s), ("bldg_nb", n)) if v})
            p.used.append(ln)
        elif "twn_nm" in p.fields and "ctry_sub_dvsn" not in p.fields:
            p.fields["ctry_sub_dvsn"] = ln.strip()
            p.used.append(ln)
    return p


def _parse_gb(lines: list[str]) -> Parsed:
    p = Parsed(rule="GB: number + street; town + postcode")
    for ln in lines:
        if m := re.match(rf"^\s*([A-Za-z][A-Za-z .'-]*?)\s+({POSTCODE['GB']})\s*$", ln.upper()):
            p.fields.update(twn_nm=ln.strip()[: len(m.group(1))].strip(), pst_cd=m.group(2))
            p.used.append(ln)
        elif "strt_nm" not in p.fields and re.match(r"^\s*\d", ln):
            s, n = _street_number(ln, number_first=True)
            p.fields.update({k: v for k, v in (("strt_nm", s), ("bldg_nb", n)) if v})
            p.used.append(ln)
    return p


def _parse_de(lines: list[str]) -> Parsed:
    p = Parsed(rule="DE: street + number; 5-digit PLZ + town")
    for ln in lines:
        if m := re.match(r"^\s*(\d{5})\s+(.+?)\s*$", ln):
            p.fields.update(pst_cd=m.group(1), twn_nm=m.group(2))
            p.used.append(ln)
        elif "strt_nm" not in p.fields and re.search(r"\d", ln):
            s, n = _street_number(ln)
            p.fields.update({k: v for k, v in (("strt_nm", s), ("bldg_nb", n)) if v})
            p.used.append(ln)
    return p


PARSERS = {"MX": _parse_mx, "BR": _parse_br, "CO": _parse_co, "AR": _parse_ar,
           "GB": _parse_gb, "DE": _parse_de}


# ---------- the result ----------

@dataclass
class Result:
    status: str                    # STRUCTURED | HYBRID | NEEDS_REVIEW
    address: dict[str, Any] | None  # proposed PstlAdr fields (+ adr_lines)
    reason: str
    rule: str = ""
    evidence: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "address": self.address, "reason": self.reason,
                "rule": self.rule, "country_evidence": self.evidence}


def resolve_country(lines, iban, agent_bic) -> tuple[str | None, dict[str, str], str | None]:
    ev = country_evidence(lines, iban, agent_bic)
    codes = set(ev.values())
    if len(codes) > 1:
        return None, ev, "country sources disagree (" + ", ".join(f"{k}: {v}" for k, v in ev.items()) + ")"
    if not codes:
        return None, ev, "no country in the address, the IBAN or the agent BIC"
    return codes.pop(), ev, None


def structure(lines: list[str], iban: str | None = None, agent_bic: str | None = None) -> Result:
    """Deterministic: country rules first. Never guesses a town or a country."""
    lines = [l for l in (x.strip() for x in lines) if l]
    ctry, ev, problem = resolve_country(lines, iban, agent_bic)
    if problem:
        return Result("NEEDS_REVIEW", None, problem, evidence=ev)
    body = lines[:-1] if ev.get("country line") else lines
    parser = PARSERS.get(ctry)
    if parser is None:
        return Result("NEEDS_REVIEW", None, f"no address rules for {ctry} yet", evidence=ev)
    parsed = parser(body)
    addr = {**parsed.fields, "ctry": ctry}
    rest = [l for l in body if l not in parsed.used]
    if rest:
        addr["adr_lines"] = rest
    issues = check_proposal(addr, lines, ctry)
    if issues:
        return Result("NEEDS_REVIEW", None, "; ".join(issues), parsed.rule, ev)
    return Result("HYBRID" if rest else "STRUCTURED", addr,
                  "all text placed in structured fields" if not rest
                  else f"{len(rest)} line(s) kept as address lines", parsed.rule, ev)


def check_proposal(addr: dict, original_lines: list[str], country: str) -> list[str]:
    """The same checks for a rules result and a model result. Empty list = accept."""
    issues = cbpr_issues(addr)
    source = norm(" ".join(original_lines))
    for f in FIELDS:
        v = addr.get(f)
        if f == "ctry" or not v:
            continue
        if norm(v) not in source:
            issues.append(f"{XML_TAG[f]} '{v}' does not appear in the original address (invented)")
    for l in addr.get("adr_lines") or []:
        if norm(l) not in source:
            issues.append(f"address line '{l}' does not appear in the original address (invented)")
    if addr.get("ctry") != country:
        issues.append(f"Country {addr.get('ctry')} does not match the evidence ({country})")
    pc = addr.get("pst_cd")
    if pc and country in POSTCODE and not re.fullmatch(POSTCODE[country], pc.upper()):
        issues.append(f"postcode '{pc}' is not a valid {country} format")
    return issues


# ---------- applying a proposal to the message (draft only) ----------

def apply(pacs008_xml: bytes, addr: dict) -> bytes:
    """Return a copy of the pacs.008 with the creditor PstlAdr replaced by `addr`."""
    from lxml import etree
    root = etree.fromstring(pacs008_xml)
    ns = etree.QName(root).namespace
    cdtr = root.find(f".//{{{ns}}}CdtTrfTxInf/{{{ns}}}Cdtr")
    old = cdtr.find(f"{{{ns}}}PstlAdr")
    new = etree.Element(f"{{{ns}}}PstlAdr")
    by_tag = {XML_TAG[k]: v for k, v in addr.items() if k in XML_TAG and v}
    for tag in XML_ORDER:
        if tag == "AdrLine":
            for l in addr.get("adr_lines") or []:
                etree.SubElement(new, f"{{{ns}}}AdrLine").text = l
        elif tag in by_tag:
            etree.SubElement(new, f"{{{ns}}}{tag}").text = by_tag[tag]
    cdtr.replace(old, new) if old is not None else cdtr.insert(1, new)
    return etree.tostring(root, pretty_print=True, xml_declaration=True, encoding="UTF-8")


def changes(before: dict, after: dict) -> list[dict]:
    rows = [{"field": "Cdtr.PstlAdr.AdrLine", "before": " | ".join(before.get("adr_lines") or []) or None,
             "after": " | ".join(after.get("adr_lines") or []) or None}]
    rows += [{"field": f"Cdtr.PstlAdr.{XML_TAG[f]}", "before": before.get(f), "after": after.get(f)}
             for f in FIELDS if after.get(f) != before.get(f)]
    return rows


def finalize(pacs008_xml: bytes, addr: dict, why: str) -> dict:
    """Apply, then prove the draft: only the creditor address moved, the message is
    still schema-valid, and the new address is valid CBPR+. Otherwise refuse."""
    from .parser import parse
    from .validator import validate_message
    repaired = apply(pacs008_xml, addr)
    before, after = parse(pacs008_xml)["transaction"], parse(repaired)["transaction"]
    strip = lambda t: {**t, "creditor": {**t["creditor"], "address": None}}  # noqa: E731
    if strip(before) != strip(after):
        return {"status": "REFUSED", "reason": "draft touched fields outside the creditor address"}
    check = validate_message(repaired)
    if not check["valid"]:
        return {"status": "REFUSED", "reason": "drafted message fails validation", "findings": check["findings"]}
    if issues := cbpr_issues(after["creditor"]["address"]):
        return {"status": "REFUSED", "reason": "; ".join(issues)}
    return {
        "status": "DRAFT_PENDING_APPROVAL",
        "reason_code": "ADDR",
        "format": classify(after["creditor"]["address"]),
        "changes": changes(before["creditor"]["address"] or {}, after["creditor"]["address"]),
        "repaired_xml": repaired.decode(),
        "post_repair_validation": check,
        "why": why,
        "requires_human_approval": True,
        "note": "Draft only. Nothing has been sent.",
    }


def draft_address(pacs008_xml: str | bytes) -> dict[str, Any]:
    """MCP tool body: rules only, deterministic. Returns a draft or NEEDS_REVIEW."""
    from .parser import ParseError, parse
    raw = pacs008_xml.encode() if isinstance(pacs008_xml, str) else pacs008_xml
    try:
        tx = parse(raw)["transaction"]
    except ParseError as e:
        return {"status": "REFUSED", "reason": str(e)}
    addr = (tx.get("creditor") or {}).get("address")
    kind = classify(addr)
    if kind != "unstructured":
        return {"status": "NOT_NEEDED" if kind != "missing" else "REFUSED",
                "current_format": kind,
                "reason": "creditor address already CBPR+ " + kind if kind != "missing"
                else "no creditor address at all (RR03 territory): ask the client"}
    r = structure(addr.get("adr_lines") or [], (tx.get("creditor_account") or {}).get("iban"),
                  (tx.get("creditor_agent") or {}).get("bic"))
    out = {"current_format": kind, **r.as_dict()}
    if r.address is None:
        return out
    why = f"{r.rule}. {r.reason}. Country from: " + ", ".join(f"{k} ({v})" for k, v in r.evidence.items()) + "."
    draft = finalize(raw, r.address, why)
    return {**out, **draft, "status": r.status if draft["status"] != "REFUSED" else "NEEDS_REVIEW"}
