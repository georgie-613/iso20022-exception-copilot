"""The Exception Copilot: turns one case into one proposal for a human.

Flow per case:
  Lane A (RJCT): explain_reject -> policy action -> draft_repair or outreach note
  Lane B (ACSC + client request): classify the request -> draft recall or escalate

The model only ever does two things: classify free-text recall requests and
word client outreach. It never decides whether something is repairable and
never touches a payment field.

Controls around every model call (enforced here, not in a prompt):
- schema-constrained output (RecallClassification / OutreachDraft)
- fraud override: a fraud keyword in the request, a FRAD reason, or ANY fraud
  indicator from the model escalates. Zero-miss on FRAD beats recall accuracy.
- confidence floor: below COPILOT_MIN_CONFIDENCE the case escalates
- outreach fact check: the message must carry the exact reference and amount
  and may not introduce account-like numbers that weren't in the facts
- fallback: if the model errors or fails a check, the rules baseline answers
- audit: model name, prompt SHA-256, latency, output and fallback are logged
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from iso20022_mcp.parser import parse
from iso20022_mcp.triage import draft_repair, explain_reject

from .llm import OutreachDraft, RecallClassification

Action = Literal["repair", "outreach", "escalate", "recall"]

FRAUD_SIGNALS = re.compile(r"fraud|spoof|scam|phish|compromis|unauthori[sz]ed|hack", re.I)
MODEL_ACTOR = "copilot-model"


def min_confidence() -> float:
    return float(os.environ.get("COPILOT_MIN_CONFIDENCE", "0.7"))


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
    model_note: str | None = None              # fallback / low-confidence / override explanation


class LanguageModel(Protocol):
    name: str

    def classify_recall(self, request_text: str) -> tuple[RecallClassification, str | None]: ...

    def write_outreach(self, context: dict[str, Any]) -> tuple[OutreachDraft, str | None]: ...


class RulesBaseline:
    """Deterministic stand-in for the model. Keeps CI and evals key-free.
    Confidence is 1.0: rules don't hedge, which is exactly their weakness."""

    name = "rules-baseline"

    def classify_recall(self, request_text: str) -> tuple[RecallClassification, None]:
        t = request_text.lower()
        if FRAUD_SIGNALS.search(t):
            reason = "FRAD"
        elif re.search(r"twice|duplicate|again", t):
            reason = "DUPL"
        elif re.search(r"system error|technical|erp|in error|wrong batch", t):
            reason = "TECH"
        else:
            reason = "CUST"
        hits = FRAUD_SIGNALS.findall(t)
        return RecallClassification(reason=reason, fraud_indicators=hits, confidence=1.0,
                                    rationale="keyword rules"), None

    def write_outreach(self, ctx: dict[str, Any]) -> tuple[OutreachDraft, None]:
        msg = (
            f"Hello {ctx['debtor']},\n\n"
            f"Your payment {ctx['end_to_end_id']} for {ctx['currency']} {ctx['amount']} to "
            f"{ctx['creditor']} was returned by the beneficiary's bank: {ctx['title'].lower()}.\n"
            f"{ctx['ask']}\n\n"
            "We have not resubmitted the payment. Reply to this message and we will "
            "process it once we have the information.\n"
        )
        return OutreachDraft(message=msg, confidence=1.0), None


_OUTREACH_ASK = {
    "AC01": "Please confirm the beneficiary's account number with them directly (not by email from the vendor).",
    "AC04": "The beneficiary's account is closed. Please provide their new account details, confirmed by phone.",
    "BE01": "The beneficiary name does not match the account holder. Please confirm who you intended to pay.",
    "RR03": "Please send the beneficiary's full street address, city, postal code and country.",
    "AM04": "Please confirm the account is funded so we can resubmit, or tell us to cancel.",
}


class ModelCheckFailed(Exception):
    pass


_CREDENTIAL = re.compile(r"password|passcode|\bPIN\b|one-time code|security code", re.I)
_NEGATION = re.compile(r"\b(never|not|don't|won't|no one)\b", re.I)


def asks_for_credentials(message: str) -> bool:
    """True if any sentence asks for a credential. A warning like "please do not share any
    passwords" mentions one without asking for it: a negation *before* the credential word
    in the same sentence makes it a warning. (Live run 39358916870 blocked three good
    messages on the bare word; see evals/results.)"""
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", message):
        m = _CREDENTIAL.search(sentence)
        if m and not _NEGATION.search(sentence[: m.start()]):
            return True
    return False


def check_outreach(message: str, ctx: dict[str, Any]) -> None:
    """The model may word the message; it may not change or add facts."""
    if ctx["end_to_end_id"] not in message:
        raise ModelCheckFailed("payment reference missing or altered")
    if ctx["amount"] not in message:
        raise ModelCheckFailed("amount missing or altered")
    allowed = " ".join(str(v) for v in ctx.values())
    for num in re.findall(r"\d{6,}", message):
        if num not in allowed:
            raise ModelCheckFailed(f"introduced a number not in the facts: {num}")
    if asks_for_credentials(message):
        raise ModelCheckFailed("asks for credentials")


