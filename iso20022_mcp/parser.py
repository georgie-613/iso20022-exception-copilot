"""Parse pacs.008 / pacs.002 XML into a flat, agent-friendly structure.

Parsing is deliberately schema-tolerant: a message that fails the XSD (a CH16
reject) must still be readable, or the copilot can't explain what is wrong.
"""

from __future__ import annotations

from typing import Any

from lxml import etree

SUPPORTED = {
    "urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08": "pacs.008.001.08",
    "urn:iso:std:iso:20022:tech:xsd:pacs.002.001.10": "pacs.002.001.10",
}


class ParseError(ValueError):
    pass


def load(xml: str | bytes) -> tuple[etree._Element, str, str]:
    """Return (root Document element, namespace, message type)."""
    if isinstance(xml, str):
        xml = xml.encode()
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
        root = etree.fromstring(xml, parser)
    except etree.XMLSyntaxError as e:
        raise ParseError(f"not well-formed XML: {e}") from e
    ns = etree.QName(root).namespace or ""
    if ns not in SUPPORTED:
        raise ParseError(f"unsupported message namespace: {ns or '(none)'}")
    return root, ns, SUPPORTED[ns]


def _t(node: etree._Element | None, path: str, ns: str) -> str | None:
    if node is None:
        return None
    parts = "/".join(f"n:{p}" for p in path.split("/"))
    found = node.find(parts, {"n": ns})
    return found.text if found is not None else None


def _agent(node, tag, ns) -> dict[str, Any] | None:
    el = node.find(f"n:{tag}", {"n": ns})
    if el is None:
        return None
    return {"bic": _t(el, "FinInstnId/BICFI", ns), "name": _t(el, "FinInstnId/Nm", ns)}


def _party(node, tag, ns) -> dict[str, Any] | None:
    el = node.find(f"n:{tag}", {"n": ns})
    if el is None:
        return None
    adr = el.find("n:PstlAdr", {"n": ns})
    address = None
    if adr is not None:
        address = {k: _t(adr, tag_, ns) for k, tag_ in
                   [("strt_nm", "StrtNm"), ("bldg_nb", "BldgNb"), ("pst_cd", "PstCd"),
                    ("twn_nm", "TwnNm"), ("ctry", "Ctry")]}
    return {"name": _t(el, "Nm", ns), "address": address}


def _account(node, tag, ns) -> dict[str, Any] | None:
    el = node.find(f"n:{tag}", {"n": ns})
    if el is None:
        return None
    return {"iban": _t(el, "Id/IBAN", ns), "other_id": _t(el, "Id/Othr/Id", ns)}


def _pacs008(root, ns) -> dict[str, Any]:
    body = root.find("n:FIToFICstmrCdtTrf", {"n": ns})
    if body is None:
        raise ParseError("pacs.008 has no FIToFICstmrCdtTrf")
    grp = body.find("n:GrpHdr", {"n": ns})
    tx = body.find("n:CdtTrfTxInf", {"n": ns})
    if tx is None:
        raise ParseError("pacs.008 has no CdtTrfTxInf")
    amt = tx.find("n:IntrBkSttlmAmt", {"n": ns})
    return {
        "group_header": {
            "msg_id": _t(grp, "MsgId", ns),
            "cre_dt_tm": _t(grp, "CreDtTm", ns),
            "nb_of_txs": _t(grp, "NbOfTxs", ns),
            "sttlm_mtd": _t(grp, "SttlmInf/SttlmMtd", ns),
        },
        "transaction": {
            "instr_id": _t(tx, "PmtId/InstrId", ns),
            "end_to_end_id": _t(tx, "PmtId/EndToEndId", ns),
            "uetr": _t(tx, "PmtId/UETR", ns),
            "amount": amt.text if amt is not None else None,
            "currency": amt.get("Ccy") if amt is not None else None,
            "settlement_date": _t(tx, "IntrBkSttlmDt", ns),
            "charge_bearer": _t(tx, "ChrgBr", ns),
            "instructing_agent": _agent(tx, "InstgAgt", ns),
            "instructed_agent": _agent(tx, "InstdAgt", ns),
            "debtor": _party(tx, "Dbtr", ns),
            "debtor_account": _account(tx, "DbtrAcct", ns),
            "debtor_agent": _agent(tx, "DbtrAgt", ns),
            "creditor_agent": _agent(tx, "CdtrAgt", ns),
            "creditor": _party(tx, "Cdtr", ns),
            "creditor_account": _account(tx, "CdtrAcct", ns),
            "remittance": _t(tx, "RmtInf/Ustrd", ns),
        },
    }


def _pacs002(root, ns) -> dict[str, Any]:
    body = root.find("n:FIToFIPmtStsRpt", {"n": ns})
    if body is None:
        raise ParseError("pacs.002 has no FIToFIPmtStsRpt")
    grp = body.find("n:GrpHdr", {"n": ns})
    og = body.find("n:OrgnlGrpInfAndSts", {"n": ns})
    tx = body.find("n:TxInfAndSts", {"n": ns})
    return {
        "group_header": {
            "msg_id": _t(grp, "MsgId", ns),
            "cre_dt_tm": _t(grp, "CreDtTm", ns),
            "from_bic": _t(grp, "InstgAgt/FinInstnId/BICFI", ns),
            "to_bic": _t(grp, "InstdAgt/FinInstnId/BICFI", ns),
        },
        "original": {
            "msg_id": _t(og, "OrgnlMsgId", ns),
            "msg_name_id": _t(og, "OrgnlMsgNmId", ns),
            "instr_id": _t(tx, "OrgnlInstrId", ns),
            "end_to_end_id": _t(tx, "OrgnlEndToEndId", ns),
            "uetr": _t(tx, "OrgnlUETR", ns),
        },
        "status": _t(tx, "TxSts", ns),
        "reason_code": _t(tx, "StsRsnInf/Rsn/Cd", ns),
        "additional_info": _t(tx, "StsRsnInf/AddtlInf", ns),
    }


def parse(xml: str | bytes) -> dict[str, Any]:
    root, ns, msg_type = load(xml)
    fields = _pacs008(root, ns) if msg_type.startswith("pacs.008") else _pacs002(root, ns)
    return {"message_type": msg_type, **fields}
