"""Build a pacs.002.001.10 status report for an original pacs.008."""

from __future__ import annotations

import random
from datetime import timedelta

from lxml import etree

from .model import Payment

NS_PACS002 = "urn:iso:std:iso:20022:tech:xsd:pacs.002.001.10"


def _el(parent, tag, text=None):
    e = etree.SubElement(parent, f"{{{NS_PACS002}}}{tag}")
    if text is not None:
        e.text = text
    return e


def _agent(parent, tag, bic):
    _el(_el(_el(parent, tag), "FinInstnId"), "BICFI", bic)


def to_pacs002(p: Payment, rng: random.Random, status: str,
               reason: str | None = None, addtl_inf: str | None = None) -> bytes:
    """status: RJCT (Lane A) or ACSC (Lane B). The report is sent back by the
    instructed agent, so instructing/instructed agents are swapped."""
    created = p.cre_dt_tm + timedelta(minutes=rng.randint(2, 90))
    doc = etree.Element(f"{{{NS_PACS002}}}Document", nsmap={None: NS_PACS002})
    root = _el(doc, "FIToFIPmtStsRpt")

    grp = _el(root, "GrpHdr")
    _el(grp, "MsgId", f"{p.instd_agt.bic[:4]}{created:%Y%m%d}{rng.randint(100000, 999999)}")
    _el(grp, "CreDtTm", created.strftime("%Y-%m-%dT%H:%M:%S+00:00"))
    _agent(grp, "InstgAgt", p.instd_agt.bic)
    _agent(grp, "InstdAgt", p.instg_agt.bic)

    og = _el(root, "OrgnlGrpInfAndSts")
    _el(og, "OrgnlMsgId", p.msg_id)
    _el(og, "OrgnlMsgNmId", "pacs.008.001.08")

    tx = _el(root, "TxInfAndSts")
    _el(tx, "OrgnlInstrId", p.instr_id)
    _el(tx, "OrgnlEndToEndId", p.end_to_end_id)
    _el(tx, "OrgnlUETR", p.uetr)
    _el(tx, "TxSts", status)
    if reason:
        sri = _el(tx, "StsRsnInf")
        _el(_el(sri, "Rsn"), "Cd", reason)
        if addtl_inf:
            _el(sri, "AddtlInf", addtl_inf[:105])

    return etree.tostring(doc, pretty_print=True, xml_declaration=True, encoding="UTF-8")
