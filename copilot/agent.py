"""The Exception Copilot: turns one case into one proposal for a human.

Flow per case:
  Lane A (RJCT): explain_reject -> policy action -> draft_repair or outreach note
  Lane B (ACSC + client request): classify the request -> draft recall or escalate

The model (when configured) only ever does two things: classify free-text
recall requests and word client outreach. It never decides whether something
is repairable and never touches a payment field. Without a model, the
deterministic baseline below runs, which is what CI and the eval use.

Hard rule that overrides any model output: any fraud signal in a recall
request escalates. Zero-miss on FRAD beats recall accuracy.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from iso20022_mcp.parser import parse
from iso20022_mcp.triage import draft_repair, explain_reject

Action = Literal["repair", "outreach", "escalate", "recall"]

FRAUD_SIGNALS = re.compile(r"fraud|spoof|scam|phish|compromis|unauthori[sz]ed|hack", re.I)


class Proposal(BaseModel):
    case_id: str
    lane: Literal["A", "B"]
    reason_code: str | None
    action: Action
    summary: str
    repair: dict[str, Any] | None = None       # draft_repair output when action == repair
    outreach_message: str | None = None        # when action == outreach
    recall: dict[str, Any] | None = None       # camt.056 fields when action == recall
    evidence: list[dict] | None = None
    proposed_by: str = "copilot"
    model: str = "rules-baseline"


class LanguageModel(Protocol):
    name: str

    def classify_recall(self, request_text: str) -> str: ...

    def write_outreach(self, context: dict[str, Any]) -> str: ...


class RulesBaseline:
    """Deterministic stand-in for the model. Keeps CI and evals key-free."""

    name = "rules-baseline"

    def classify_recall(self, request_text: str) -> str:
        t = request_text.lower()
        if FRAUD_SIGNALS.search(t):
            return "FRAD"
        if re.search(r"twice|duplicate|again", t):
            return "DUPL"
        if re.search(r"system error|technical|erp|in error|wrong batch", t):
            return "TECH"
        return "CUST"

    def write_outreach(self, ctx: dict[str, Any]) -> str:
        return (
            f"Hello {ctx['debtor']},\n\n"
            f"Your payment {ctx['end_to_end_id']} for {ctx['currency']} {ctx['amount']} to "
            f"{ctx['creditor']} was returned by the beneficiary's bank: {ctx['title'].lower()}.\n"
            f"{ctx['ask']}\n\n"
            "We have not resubmitted the payment. Reply to this message and we will "
            "process it once we have the information.\n"
        )


_OUTREACH_ASK = {
    "AC01": "Please confirm the beneficiary's account number with them directly (not by email from the vendor).",
    "AC04": "The beneficiary's account is closed. Please provide their new account details, confirmed by phone.",
    "BE01": "The beneficiary name does not match the account holder. Please confirm who you intended to pay.",
    "RR03": "Please send the beneficiary's full street address, city, postal code and country.",
    "AM04": "Please confirm the account is funded so we can resubmit, or tell us to cancel.",
}


class Copilot:
    def __init__(self, model: LanguageModel | None = None) -> None:
        self.model = model or RulesBaseline()

    def triage(self, case_dir: str | Path) -> Proposal:
        d = Path(case_dir)
        p8, p2 = (d / "pacs008.xml").read_bytes(), (d / "pacs002.xml").read_bytes()
        status = parse(p2)
        if status["status"] == "ACSC":
            request = (d / "request.txt").read_text() if (d / "request.txt").exists() else ""
            return self._recall(d.name, p8, request)
        return self._reject(d.name, p8, p2)

    # ---- Lane A ----
    def _reject(self, case_id: str, p8: bytes, p2: bytes) -> Proposal:
        e = explain_reject(p2, p8)
        if "error" in e:
            return Proposal(case_id=case_id, lane="A", reason_code=None, action="escalate",
                            summary=f"Could not read the case: {e['error']}", model=self.model.name)
        base = dict(case_id=case_id, lane="A", reason_code=e["reason_code"],
                    evidence=e.get("evidence"), model=self.model.name)
        action = e["recommended_action"]

        if action == "repair":
            draft = draft_repair(p8, p2)
            if draft["status"] == "DRAFT_PENDING_APPROVAL":
                ch = "; ".join(f"{c['field']}: {c['before']} -> {c['after']}" for c in draft["changes"])
                return Proposal(**base, action="repair", repair=draft,
                                summary=f"{e['title']}. Proposed repair: {ch}")
            return Proposal(**base, action="escalate",
                            summary=f"{e['title']}. Repair refused: {draft['reason']}")

        if action == "outreach":
            tx = parse(p8)["transaction"]
            msg = self.model.write_outreach({
                "debtor": tx["debtor"]["name"], "creditor": tx["creditor"]["name"],
                "end_to_end_id": tx["end_to_end_id"], "currency": tx["currency"],
                "amount": tx["amount"], "title": e["title"],
                "ask": _OUTREACH_ASK.get(e["reason_code"], "Please contact us about this payment."),
            })
            return Proposal(**base, action="outreach", outreach_message=msg,
                            summary=f"{e['title']}. Client must confirm; draft message attached.")

        return Proposal(**base, action="escalate", summary=f"{e['title']}. {e['explanation']}")

    # ---- Lane B ----
    def _recall(self, case_id: str, p8: bytes, request: str) -> Proposal:
        reason = self.model.classify_recall(request)
        # Hard override: fraud signal anywhere in the text escalates, whatever the model said.
        if FRAUD_SIGNALS.search(request) or reason == "FRAD":
            return Proposal(case_id=case_id, lane="B", reason_code="FRAD", action="escalate",
                            summary="Client reports possible fraud. Route to fraud operations now; no recall drafted.",
                            model=self.model.name)
        tx = parse(p8)["transaction"]
        recall = {
            "message": "camt.056.001.08",
            "orgnl_msg_id": parse(p8)["group_header"]["msg_id"],
            "orgnl_end_to_end_id": tx["end_to_end_id"],
            "orgnl_uetr": tx["uetr"],
            "orgnl_amount": tx["amount"],
            "currency": tx["currency"],
            "assignee_bic": tx["instructed_agent"]["bic"],
            "cancellation_reason": reason,
        }
        return Proposal(case_id=case_id, lane="B", reason_code=reason, action="recall", recall=recall,
                        summary=f"Client requests recall ({reason}). camt.056 drafted for approval.",
                        model=self.model.name)
