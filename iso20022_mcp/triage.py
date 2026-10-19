"""explain_reject and draft_repair.

Guardrails enforced in code, not in a prompt:
1. Only codes the policy marks `repair` can be drafted.
2. A repair needs evidence: validate_message must find the defect the reject
   code points at. No evidence -> escalate; don't fix what you can't see.
3. A draft may only change the fields the policy allows for that code; the
   diff is checked after the edit and the draft is discarded if anything
   else moved.
4. The repaired message must pass validation, or there is no draft.
5. Output is always a DRAFT pending human approval. Nothing is sent.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from lxml import etree

from gen.base import next_business_day

from .parser import ParseError, load, parse
from .policy import policy_for
from .refdata import KNOWN_CURRENCIES, bic_directory, debtor_master
from .validator import validate_message


def _ns(root) -> dict[str, str]:
    return {"n": etree.QName(root).namespace}


def _evidence(pacs008_xml: str | bytes | None, code: str) -> list[dict] | None:
    if pacs008_xml is None:
        return None
    return [f for f in validate_message(pacs008_xml)["findings"] if f["reason_code_hint"] == code]


def explain_reject(pacs002_xml: str | bytes, pacs008_xml: str | bytes | None = None) -> dict[str, Any]:
    try:
        status = parse(pacs002_xml)
    except ParseError as e:
        return {"error": str(e)}
    if status["message_type"] != "pacs.002.001.10":
        return {"error": "first argument must be a pacs.002 status report"}

    if pacs008_xml is not None:
        try:
            original = parse(pacs008_xml)
        except ParseError as e:
            return {"error": f"original message: {e}"}
        if original["transaction"]["uetr"] != status["original"]["uetr"]:
            return {"error": "pacs.002 does not refer to this pacs.008 (UETR mismatch)"}

    out: dict[str, Any] = {
        "status": status["status"],
        "uetr": status["original"]["uetr"],
        "end_to_end_id": status["original"]["end_to_end_id"],
        "rejected_by": status["group_header"]["from_bic"],
    }
    if status["status"] != "RJCT":
        out.update(
            recommended_action="none" if status["status"] == "ACSC" else "escalate",
            explanation=("Payment settled. It can only be pulled back with a recall request "
                         "(camt.056) if the client asks." if status["status"] == "ACSC"
                         else f"Status {status['status']} is not handled in v1."),
        )
        return out

    code = status["reason_code"]
    pol = policy_for(code)
    evidence = _evidence(pacs008_xml, code)
    action = pol.action
    note = None
    if action == "repair" and evidence is not None and not evidence:
        action, note = "escalate", "Policy allows a repair, but the defect is not visible in the original message. Escalated."
    elif action == "repair" and evidence is None:
        note = "Provide the original pacs.008 to confirm the defect before drafting a repair."

    out.update(
        reason_code=code,
        title=pol.title,
        explanation=pol.explanation,
        bank_additional_info=status["additional_info"],
        recommended_action=action,
        repairable_fields=pol.allowed_fields if action == "repair" else [],
        evidence=evidence,
        note=note,
        requires_human_approval=True,
    )
    return out


# ---------- repairs: one per repairable code ----------

class Refuse(Exception):
    pass


def _fix_rc01(root, ns):
    fin = root.find(".//n:CdtTrfTxInf/n:CdtrAgt/n:FinInstnId", ns)
    name = fin.findtext("n:Nm", namespaces=ns)
    if not name:
        raise Refuse("creditor agent has no name on the message; cannot look up the BIC")
    match = bic_directory().find_by_name(name)
    if not match:
        raise Refuse(f"no directory entry for agent name '{name}'")
    fin.find("n:BICFI", ns).text = match["bic"]


def _fix_ch16(root, ns):
    amt = root.find(".//n:CdtTrfTxInf/n:IntrBkSttlmAmt", ns)
    ccy = amt.get("Ccy") or ""
    if ccy.upper() not in KNOWN_CURRENCIES or ccy.upper() == ccy:
        raise Refuse("CH16 repair in v1 only covers a mis-cased currency code")
    amt.set("Ccy", ccy.upper())


def _fix_dt01(root, ns):
    el = root.find(".//n:CdtTrfTxInf/n:IntrBkSttlmDt", ns)
    el.text = next_business_day(date.fromisoformat(el.text)).isoformat()


def _fix_rr02(root, ns):
    dbtr = root.find(".//n:CdtTrfTxInf/n:Dbtr", ns)
    name = dbtr.findtext("n:Nm", namespaces=ns)
    addr = debtor_master().get(name)
    if addr is None:
        raise Refuse(f"debtor '{name}' not found in customer master")
    q = lambda t: f"{{{ns['n']}}}{t}"  # noqa: E731
    adr = etree.Element(q("PstlAdr"))
    for tag, val in [("StrtNm", addr.strt_nm), ("BldgNb", addr.bldg_nb), ("PstCd", addr.pst_cd),
                     ("TwnNm", addr.twn_nm), ("Ctry", addr.ctry)]:
        if val:
            etree.SubElement(adr, q(tag)).text = val
    dbtr.find("n:Nm", ns).addnext(adr)


_FIXERS = {"RC01": _fix_rc01, "CH16": _fix_ch16, "DT01": _fix_dt01, "RR02": _fix_rr02}

# parsed-field accessors used to compute and police the diff
_FIELD_VIEW = {
    "CdtrAgt.FinInstnId.BICFI": lambda t: t["creditor_agent"]["bic"],
    "IntrBkSttlmAmt/@Ccy": lambda t: t["currency"],
    "IntrBkSttlmDt": lambda t: t["settlement_date"],
    "Dbtr.PstlAdr": lambda t: t["debtor"]["address"],
}


def _strip(tx: dict, allowed: list[str]) -> dict:
    """The parsed transaction with the allowed fields blanked, for diffing."""
    t = {k: (dict(v) if isinstance(v, dict) else v) for k, v in tx.items()}
    for f in allowed:
        if f == "CdtrAgt.FinInstnId.BICFI":
            t["creditor_agent"]["bic"] = None
        elif f == "IntrBkSttlmAmt/@Ccy":
            t["currency"] = None
        elif f == "IntrBkSttlmDt":
            t["settlement_date"] = None
        elif f == "Dbtr.PstlAdr":
            t["debtor"]["address"] = None
    return t


def draft_repair(pacs008_xml: str | bytes, pacs002_xml: str | bytes) -> dict[str, Any]:
    explained = explain_reject(pacs002_xml, pacs008_xml)
    if "error" in explained:
        return {"status": "REFUSED", "reason": explained["error"]}
    code = explained.get("reason_code")
    if explained["recommended_action"] != "repair":
        return {"status": "REFUSED", "reason_code": code,
                "reason": f"policy action is '{explained['recommended_action']}', not repair",
                "route_to": explained["recommended_action"]}

    raw = pacs008_xml.encode() if isinstance(pacs008_xml, str) else pacs008_xml
    root, _, _ = load(raw)
    ns = _ns(root)
    try:
        _FIXERS[code](root, ns)
    except Refuse as e:
        return {"status": "REFUSED", "reason_code": code, "reason": str(e), "route_to": "escalate"}

    repaired = etree.tostring(root, pretty_print=True, xml_declaration=True, encoding="UTF-8")
    allowed = policy_for(code).allowed_fields
    before, after = parse(raw)["transaction"], parse(repaired)["transaction"]

    # Guardrail 3: nothing outside the allowlist may change.
    if _strip(before, allowed) != _strip(after, allowed):
        return {"status": "REFUSED", "reason_code": code, "route_to": "escalate",
                "reason": "repair touched fields outside the policy allowlist; draft discarded"}

    # Guardrail 4: the draft must pass validation.
    check = validate_message(repaired)
    if not check["valid"]:
        return {"status": "REFUSED", "reason_code": code, "route_to": "escalate",
                "reason": "repaired message still fails validation", "findings": check["findings"]}

    changes = [{"field": f, "before": _FIELD_VIEW[f](before), "after": _FIELD_VIEW[f](after)}
               for f in allowed if _FIELD_VIEW[f](before) != _FIELD_VIEW[f](after)]
    return {
        "status": "DRAFT_PENDING_APPROVAL",
        "reason_code": code,
        "changes": changes,
        "repaired_xml": repaired.decode(),
        "post_repair_validation": check,
        "requires_human_approval": True,
        "note": "Draft only. Nothing has been sent. Resubmission IDs are assigned at release.",
    }
