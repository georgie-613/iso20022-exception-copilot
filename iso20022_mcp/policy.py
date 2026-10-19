"""Reason-code policy: what each reject means and what the copilot may do.

This table IS the governance. The model does not decide whether a payment is
repairable; this table does, and the tools enforce it in code.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Action = Literal["repair", "outreach", "escalate"]


class CodePolicy(BaseModel):
    code: str
    title: str
    explanation: str
    action: Action
    allowed_fields: list[str] = []


_P = [
    CodePolicy(code="RC01", title="Invalid BIC",
               explanation="The receiving bank could not route the payment because the creditor agent's BIC is not a valid, listed identifier.",
               action="repair", allowed_fields=["CdtrAgt.FinInstnId.BICFI"]),
    CodePolicy(code="CH16", title="Element content formally incorrect",
               explanation="A field in the message does not follow the ISO 20022 format rules, so it failed schema validation.",
               action="repair", allowed_fields=["IntrBkSttlmAmt/@Ccy"]),
    CodePolicy(code="DT01", title="Invalid date",
               explanation="The settlement date is not a valid business day for this payment.",
               action="repair", allowed_fields=["IntrBkSttlmDt"]),
    CodePolicy(code="RR02", title="Missing debtor name or address",
               explanation="Regulations require the payer's full address; it was missing from the payment.",
               action="repair", allowed_fields=["Dbtr.PstlAdr"]),
    CodePolicy(code="RR03", title="Missing creditor name or address",
               explanation="Regulations require the beneficiary's address. We do not hold it; only the client does.",
               action="outreach"),
    CodePolicy(code="AC01", title="Incorrect account number",
               explanation="The beneficiary account number is wrong or does not exist. Never guess an account number.",
               action="outreach"),
    CodePolicy(code="AC04", title="Closed account",
               explanation="The beneficiary's account is closed. The client must provide new details.",
               action="outreach"),
    CodePolicy(code="BE01", title="Inconsistent with end customer",
               explanation="The beneficiary name does not match the account holder. Confirm with the client; this can be a fraud signal.",
               action="outreach"),
    CodePolicy(code="AM04", title="Insufficient funds",
               explanation="There were not enough funds to settle the payment. The client must fund or re-authorize.",
               action="outreach"),
    CodePolicy(code="AM05", title="Duplication",
               explanation="The receiving bank flagged this as a duplicate. A person must confirm before closing.",
               action="escalate"),
    CodePolicy(code="AC06", title="Blocked account",
               explanation="The account is blocked. This can be legal or sanctions-related.",
               action="escalate"),
    CodePolicy(code="AG01", title="Transaction forbidden",
               explanation="This type of transaction is not allowed on the account. Needs a policy decision.",
               action="escalate"),
    CodePolicy(code="RR04", title="Regulatory reason",
               explanation="Rejected for a regulatory reason. Compliance must review; no automated handling.",
               action="escalate"),
    CodePolicy(code="MS03", title="Reason not specified",
               explanation="The bank gave no reason. Nothing can be safely inferred.",
               action="escalate"),
]

POLICY: dict[str, CodePolicy] = {p.code: p for p in _P}


def policy_for(code: str | None) -> CodePolicy:
    """Unknown codes are escalated: the safe default is a person."""
    if code and code in POLICY:
        return POLICY[code]
    return CodePolicy(code=code or "?", title="Unknown reason code",
                      explanation="This reason code is not in the v1 policy table.",
                      action="escalate")
