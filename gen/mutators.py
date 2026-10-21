"""Lane A: one mutator per reject reason code.

Each mutator takes a valid Payment, plants the defect that causes the reject,
and returns a Mutation: the answer key plus the rejecting bank's free-text
explanation for the pacs.002.

Repair boundary (from the PRD): the copilot may only draft formatting or
enrichment fixes from data we hold. Anything that needs the truth from the
client goes to outreach; anything compliance-, fraud- or judgment-related is
escalated to a human. Never guess.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable

from .base import next_business_day
from .customers import debtor_master
from .directory import Directory
from .model import Action, Payment


@dataclass
class Mutation:
    action: Action
    rationale: str
    addtl_inf: str | None = None
    allowed_fields: list[str] = field(default_factory=list)
    expected_value: str | dict | None = None


Mutator = Callable[[Payment, random.Random, Directory], Mutation]


def rc01(p: Payment, rng: random.Random, d: Directory) -> Mutation:
    """Invalid BIC: well-formed creditor agent BIC that isn't in the directory.
    The agent name is still present, so the true BIC is recoverable."""
    true_bic = p.creditor_agent.bic
    p.creditor_agent = p.creditor_agent.model_copy(
        update={"bic": d.unlisted_bic(rng, true_bic[4:6])})
    return Mutation(
        action="repair",
        allowed_fields=["CdtrAgt.FinInstnId.BICFI"],
        expected_value=true_bic,
        addtl_inf="CREDITOR AGENT BIC NOT FOUND IN DIRECTORY",
        rationale="Formatting/enrichment: correct BIC recoverable from directory by agent name.",
    )


def _typo(code: str, rng: random.Random) -> str:
    """One keying error in a 4-letter bank code: adjacent transposition or one wrong letter."""
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    i = rng.randrange(3)
    if code[i] != code[i + 1] and rng.random() < 0.5:
        return code[:i] + code[i + 1] + code[i] + code[i + 2:]
    j = rng.randrange(4)
    return code[:j] + rng.choice([c for c in letters if c != code[j]]) + code[j + 1:]


def rc01_typo(p: Payment, rng: random.Random, d: Directory) -> Mutation:
    """Invalid BIC from a keying error: one or two characters off a listed BIC.
    The agent name is on the message and points to the BIC it was meant to be."""
    true_bic = p.creditor_agent.bic
    while True:
        bad = _typo(true_bic[:4], rng) + true_bic[4:]
        if bad not in d and bad != true_bic:
            break
    p.creditor_agent = p.creditor_agent.model_copy(update={"bic": bad})
    return Mutation(
        action="repair",
        allowed_fields=["CdtrAgt.FinInstnId.BICFI"],
        expected_value=true_bic,
        addtl_inf="CREDITOR AGENT BIC NOT FOUND IN DIRECTORY",
        rationale="Keying error: the BIC is one or two characters off the BIC of the bank named on the message.",
    )


def rc01_wrong_bank(p: Payment, rng: random.Random, d: Directory) -> Mutation:
    """Invalid BIC that looks nothing like the named bank's BIC. More likely the client
    picked the wrong bank than mistyped one: ask them, don't swap in a different bank."""
    m = rc01(p, rng, d)
    return Mutation(action="outreach", addtl_inf=m.addtl_inf,
                    rationale="BIC is not a typo of the named bank's BIC: confirm the beneficiary bank with the client.")


def ch16(p: Payment, rng: random.Random, d: Directory) -> Mutation:
    """Element content formally incorrect: lowercase currency fails the schema."""
    true_ccy = p.currency
    p.currency = true_ccy.lower()
    return Mutation(
        action="repair",
        allowed_fields=["IntrBkSttlmAmt/@Ccy"],
        expected_value=true_ccy,
        addtl_inf="SCHEMA VALIDATION FAILED: IntrBkSttlmAmt/@Ccy",
        rationale="Pure formatting: the intended value is unambiguous.",
    )


def dt01(p: Payment, rng: random.Random, d: Directory) -> Mutation:
    """Invalid date: settlement date falls on a weekend."""
    days_to_sat = (5 - p.settlement_date.weekday()) % 7
    p.settlement_date = p.settlement_date + timedelta(days=days_to_sat + rng.choice([0, 1]))
    fixed = next_business_day(p.settlement_date)
    return Mutation(
        action="repair",
        allowed_fields=["IntrBkSttlmDt"],
        expected_value=fixed.isoformat(),
        addtl_inf="SETTLEMENT DATE IS NOT A BUSINESS DAY",
        rationale="Formatting: roll to next business day per standard convention.",
    )


