"""Lane B: settled payments the client asks to recall (camt.056 candidates).

Each case is the original pacs.008, a pacs.002 with status ACSC (settled),
and the client's free-text recall request. DUPL/CUST/TECH get a drafted
recall for human approval. FRAD is always escalated: fraud handling has its
own process and the copilot must not act on it.
"""

from __future__ import annotations

import random

from .model import Action, Payment

_REQUESTS = {
    "DUPL": [
        "We sent payment {e2e} twice by mistake. Please recall the second one for {ccy} {amt}.",
        "Duplicate wire {e2e} - our AP system released it again. Kindly recall.",
    ],
    "CUST": [
        "Please cancel wire {e2e} for {ccy} {amt}; the supplier contract was terminated.",
        "We no longer want payment {e2e} to go through. Customer requests recall.",
    ],
    "TECH": [
        "Our ERP sent {e2e} with the wrong batch due to a system error. Please recall.",
        "Technical problem on our side generated wire {e2e} in error. Request recall.",
    ],
    "FRAD": [
        "URGENT: payment {e2e} for {ccy} {amt} was authorized after a spoofed vendor email. Please stop it.",
        "We believe wire {e2e} is fraudulent. The vendor's bank details were changed by email. Recall immediately.",
    ],
}

_ACTION: dict[str, Action] = {"DUPL": "recall", "CUST": "recall", "TECH": "recall", "FRAD": "escalate"}
_RATIONALE = {
    "DUPL": "Duplicate confirmed by client: draft camt.056 with reason DUPL for approval.",
    "CUST": "Client-requested cancellation: draft camt.056 with reason CUST for approval.",
    "TECH": "Technical error: draft camt.056 with reason TECH for approval.",
    "FRAD": "Fraud claim: always human, fraud process owns it; copilot must not draft.",
}

LANE_B_CODES = tuple(_REQUESTS)


def recall_request(code: str, p: Payment, rng: random.Random) -> tuple[str, Action, str]:
    text = rng.choice(_REQUESTS[code]).format(e2e=p.end_to_end_id, ccy=p.currency, amt=p.amount)
    return text, _ACTION[code], _RATIONALE[code]
