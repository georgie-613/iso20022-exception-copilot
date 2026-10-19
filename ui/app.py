"""Approval screen: the human side of the copilot.

    uvicorn ui.app:create_app --factory    # then open http://127.0.0.1:8000

Env:
  CASES_DIR   case folder to load (default: cases)
  CASE_SET    which labeled set to show (default: demo)
  AUDIT_LOG   audit log path (default: audit.jsonl)

The audit log is the source of truth: restart the app and every decision is
still there. v1 has no login; the reviewer types their name and the gate
refuses the copilot's own identity. Real deployments put SSO in front.
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from copilot.agent import Copilot, Proposal
from copilot.approval import ApprovalError, ApprovalQueue
from copilot.audit import AuditLog

ACTION_LABEL = {
    "repair": ("Repair drafted", "repair"),
    "outreach": ("Client outreach", "outreach"),
    "escalate": ("Escalated to a person", "escalate"),
    "recall": ("Recall drafted", "recall"),
}


def create_app(cases_dir: str | Path | None = None, case_set: str | None = None,
               audit_path: str | Path | None = None) -> FastAPI:
    cases_dir = Path(cases_dir or os.environ.get("CASES_DIR", "cases"))
    case_set = case_set or os.environ.get("CASE_SET", "demo")
    audit = AuditLog(audit_path or os.environ.get("AUDIT_LOG", "audit.jsonl"))
    queue = ApprovalQueue(audit)
    copilot = Copilot()

    proposals: dict[str, Proposal] = {}
    for d in sorted(cases_dir.iterdir()) if cases_dir.exists() else []:
        lf = d / "label.json"
        if lf.exists() and json.loads(lf.read_text()).get("set") == case_set:
            proposals[d.name] = copilot.triage(d)
    queue.load(list(proposals.values()))

    app = FastAPI(title="ISO 20022 Exception Copilot")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        rows = []
        for cid, p in proposals.items():
            label, cls = ACTION_LABEL[p.action]
            status = queue.status(cid)
            rows.append(
                f'<a class="row" href="/case/{_e(cid)}">'
                f'<span class="cid">{_e(cid)}</span>'
                f'<span class="code">{_e(p.reason_code or "")}</span>'
                f'<span class="pill {cls}">{label}</span>'
                f'<span class="status {status}">{status}</span>'
                f'<span class="sum">{_e(p.summary)}</span></a>'
            )
        pending = sum(queue.status(c) == "pending" for c in proposals)
        ok, why = audit.verify()
        body = (
            f'<h1>Exception queue</h1><p class="muted">{pending} awaiting review · '
            f'{len(proposals)} cases · model: {_e(copilot.model.name)} · '
            f'audit log: <a href="/audit">{"verified" if ok else "BROKEN: " + _e(why)}</a></p>'
            f'<div class="list">{"".join(rows) or "<p>No cases found. Run python -m gen.build first.</p>"}</div>'
        )
        return _page("Queue", body)

    @app.get("/case/{case_id}", response_class=HTMLResponse)
    def case(case_id: str, error: str | None = None) -> str:
        p = proposals.get(case_id)
        if p is None:
            raise HTTPException(404, "unknown case")
        label, cls = ACTION_LABEL[p.action]
        status = queue.status(case_id)
        parts = [
            '<p><a href="/">&larr; Queue</a></p>',
            f'<h1>{_e(case_id)} <span class="pill {cls}">{label}</span></h1>',
            f'<p class="lead">{_e(p.summary)}</p>',
        ]
        if error:
            parts.append(f'<div class="error">{_e(error)}</div>')

        if p.repair:
            parts.append("<h2>Proposed change</h2><table><tr><th>Field</th><th>Before</th><th>After</th></tr>")
            for c in p.repair["changes"]:
                parts.append(f'<tr><td><code>{_e(c["field"])}</code></td>'
                             f'<td class="before">{_fmt(c["before"])}</td>'
                             f'<td class="after">{_fmt(c["after"])}</td></tr>')
            parts.append("</table><p class=\"muted\">Only the fields above changed; the tool rejects any draft that "
                         "touches anything else. The repaired message passed validation.</p>")
            parts.append(f'<details><summary>Repaired XML</summary><pre>{_e(p.repair["repaired_xml"])}</pre></details>')
        if p.outreach_message:
            parts.append(f'<h2>Draft message to client</h2><pre class="msg">{_e(p.outreach_message)}</pre>')
        if p.recall:
            parts.append("<h2>Recall request (camt.056) fields</h2><table>")
            parts += [f"<tr><td>{_e(k)}</td><td><code>{_e(str(v))}</code></td></tr>" for k, v in p.recall.items()]
            parts.append("</table>")
        if p.evidence:
            parts.append("<h2>Evidence in the original message</h2><ul>")
            parts += [f'<li><code>{_e(f["field"])}</code>: {_e(f["message"])}</li>' for f in p.evidence]
            parts.append("</ul>")

        if status == "pending":
            parts.append(f"""
<h2>Decision</h2>
<form method="post" action="/case/{_e(case_id)}/decide" class="decide">
  <label>Reviewer <input name="reviewer" required placeholder="your name"></label>
  <label>Note <input name="note" placeholder="required to reject"></label>
  <div class="btns">
    <button name="decision" value="approve" class="approve">Approve</button>
    <button name="decision" value="reject" class="reject">Reject</button>
  </div>
