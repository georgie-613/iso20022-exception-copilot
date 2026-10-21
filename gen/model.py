"""Data models for synthetic cases.

A Payment is the business content of one pacs.008 credit transfer. The
generator builds a valid Payment, mutators corrupt it to produce a reject
cause, and the Label records the answer key for the eval.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class Address(BaseModel):
    strt_nm: str
    bldg_nb: str | None = None
    pst_cd: str
    twn_nm: str
    ctry: str  # ISO 3166 alpha-2


class Party(BaseModel):
    name: str
    address: Address | None  # None = address block removed (RR02 / RR03)


class Agent(BaseModel):
    bic: str
    name: str


class Account(BaseModel):
    iban: str | None = None
    other_id: str | None = None


class Payment(BaseModel):
    msg_id: str
    cre_dt_tm: datetime
    instr_id: str
    end_to_end_id: str
    uetr: str
    amount: str  # decimal string, two places, e.g. "12500.00"
    currency: str
    settlement_date: date
    charge_bearer: str = "SHAR"
    instg_agt: Agent
    instd_agt: Agent
    debtor: Party
    debtor_account: Account
    debtor_agent: Agent
    creditor_agent: Agent
    creditor: Party
    creditor_account: Account
    remittance: str


Action = Literal["repair", "outreach", "escalate", "recall"]


class Label(BaseModel):
    """Answer key for one case.

    action:
      repair   - formatting/enrichment fix the copilot may draft (human approves)
      outreach - data-truth problem; contact the client, never guess
      escalate - always-human: compliance, fraud, unspecified, or judgment call
      recall   - Lane B: draft a camt.056 recall request (human approves)
    """

    case_id: str
    set: Literal["eval", "demo", "holdout", "address", "address-holdout"]
    lane: Literal["A", "B", "C"]
    code: str
    action: Action
    allowed_fields: list[str] = Field(default_factory=list)
    expected_value: str | dict | None = None
    rationale: str