class Copilot:
    def __init__(self, model: LanguageModel | None = None, audit=None) -> None:
        self.model = model or RulesBaseline()
        self.fallback = RulesBaseline()
        self.audit = audit

    # ---- model calls: timed, hashed, checked, logged, with fallback ----
    def _call(self, case_id: str, kind: str, fn_name: str, arg: Any, check=None):
        start = time.perf_counter()
        used, error, fallback_kind = self.model, None, None
        out = prompt = rejected = None
        try:
            out, prompt = getattr(self.model, fn_name)(arg)
            if out is None:  # refusal or truncated output: no schema-valid answer
                raise RuntimeError("model returned no structured output")
            if check:
                check(out)
        except ModelCheckFailed as e:
            # The model answered; a guardrail blocked the answer. The control working.
            error, fallback_kind = f"ModelCheckFailed: {e}"[:300], "check_rejected"
            rejected = out.model_dump() if out is not None else None
        except Exception as e:  # noqa: BLE001 - transport/API/refusal: no usable model answer
            error, fallback_kind = f"{type(e).__name__}: {e}"[:300], "api_error"
        if fallback_kind:
            used = self.fallback
            out, _ = getattr(self.fallback, fn_name)(arg)
        latency_ms = round((time.perf_counter() - start) * 1000)
        if self.audit is not None and not isinstance(self.model, RulesBaseline):
            self.audit.append(case_id, MODEL_ACTOR, "model_call", {
                "kind": kind,
                "model": self.model.name,
                "answered_by": used.name,
                "fallback": used is self.fallback,
                "fallback_kind": fallback_kind,         # None | check_rejected | api_error
                "error": error,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest() if prompt else None,
                "latency_ms": latency_ms,
                "rejected_output": rejected,            # what the guardrail blocked, for review
                "output": out.model_dump(),
            })
        return out, used, error

    def triage(self, case_dir: str | Path) -> Proposal:
        d = Path(case_dir)
        p8, p2 = (d / "pacs008.xml").read_bytes(), (d / "pacs002.xml").read_bytes()
        status = parse(p2)
        if status["status"] == "ACSC":
            request = (d / "request.txt").read_text(encoding="utf-8") if (d / "request.txt").exists() else ""
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
            ctx = {
                "debtor": tx["debtor"]["name"], "creditor": tx["creditor"]["name"],
                "end_to_end_id": tx["end_to_end_id"], "currency": tx["currency"],
                "amount": tx["amount"], "title": e["title"],
                "ask": _OUTREACH_ASK.get(e["reason_code"], "Please contact us about this payment."),
            }
            draft, used, error = self._call(case_id, "outreach", "write_outreach", ctx,
                                            check=lambda o: check_outreach(o.message, ctx))
            note = f"Model output rejected ({error}); rules template used." if error else None
            if used is not self.fallback and draft.confidence < min_confidence():
                return Proposal(**base, action="escalate", model_note=f"low confidence {draft.confidence:.2f}",
                                summary=f"{e['title']}. Model unsure about the client message; a person drafts it.")
            return Proposal(**base, action="outreach", outreach_message=draft.message, model_note=note,
                            summary=f"{e['title']}. Client must confirm; draft message attached.")

        return Proposal(**base, action="escalate", summary=f"{e['title']}. {e['explanation']}")

    # ---- Lane B ----
    def _recall(self, case_id: str, p8: bytes, request: str) -> Proposal:
        c, used, error = self._call(case_id, "classify_recall", "classify_recall", request)
        base = dict(case_id=case_id, lane="B", model=self.model.name)
        note = f"Model unavailable ({error}); rules baseline used." if error else None

        # Hard override: any fraud signal from any source escalates, whatever the reason says.
        if FRAUD_SIGNALS.search(request) or c.reason == "FRAD" or c.fraud_indicators:
            signals = ", ".join(c.fraud_indicators) or "fraud keyword in request"
            return Proposal(**base, reason_code="FRAD", action="escalate", model_note=note,
                            summary=f"Possible fraud ({signals}). Route to fraud operations now; no recall drafted.")
        if used is not self.fallback and c.confidence < min_confidence():
            return Proposal(**base, reason_code=c.reason, action="escalate",
                            model_note=f"low confidence {c.confidence:.2f}",
                            summary=f"Recall reason unclear (model {c.confidence:.2f} on {c.reason}). A person decides.")

        tx = parse(p8)["transaction"]
        recall = {
            "message": "camt.056.001.08",
            "orgnl_msg_id": parse(p8)["group_header"]["msg_id"],
            "orgnl_end_to_end_id": tx["end_to_end_id"],
            "orgnl_uetr": tx["uetr"],
            "orgnl_amount": tx["amount"],
            "currency": tx["currency"],
            "assignee_bic": tx["instructed_agent"]["bic"],
            "cancellation_reason": c.reason,
        }
        return Proposal(**base, reason_code=c.reason, action="recall", recall=recall, model_note=note,
                        summary=f"Client requests recall ({c.reason}). camt.056 drafted for approval.")