def rr02(p: Payment, rng: random.Random, d: Directory) -> Mutation:
    """Missing debtor address. Debtor is our customer, so KYC master has it."""
    addr = debtor_master()[p.debtor.name]
    p.debtor = p.debtor.model_copy(update={"address": None})
    return Mutation(
        action="repair",
        allowed_fields=["Dbtr.PstlAdr"],
        expected_value=addr.model_dump(),
        addtl_inf="MISSING DEBTOR POSTAL ADDRESS",
        rationale="Enrichment from our own customer master data.",
    )


def rr03(p: Payment, rng: random.Random, d: Directory) -> Mutation:
    """Missing creditor address. We do not hold beneficiary master data."""
    p.creditor = p.creditor.model_copy(update={"address": None})
    return Mutation(
        action="outreach",
        addtl_inf="MISSING CREDITOR POSTAL ADDRESS",
        rationale="Data-truth: only the client knows the beneficiary's address.",
    )


def ac01(p: Payment, rng: random.Random, d: Directory) -> Mutation:
    """Incorrect account number: two adjacent digits transposed."""
    acct = p.creditor_account
    value = acct.iban or acct.other_id
    start = 4 if acct.iban else 0
    digit_pairs = [i for i in range(start, len(value) - 1)
                   if value[i].isdigit() and value[i + 1].isdigit() and value[i] != value[i + 1]]
    i = rng.choice(digit_pairs)
    bad = value[:i] + value[i + 1] + value[i] + value[i + 2:]
    p.creditor_account = acct.model_copy(update={"iban": bad} if acct.iban else {"other_id": bad})
    return Mutation(
        action="outreach",
        addtl_inf="ACCOUNT NUMBER INVALID OR DOES NOT EXIST",
        rationale="Data-truth: never guess an account number, even if a transposition looks obvious.",
    )


def ac04(p: Payment, rng: random.Random, d: Directory) -> Mutation:
    return Mutation(action="outreach", addtl_inf="CREDITOR ACCOUNT CLOSED",
                    rationale="Data-truth: client must supply new beneficiary account.")


def be01(p: Payment, rng: random.Random, d: Directory) -> Mutation:
    """Inconsistent with end customer: creditor name doesn't match the account."""
    words = p.creditor.name.split()
    words[1] = rng.choice([w for w in ["Aurora", "Delta", "Sol", "Horizonte", "Cumbre",
                                        "Atlas", "Brisa", "Faro", "Nova", "Puente"] if w != words[1]])
    p.creditor = p.creditor.model_copy(update={"name": " ".join(words)})
    return Mutation(action="outreach", addtl_inf="CREDITOR NAME DOES NOT MATCH ACCOUNT HOLDER",
                    rationale="Data-truth: confirm the beneficiary with the client; possible fraud signal.")


def am04(p: Payment, rng: random.Random, d: Directory) -> Mutation:
    return Mutation(action="outreach", addtl_inf="INSUFFICIENT FUNDS",
                    rationale="Client must fund the account or authorize a resend.")


def am05(p: Payment, rng: random.Random, d: Directory) -> Mutation:
    return Mutation(action="escalate", addtl_inf="DUPLICATE PAYMENT DETECTED",
                    rationale="Human confirms whether it is a true duplicate before closing.")


def ac06(p: Payment, rng: random.Random, d: Directory) -> Mutation:
    return Mutation(action="escalate", addtl_inf="ACCOUNT BLOCKED",
                    rationale="Blocked account can be sanctions/legal related: always human.")


def ag01(p: Payment, rng: random.Random, d: Directory) -> Mutation:
    return Mutation(action="escalate", addtl_inf="TRANSACTION FORBIDDEN ON THIS ACCOUNT TYPE",
                    rationale="Policy/product restriction: human judgment required.")


def rr04(p: Payment, rng: random.Random, d: Directory) -> Mutation:
    return Mutation(action="escalate", addtl_inf=None,
                    rationale="Regulatory reason: always human, zero-miss guardrail.")


def ms03(p: Payment, rng: random.Random, d: Directory) -> Mutation:
    return Mutation(action="escalate", addtl_inf=None,
                    rationale="Reason not specified: nothing safe to infer, always human.")


LANE_A: dict[str, Mutator] = {
    "RC01": rc01_typo, "CH16": ch16, "DT01": dt01, "RR02": rr02, "RR03": rr03,
    "AC01": ac01, "AC04": ac04, "BE01": be01, "AM04": am04, "AM05": am05,
    "AC06": ac06, "AG01": ag01, "RR04": rr04, "MS03": ms03,
}

# Codes whose original message intentionally fails the XSD.
SCHEMA_INVALID_CODES = {"CH16"}
