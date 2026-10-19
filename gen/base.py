"""Build a valid pacs.008.001.08 from a seed, and serialize it to XML."""

from __future__ import annotations

import random
import uuid
from datetime import date, datetime, timedelta, timezone

from lxml import etree

from .customers import debtor_customers, make_beneficiary
from .directory import Directory
from .model import Account, Address, Agent, Party, Payment

NS_PACS008 = "urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08"

# Fixed anchor so output never depends on the wall clock.
ANCHOR = datetime(2026, 9, 1, 13, 0, 0, tzinfo=timezone.utc)

_PURPOSES = [
    "INVOICE {n} GOODS SUPPLY", "PO {n} RAW MATERIALS", "SERVICES AGREEMENT {n}",
    "FREIGHT CHARGES INV {n}", "CONSULTING FEES Q{q} REF {n}",
]


def next_business_day(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def rng_uuid4(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def build_payment(rng: random.Random, directory: Directory) -> Payment:
    created = ANCHOR + timedelta(days=rng.randint(0, 40), minutes=rng.randint(0, 480))
    settle = next_business_day(created.date())
    debtor, debtor_acct = rng.choice(debtor_customers())
    cdtr_agt = rng.choice(directory.creditor_agents())
    creditor, creditor_acct = make_beneficiary(rng, cdtr_agt.bic[4:6], cdtr_agt.bic[:4])
    ref = rng.randint(100000, 999999)
    stamp = created.strftime("%Y%m%d")
    return Payment(
        msg_id=f"FSYN{stamp}{ref}",
        cre_dt_tm=created,
        instr_id=f"FSYN-I-{ref}",
        end_to_end_id=f"E2E-{stamp}-{ref}",
        uetr=rng_uuid4(rng),
        amount=f"{rng.randint(1_000, 250_000)}.{rng.randint(0, 99):02d}",
        currency="USD",
        settlement_date=settle,
        instg_agt=directory.our_bank,
        instd_agt=rng.choice(directory.correspondents()),
        debtor=debtor,
        debtor_account=debtor_acct,
        debtor_agent=directory.our_bank,
        creditor_agent=cdtr_agt,
        creditor=creditor,
        creditor_account=creditor_acct,
        remittance=rng.choice(_PURPOSES).format(n=ref, q=rng.randint(1, 4)),
    )


# ---------- serialization ----------

def _el(parent: etree._Element, tag: str, text: str | None = None, **attrs) -> etree._Element:
    e = etree.SubElement(parent, f"{{{NS_PACS008}}}{tag}", **attrs)
    if text is not None:
        e.text = text
    return e


def _agent(parent, tag: str, agent: Agent, with_name: bool = False) -> None:
    fin = _el(_el(parent, tag), "FinInstnId")
    _el(fin, "BICFI", agent.bic)
    if with_name:
        _el(fin, "Nm", agent.name)


def _address(parent, addr: Address) -> None:
    a = _el(parent, "PstlAdr")
    _el(a, "StrtNm", addr.strt_nm)
    if addr.bldg_nb:
        _el(a, "BldgNb", addr.bldg_nb)
    _el(a, "PstCd", addr.pst_cd)
    _el(a, "TwnNm", addr.twn_nm)
    _el(a, "Ctry", addr.ctry)


def _party(parent, tag: str, party: Party) -> None:
    p = _el(parent, tag)
    _el(p, "Nm", party.name)
    if party.address is not None:
        _address(p, party.address)


def _account(parent, tag: str, acct: Account) -> None:
    acct_id = _el(_el(parent, tag), "Id")
    if acct.iban:
        _el(acct_id, "IBAN", acct.iban)
    else:
        _el(_el(acct_id, "Othr"), "Id", acct.other_id)


def to_xml(p: Payment) -> bytes:
    doc = etree.Element(f"{{{NS_PACS008}}}Document", nsmap={None: NS_PACS008})
    root = _el(doc, "FIToFICstmrCdtTrf")

    grp = _el(root, "GrpHdr")
    _el(grp, "MsgId", p.msg_id)
    _el(grp, "CreDtTm", p.cre_dt_tm.strftime("%Y-%m-%dT%H:%M:%S+00:00"))
    _el(grp, "NbOfTxs", "1")
    _el(_el(grp, "SttlmInf"), "SttlmMtd", "INDA")

    tx = _el(root, "CdtTrfTxInf")
    pid = _el(tx, "PmtId")
    _el(pid, "InstrId", p.instr_id)
    _el(pid, "EndToEndId", p.end_to_end_id)
    _el(pid, "UETR", p.uetr)
    _el(tx, "IntrBkSttlmAmt", p.amount, Ccy=p.currency)
    _el(tx, "IntrBkSttlmDt", p.settlement_date.isoformat())
    _el(tx, "ChrgBr", p.charge_bearer)
    _agent(tx, "InstgAgt", p.instg_agt)
    _agent(tx, "InstdAgt", p.instd_agt)
    _party(tx, "Dbtr", p.debtor)
    _account(tx, "DbtrAcct", p.debtor_account)
    _agent(tx, "DbtrAgt", p.debtor_agent)
    _agent(tx, "CdtrAgt", p.creditor_agent, with_name=True)
    _party(tx, "Cdtr", p.creditor)
    _account(tx, "CdtrAcct", p.creditor_account)
    _el(_el(tx, "RmtInf"), "Ustrd", p.remittance)

    return etree.tostring(doc, pretty_print=True, xml_declaration=True, encoding="UTF-8")
