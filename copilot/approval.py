"""Human approval gate with maker-checker.

The copilot is the maker. A named human is the checker. The two can never be
the same identity, and nothing reaches 'approved' without a human decision.
Release to the network is out of scope: approval is the last step here.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from .agent import Proposal
from .audit import AuditLog

AI_ACTORS = {"copilot", "copilot-model"}


class ApprovalError(Exception):
    pass


class ApprovalQueue:
    def __init__(self, audit: AuditLog) -> None:
        self.audit = audit
        self._pending: dict[str, Proposal] = {}
        self._decided: dict[str, str] = {}

    def submit(self, p: Proposal) -> None:
        if p.case_id in self._pending or p.case_id in self._decided:
            raise ApprovalError(f"{p.case_id} already submitted")
        self._pending[p.case_id] = p
        payload = {"action": p.action, "reason_code": p.reason_code, "model": p.model,
                   "summary": p.summary}
        if p.repair:
            payload["changes"] = p.repair["changes"]
            payload["draft_sha256"] = hashlib.sha256(p.repair["repaired_xml"].encode()).hexdigest()
        if p.recall:
            payload["recall"] = p.recall
        # The full proposal is pinned in the log: after a restart the reviewer sees exactly
        # what was proposed, never a regenerated (possibly different) model output.
        payload["proposal"] = p.model_dump()
        self.audit.append(p.case_id, p.proposed_by, "proposed", payload)

    def restore(self) -> dict[str, Proposal]:
        """Rebuild queue state from the audit log alone. Returns every logged proposal;
        undecided ones go back to pending, decided ones stay decided."""
        proposals: dict[str, Proposal] = {}
        for e in self.audit.events():
            cid = e["case_id"]
            if e["event"] == "proposed" and "proposal" in e["payload"]:
                proposals[cid] = Proposal.model_validate(e["payload"]["proposal"])
                self._pending[cid] = proposals[cid]
            elif e["event"] in ("approved", "rejected"):
                self._pending.pop(cid, None)
                self._decided[cid] = "approve" if e["event"] == "approved" else "reject"
        return proposals

    def load(self, proposals: list[Proposal]) -> None:
        """Rebuild queue state after a restart, from the audit log (the source of truth).
        Cases already decided stay decided; proposed-but-undecided return to pending
        without a duplicate 'proposed' event; new cases are submitted."""
        last: dict[str, str] = {}
        for e in self.audit.events():
            last[e["case_id"]] = e["event"]
        for p in proposals:
            state = last.get(p.case_id)
            if state in ("approved", "rejected"):
                self._decided[p.case_id] = "approve" if state == "approved" else "reject"
            elif state == "proposed":
                self._pending[p.case_id] = p
            else:
                self.submit(p)

    def status(self, case_id: str) -> str:
        if case_id in self._pending:
            return "pending"
        return {"approve": "approved", "reject": "rejected"}.get(self._decided.get(case_id, ""), "unknown")

    def get(self, case_id: str) -> Proposal | None:
        return self._pending.get(case_id)

    def pending(self) -> list[Proposal]:
        return list(self._pending.values())

    def decide(self, case_id: str, approver: str, decision: Literal["approve", "reject"],
               note: str = "") -> None:
        p = self._pending.get(case_id)
        if p is None:
            raise ApprovalError(f"{case_id} is not pending")
        if not approver or approver in AI_ACTORS or approver == p.proposed_by:
            raise ApprovalError("maker-checker: the proposer cannot approve its own proposal")
        if decision == "reject" and not note:
            raise ApprovalError("a rejection needs a reason")
        del self._pending[case_id]
        self._decided[case_id] = decision
        self.audit.append(case_id, approver, "approved" if decision == "approve" else "rejected",
                          {"note": note, "action": p.action})
