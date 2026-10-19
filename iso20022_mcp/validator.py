"""Validate a message: XSD first, then business rules.

Every finding names the ISO reason code it would most likely trigger
(`reason_code_hint`). That is what lets the copilot connect a pacs.002 reject
to the defect in the original message. A message can be schema-valid and
still be rejected (AC04, AM04, BE01...): in that case validation returns no
findings, which is itself evidence that the cause is outside the message.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal

from lxml import etree
from pydantic import BaseModel

from gen.customers import iban_is_valid
from gen.validate import validate as xsd_validate

from .parser import ParseError, load, parse
from .refdata import KNOWN_CURRENCIES, bic_directory

BIC_RE = re.compile(r"^[A-Z0-9]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")


class Finding(BaseModel):
    rule: str
    severity: Literal["error", "warning"]
    field: str
    message: str
    reason_code_hint: str | None = None


def _xsd_findings(xml: bytes, msg_type: str) -> list[Finding]:
    out = []
    for err in xsd_validate(xml, msg_type):
        # err looks like "line N: Element '{ns}X', attribute 'Y': ..."
        m = re.search(r"Element '\{[^}]*\}(\w+)'(?:, attribute '(\w+)')?", err)
        field = (m.group(1) + (f"/@{m.group(2)}" if m.group(2) else "")) if m else "?"
        out.append(Finding(rule="xsd", severity="error", field=field,
                           message=err, reason_code_hint="CH16"))
    return out


def _business_findings_pacs008(p: dict[str, Any]) -> list[Finding]:
    tx = p["transaction"]
    out: list[Finding] = []
    directory = bic_directory()

    for role, key in [("CdtrAgt", "creditor_agent"), ("InstdAgt", "instructed_agent")]:
        agent = tx.get(key) or {}
        bic = agent.get("bic")
        if not bic:
            continue
        if not BIC_RE.match(bic):
            out.append(Finding(rule="bic_format", severity="error", field=f"{role}.FinInstnId.BICFI",
                               message=f"BIC '{bic}' is not well-formed", reason_code_hint="RC01"))
        elif bic not in directory:
            hint = f" (agent name on message: '{agent['name']}')" if agent.get("name") else ""
            out.append(Finding(rule="bic_directory", severity="error", field=f"{role}.FinInstnId.BICFI",
                               message=f"BIC '{bic}' not found in directory{hint}", reason_code_hint="RC01"))

    sd = tx.get("settlement_date")
    if sd:
        try:
            d = date.fromisoformat(sd)
            if d.weekday() >= 5:
                out.append(Finding(rule="business_day", severity="error", field="IntrBkSttlmDt",
                                   message=f"settlement date {sd} is a {d:%A}", reason_code_hint="DT01"))
        except ValueError:
            pass  # malformed dates are an XSD finding already

    if tx.get("debtor") and tx["debtor"].get("address") is None:
        out.append(Finding(rule="debtor_address", severity="error", field="Dbtr.PstlAdr",
                           message="debtor postal address missing", reason_code_hint="RR02"))
    if tx.get("creditor") and tx["creditor"].get("address") is None:
        out.append(Finding(rule="creditor_address", severity="error", field="Cdtr.PstlAdr",
                           message="creditor postal address missing", reason_code_hint="RR03"))

    for role, key in [("DbtrAcct", "debtor_account"), ("CdtrAcct", "creditor_account")]:
        iban = (tx.get(key) or {}).get("iban")
        if iban and not iban_is_valid(iban):
            out.append(Finding(rule="iban_checksum", severity="error", field=f"{role}.Id.IBAN",
                               message=f"IBAN '{iban}' fails mod-97 check", reason_code_hint="AC01"))

    ccy = tx.get("currency")
    if ccy and ccy.isupper() and ccy not in KNOWN_CURRENCIES:
        out.append(Finding(rule="currency_known", severity="warning", field="IntrBkSttlmAmt/@Ccy",
                           message=f"currency '{ccy}' not expected on this rail"))

    amt = tx.get("amount")
    try:
        if amt is not None and float(amt) <= 0:
            out.append(Finding(rule="amount_positive", severity="error", field="IntrBkSttlmAmt",
                               message="amount must be positive", reason_code_hint="AM01"))
    except ValueError:
        pass
    return out


def validate_message(xml: str | bytes) -> dict[str, Any]:
    """Return {message_type, valid, schema_valid, findings[]}. Never raises on bad
    content; unreadable input comes back as a single 'parse' finding."""
    raw = xml.encode() if isinstance(xml, str) else xml
    try:
        _, _, msg_type = load(raw)
        parsed = parse(raw)
    except ParseError as e:
        f = Finding(rule="parse", severity="error", field="", message=str(e))
        return {"message_type": None, "valid": False, "schema_valid": False,
                "findings": [f.model_dump()]}

    findings = _xsd_findings(raw, msg_type)
    schema_valid = not findings
    if msg_type.startswith("pacs.008"):
        findings += _business_findings_pacs008(parsed)

    return {
        "message_type": msg_type,
        "valid": not any(f.severity == "error" for f in findings),
        "schema_valid": schema_valid,
        "findings": [f.model_dump() for f in findings],
    }