</form>""")
        else:
            parts.append(f'<p class="status {status}">Decision recorded: {status}</p>')

        trail = audit.for_case(case_id)
        parts.append("<h2>Audit trail</h2><table><tr><th>#</th><th>When (UTC)</th><th>Who</th><th>Event</th><th>Hash</th></tr>")
        parts += [f'<tr><td>{e["seq"]}</td><td>{_e(e["ts"][:19])}</td><td>{_e(e["actor"])}</td>'
                  f'<td>{_e(e["event"])}</td><td><code>{e["hash"][:12]}</code></td></tr>' for e in trail]
        parts.append("</table>")
        return _page(case_id, "".join(parts))

    @app.post("/case/{case_id}/decide")
    def decide(case_id: str, reviewer: str = Form(...), decision: str = Form(...), note: str = Form("")):
        if decision not in ("approve", "reject"):
            raise HTTPException(400, "bad decision")
        try:
            queue.decide(case_id, reviewer.strip(), decision, note.strip())
        except ApprovalError as e:
            return HTMLResponse(case(case_id, error=str(e)), status_code=400)
        return RedirectResponse(f"/case/{case_id}", status_code=303)

    @app.get("/audit", response_class=HTMLResponse)
    def audit_view() -> str:
        ok, why = audit.verify()
        rows = [f'<tr><td>{e["seq"]}</td><td>{_e(e["ts"][:19])}</td><td><a href="/case/{_e(e["case_id"])}">'
                f'{_e(e["case_id"])}</a></td><td>{_e(e["actor"])}</td><td>{_e(e["event"])}</td>'
                f'<td><code>{e["prev_hash"][:8]}&rarr;{e["hash"][:8]}</code></td></tr>' for e in audit.events()]
        body = (f'<p><a href="/">&larr; Queue</a></p><h1>Audit log</h1>'
                f'<p class="{"ok" if ok else "error"}">Hash chain: {"verified" if ok else _e(why)}</p>'
                f'<table><tr><th>#</th><th>When</th><th>Case</th><th>Who</th><th>Event</th><th>Chain</th></tr>'
                f'{"".join(rows)}</table>')
        return _page("Audit log", body)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "cases": len(proposals), "audit_verified": audit.verify()[0]}

    app.state.queue = queue
    app.state.audit = audit
    return app


def _e(s: object) -> str:
    return html.escape(str(s), quote=True)


def _fmt(v: object) -> str:
    if v is None:
        return '<span class="muted">(missing)</span>'
    if isinstance(v, dict):
        return "<br>".join(f"{_e(k)}: {_e(x)}" for k, x in v.items() if x)
    return f"<code>{_e(v)}</code>"


CSS = """
:root{--bg:#f7f7f5;--fg:#1b1b1b;--muted:#6b6b6b;--card:#fff;--line:#e4e2dc;--accent:#0b5cad;
--repair:#1f7a4d;--outreach:#a15c00;--escalate:#b42318;--recall:#5b3db5}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#ececec;--muted:#9a9a9a;--card:#1d1d1d;--line:#2e2e2e;--accent:#6aa7ec}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:920px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:8px 0}h2{font-size:15px;margin:28px 0 8px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
a{color:var(--accent);text-decoration:none}.muted{color:var(--muted)}.lead{font-size:16px}
.list{display:flex;flex-direction:column;gap:8px;margin-top:16px}
.row{display:grid;grid-template-columns:150px 56px 170px 80px 1fr;gap:12px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;color:var(--fg)}
.row:hover{border-color:var(--accent)}.cid{font-weight:600;font-family:ui-monospace,monospace;font-size:13px}.code{font-family:ui-monospace,monospace}
.sum{color:var(--muted);font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pill{display:inline-block;font-size:12px;font-weight:600;padding:2px 10px;border-radius:99px;color:#fff;vertical-align:middle}
.pill.repair{background:var(--repair)}.pill.outreach{background:var(--outreach)}.pill.escalate{background:var(--escalate)}.pill.recall{background:var(--recall)}
.status{font-size:12px;font-weight:600}.status.pending{color:var(--outreach)}.status.approved{color:var(--repair)}.status.rejected{color:var(--escalate)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top;font-size:14px}th{font-size:12px;color:var(--muted)}
td.before{background:rgba(180,35,24,.07)}td.after{background:rgba(31,122,77,.09)}
pre{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;overflow:auto;font-size:12px}pre.msg{white-space:pre-wrap;font-size:14px;font-family:inherit}
.decide{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;display:grid;gap:12px}
.decide label{display:grid;gap:4px;font-size:13px;color:var(--muted)}.decide input{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg)}
.btns{display:flex;gap:8px}button{font:inherit;font-weight:600;border:0;border-radius:8px;padding:10px 18px;color:#fff;cursor:pointer}
button.approve{background:var(--repair)}button.reject{background:var(--escalate)}
.error{background:rgba(180,35,24,.1);border:1px solid var(--escalate);color:var(--escalate);padding:10px 12px;border-radius:8px}.ok{color:var(--repair)}
@media (max-width:700px){.row{grid-template-columns:1fr auto;}.row .sum,.row .code{display:none}}
"""


def _page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{_e(title)} · Exception Copilot</title><style>{CSS}</style></head>"
            f"<body><main>{body}<p class=\"muted\" style=\"margin-top:40px;font-size:12px\">"
            f"Synthetic data only. Nothing on this screen sends a payment.</p></main></body></html>")

