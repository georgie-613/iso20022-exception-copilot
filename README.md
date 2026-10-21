# ISO 20022 Exception Copilot

An open-source ISO 20022 MCP server and an exception-triage copilot for cross-border payments: rejected payments, recall requests, and free-text beneficiary addresses that must become CBPR+ structured addresses. The copilot drafts a fix, a client message, a recall or a structured address; a human approves every action; every step is logged.

![Architecture: payment case to read-only MCP tools, copilot, Claude behind guardrails, proposal, human approval, hash-chained audit log](docs/architecture.png)

<sub>Diagram source: [`docs/architecture.mmd`](docs/architecture.mmd)</sub>

**What this proves.** You can put an LLM into a payments operations workflow without letting it touch money. The model never decides what is repairable and never edits a payment field: a policy table decides, the model only classifies client recall requests and words client messages, its output passes checks enforced in code, a named human approves every action, and a tamper-evident log records all of it. On 18 held-out cases written to break the keyword rules, Claude cut missed fraud from **4 of 5 to 0 of 5** with **zero false repairs**, and the two cases it got wrong went to a person, not out the door. The evals, misses and my own bugs included, are [published as-is](#before--after-rules-vs-model).

## Quickstart

```
pip install -e ".[ui]"                    # Python 3.12+
python -m gen.build --seed 42             # 63 synthetic cases
uvicorn ui.app:create_app --factory       # open http://127.0.0.1:8000
```

Set `ANTHROPIC_API_KEY` first to run Claude; without it the deterministic rules baseline runs and the app says so (`model: rules-baseline` in the queue header).

### The 3-minute demo

| Case | What happens | What to show |
|---|---|---|
| `DEMO-01-RC01` | Creditor agent BIC is wrong; the right one is in the bank's directory | One-field before/after diff, the validated repaired XML, approve it, the audit entry with its hash |
| `DEMO-05-AC01` | Account number rejected | The copilot does **not** guess a number: it drafts a client message. Try approving as `copilot`: refused (maker-checker) |
| `DEMO-11-FRAD` | Client asks to recall a payment their controller sent on a text from the CEO's "new mobile number". No fraud keywords | With Claude: escalated to a person with the fraud indicator, no recall drafted. With the rules baseline: a recall is drafted, the gap the model closes |
| `DEMO-12-ADDR` | Brazilian beneficiary address stored as free text | Brazil rules turn it into a CBPR+ structured address field by field, with the reason |
| `DEMO-13-ADDR` | Chilean address; no Chile rules | Rules send it to a person; with Claude, the model splits it and code verifies every value appears in the original |

**Synthetic data only.** No real bank messages, templates, BICs or customer data. Every BIC uses a test-BIC location code (second character `0`), so none can be a live production BIC.

## Status

| Piece | State |
|---|---|
| Seeded synthetic case generator (63 cases, XSD-validated) | ✅ Day 1 |
| MCP server: `parse_message`, `validate_message` | ✅ Day 2 |
| MCP server: `explain_reject`, `draft_repair` + guardrails | ✅ Day 3 |
| Copilot pipeline + maker-checker approval + hash-chained audit log | ✅ Day 4 (rules baseline) |
| Eval runner + CI gate | ✅ Day 4 |
| Approval screen (queue, diff, approve/reject, audit trail) | ✅ Day 6 |
| Demo polish: diagram, quickstart, 3-case demo story | ✅ PR 9 |
| LLM layer: Claude for recall classification + outreach wording, with fallback and audit | ✅ PR 7 · live: implicit fraud missed 4/5 → 0/5, all gates green |
| Held-out case set (18 cases the rules never saw) | ✅ Day 5 |
| Lane C: CBPR+ address structuring (country rules + model fallback + `structure_address` tool) | ✅ live: held-out 7/12 → 10/12 correct with Claude, 0 unsafe, all gates green |

## Scope (v1)

- **Rail:** cross-border correspondent payments (CBPR+-style), `pacs.008.001.08` with `pacs.002.001.10` status reports.
- **Lane A — rejects (14 reason codes):** RC01, CH16, DT01, RR02, RR03, AC01, AC04, BE01, AM04, AM05, AC06, AG01, RR04, MS03.
- **Lane B — recalls (4 reasons):** DUPL, CUST, TECH, FRAD — candidates for a `camt.056` recall.
- **Lane C — addresses (pre-send):** free-text creditor addresses turned into CBPR+ structured or hybrid addresses. See [Address structuring](#lane-c-cbpr-address-structuring).

### The repair boundary

The copilot may only draft **formatting or enrichment** fixes from data the bank already holds. Everything else goes to a person:

| Action | When | Examples |
|---|---|---|
| `repair` | Intended value is unambiguous or in our own master data | RC01 (a keying error: 1–2 characters off the BIC of the bank named on the message, same country), CH16, DT01, RR02 (debtor address from KYC master) |
| `outreach` | Only the client knows the truth — never guess | AC01, AC04, BE01, RR03, AM04 |
| `escalate` | Compliance, fraud, unspecified or judgment calls — always human | RR04, MS03, AC06, AG01, AM05, FRAD |
| `recall` | Client-requested recall, drafted for approval | DUPL, CUST, TECH |

AC01 is deliberately `outreach` even when a digit transposition looks obvious: guessing an account number is how money goes to the wrong person.

The same rule applies to bank codes. An RC01 is repaired only when the bad BIC is a keying error of the bank named on the message; a BIC that looks nothing like it means the client probably picked the wrong bank, so the copilot asks instead of swapping in a different bank. Every repair says why on the approval screen.

## The case set

```
python -m gen.build --seed 42 --out cases
```

Writes 63 cases: 50 for evaluation (40 Lane A, 10 Lane B, every code covered) and 13 demo cases that are never scored (2 of them Lane C addresses). Each case folder has:

- `pacs008.xml` — the original payment (passes the XSD, except CH16 cases by design)
- `pacs002.xml` — the status report (`RJCT` + reason code, or `ACSC` for recalls)
- `label.json` — the answer key: expected action, fields the repair may touch, expected value
- `request.txt` — Lane B only: the client's recall request

Output is byte-identical for a given seed.

## The MCP server

Five tools, all read-only (annotated `readOnlyHint`; none of them change or send anything):

| Tool | Does |
|---|---|
| `parse_message` | Reads a pacs.008 or pacs.002 into structured fields. Works on schema-invalid messages, so a CH16 reject is still explainable. |
| `validate_message` | XSD check plus business rules: BIC directory, business day, required debtor/creditor address, IBAN mod-97. Every finding carries `reason_code_hint`, the ISO reason code it would most likely trigger. |
| `explain_reject` | Explains a pacs.002 reject in plain language and classifies it under the reason-code policy: `repair`, `outreach` or `escalate`. With the original pacs.008 attached, it adds evidence. |
| `draft_repair` | For RC01, CH16, DT01, RR02 only: returns `DRAFT_PENDING_APPROVAL` with a field-level diff and the repaired XML, or `REFUSED` with where to route it. |
| `structure_address` | Turns a free-text creditor address into a CBPR+ structured or hybrid address with country rules: `STRUCTURED` / `HYBRID` with a field-level diff and the drafted XML, or `NEEDS_REVIEW` with the reason. Never invents text, never guesses a town or a country. |

### Guardrails, enforced in code

The policy table in `iso20022_mcp/policy.py` decides what is repairable, not the model. `draft_repair` then enforces:

1. **Policy gate**: only codes marked `repair` can be drafted. Unknown codes escalate.
2. **Evidence gate**: the defect the reject code points at must be visible in the message. No evidence, no repair.
3. **Allowlist gate**: after the edit, the diff is checked; if any field outside the code's allowlist moved, the draft is discarded.
4. **Validation gate**: the repaired message must pass validation.
5. **Draft only**: nothing is sent. Every draft needs a human.

Against the Lane A and B cases, `explain_reject` matches the answer key on every case and `draft_repair` produces the expected value on every repairable case. That is the deterministic baseline; the agent layer is measured against it.

A rejected payment that validates **clean** is a signal too: the cause is outside the message (closed account, funds, compliance), so it goes to a person.

Run it over stdio:

```
python -m iso20022_mcp.server
```

Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "iso20022": {
      "command": "python",
      "args": ["-m", "iso20022_mcp.server"],
      "cwd": "/path/to/iso20022-exception-copilot"
    }
  }
}
```

The BIC directory is the synthetic one from the generator, behind `iso20022_mcp/refdata.py`, the one adapter to swap for a real directory feed.

## The copilot

`copilot/agent.py` turns one case into one proposal for a human:

- **Lane A rejects:** `explain_reject` → policy action → `draft_repair` (repair), a drafted client message (outreach), or escalate.
- **Lane B recalls:** classify the client's free-text request → draft camt.056 fields, or escalate.
- **Lane C addresses:** `structure_address` country rules → if they can't place it, the model splits it → the same checks → draft a CBPR+ address, or a person decides.

The model's job is narrow: classify recall requests, word client messages, and split addresses the rules can't handle. It never decides what is repairable and never edits a payment field. **Any fraud signal in a recall request escalates, whatever the model says**; a test plugs in a model that calls every request routine and checks fraud still reaches a person.

### The model layer (`copilot/llm.py`)

Claude does three jobs and nothing else: classify the client's recall request (DUPL / CUST / TECH / FRAD, in English, Spanish or Portuguese), word the client outreach message, and split a free-text address the country rules couldn't handle into fields (it never picks the country). Controls, all enforced in `copilot/agent.py` rather than in the prompt:

| Control | What happens |
|---|---|
| Structured output | Responses are schema-constrained Pydantic models (`RecallClassification`, `OutreachDraft`), not free text |
| Fraud override | A fraud keyword, a `FRAD` reason, or **any** fraud indicator from the model escalates |
| Confidence floor | Below `COPILOT_MIN_CONFIDENCE` (default 0.7) the case goes to a person |
| Outreach fact check | The message must carry the exact reference and amount, may not introduce account-like numbers, and may not ask for credentials; otherwise the rules template is used |
| Fallback | No key, an API error, a refusal or a failed check means the rules baseline answers, and the proposal says so |
| Audit | Every call logs model, prompt SHA-256, latency, output, whether it fell back and why (API error vs guardrail), and the blocked output when a check rejects one |
| Client text is data | The request is wrapped in `<client_request>` tags; the system prompt says to ignore instructions inside it, and the fraud override doesn't depend on the model obeying |

Self-reported confidence is a weak signal on its own, so it's one control of several, not the safety mechanism.

```
pip install -e ".[llm]"
export ANTHROPIC_API_KEY=...            # optional; without it the rules baseline runs
export COPILOT_MODEL=claude-sonnet-5-5  # default; "rules" forces the baseline
```

CI never calls the API: the layer is tested against a fake client.

`copilot/approval.py` is the gate: the copilot is the maker, a named human the checker, and the two can never be the same identity. A rejection needs a reason.

`copilot/audit.py` is append-only and hash-chained: each event stores the previous event's hash, so editing or deleting any past line fails `verify()`. The `proposed` event pins the SHA-256 of the drafted XML, so what was approved is exactly what was proposed.

## Approval screen

```
pip install -e ".[ui]"
python -m gen.build --seed 42
uvicorn ui.app:create_app --factory      # http://127.0.0.1:8000
```

- **Queue:** every demo case with the copilot's proposal and its status.
- **Case page:** the field-level before/after diff for repairs, the drafted client message for outreach, the camt.056 fields for recalls, the evidence, and the case's audit trail with hashes.
- **Decision:** reviewer name plus approve or reject. The copilot's identity is refused (maker-checker), a rejection needs a note, and every decision is written to the hash-chained log.
- **Restart-safe:** proposals are pinned in the audit log and restored verbatim, so a reviewer always sees exactly what was proposed, never a regenerated model output. Only cases the log has never seen are triaged.

No login in v1: the reviewer types their name. A real deployment puts SSO in front and takes the identity from it.

## Evals

```
python -m evals.run --cases cases
```

| Metric | Gated in CI |
|---|---|
| False repairs (repair proposed where the label isn't, or wrong value) | **yes, must be 0** |
| Zero-miss: RR04, MS03, FRAD always escalate | **yes, must be 0** |
| Action accuracy, routing precision, recall-reason accuracy | reported only (1 case = 2 points; too noisy to gate) |

The rules baseline scores 100% on the current set. **Read that as plumbing, not intelligence**: the generator and the rules were written together, so the set can't surprise them.

### Held-out set

```
python -m gen.holdout --seed 42 --out cases_holdout
python -m evals.run --cases cases_holdout --set holdout --report-only
```

18 cases written *against* the rules: recall requests with none of the baseline's keywords, Spanish and Portuguese requests from LATAM clients, fraud described without the word "fraud", and reject edge cases where the obvious repair is wrong.

| Rules baseline on held-out | Result |
|---|---|
| Reject edge cases (bad BIC with no agent name, reject code with no visible defect, unknown debtor, CH16 on a client reference, unknown code) | **6/6 escalated**: the guardrails generalize |
| False repairs | **0** |
| Implicit fraud (BEC, lookalike domain, vendor callback, Portuguese) | **4 of 5 missed** |
| Recall reason accuracy | 25% |

### Before / after: rules vs model

```
python -m evals.compare --cases cases_holdout --set holdout   # needs ANTHROPIC_API_KEY
```

Runs both on the same cases and writes a side-by-side table plus a failure analysis of every model miss, with the model's own rationale and fraud indicators. It exits non-zero if the model run breaks a hard gate (false repairs, zero-miss) or if model calls **failed** (API error, refusal), because then the numbers are really the rules baseline's. A model output **blocked by a guardrail** is different: the control worked, so it is reported, with the blocked text, but doesn't fail the run. In GitHub: **Actions → live-eval → Run workflow** (uses the `ANTHROPIC_API_KEY` repo secret; regular CI never calls the API).

**Live results** (`claude-sonnet-5-5`, run 3 on 2026-10-20, [full report](evals/results/live-eval-2026-10-20-run-39360520595.md), published as-is). **All hard gates pass on both sets, with no failed model calls and no outputs blocked by guardrails.**

| Held-out set (18 cases the rules never saw) | Rules baseline | Claude |
|---|---|---|
| **Fraud missed** (implicit: BEC, lookalike domain, vendor callback, Portuguese) | **4 of 5** | **0 of 5** |
| False repairs | 0 | 0 |
| Recall reason accuracy | 25% | 83% |
| Action accuracy | 78% | 89% |
| Routing precision | 100% | 85% |
| Failed model calls / outputs blocked | – | 0 / 0 |
| Hard gates (false repairs = 0, zero-miss = 0, no failed model calls) | failed | **passed** |

**Golden set (50):** identical to the baseline on every metric (100% action and reason accuracy, 0 false repairs, 0 zero-miss), with 0 failed calls and 0 blocked outputs.

**What the model got wrong (2 of 18)**, both in the safe direction: two technical-error recalls went to a person instead of getting a drafted recall. The same two cases, with the same reasons and confidence, came up in runs 1 and 3.
- *H-B-TECH-GLITCH*: "released before it was approved internally". The model read an unapproved release as possibly unauthorized and chose FRAD (confidence 0.6). The prompt says "when in doubt, choose FRAD", and it did.
- *H-B-TECH-H2H*: a host-to-host resend after a timeout. Classed DUPL at 0.6 confidence, below the 0.7 floor, so it went to a person.

That is why routing precision drops to 85%: over-escalation costs analyst minutes, a missed fraud costs the money. The prompt was **not** changed after seeing these results; any change gets a held-out v2.

**How we got here (earlier runs kept as-is):**
- Run 1 ([report](evals/results/live-eval-2026-10-20-run-39357904969.md)): same held-out numbers. Golden was marked failed because the outreach check rejected one model message; under the old gate any fallback failed the run. The gate now fails only on API failures; a guardrail block is reported, with the blocked text.
- Run 2 ([report](evals/results/live-eval-2026-10-20-run-39358916870.md)) found two bugs of mine. The credential check blocked three *good* messages that warned clients **not** to share passwords; it now blocks requests and lets warnings through. And one held-out classification was cut off mid-JSON because the 512-token budget is shared with the model's thinking; it is now 4096. Run 3 is the first run after both fixes.

The strict `xfail` stays: it asserts the *rules baseline* catches implicit fraud, and the rules still don't. The gap is closed by the model layer, measured here, not by the rules.

The held-out set is not used to tune prompts; if a prompt changes after seeing it, a held-out v2 is written and reported instead.

The fraud misses are the known gap, tracked as a strict `xfail` test, and it is the model layer's job to close it. Even today a missed fraud case lands as a *recall draft awaiting human approval*, not an action; the zero-miss gate exists so the model can't ship until a person never has to catch it.

## Lane C: CBPR+ address structuring

Most banks store beneficiary addresses as free text, like `"Rua Augusta, 1500 - Consolacao / Sao Paulo - SP / CEP 01304-001 / BRASIL"`. Swift's CBPR+ is moving cross-border payments to **hybrid** addresses (Town Name and Country as separate elements, at most 2 address lines of 70 characters) or **fully structured** ones (no address lines). The hybrid format has been live since November 2025. Swift has since **postponed** retiring fully unstructured addresses, which can continue until at least November 2027, while some banks, J.P. Morgan among them, are keeping their own 14 November 2026 date ([J.P. Morgan](https://www.jpmorgan.com/insights/payments/cross-border-payments/iso-20022-migration)). Either way, the backlog of free-text addresses has to be cleaned, one country format at a time.

```
python -m gen.addresses --seed 42 --out cases_address                       # 24 cases
python -m gen.addresses --seed 42 --out cases_address_holdout --holdout     # 13 hand-written cases
python -m evals.address --cases cases_address --set address                 # CI gate: unsafe = 0
```

**How a draft is made, in order:**
1. **Country** from the country line, the creditor's IBAN and the creditor agent's BIC. If they disagree, a person decides. A wrong country breaks sanctions screening, so it is never guessed.
2. **Country rules** (MX, BR, CO, AR, GB, DE) place street, building number, postcode, district (colonia, bairro), town and state. What they can't place stays as an address line: a **hybrid** address.
3. If the rules can't find a town, or have no rules for the country, **the model** splits the address. It is never asked when the country is in dispute.
4. **The same checks run on the rules' draft and the model's:** every value must appear in the original address (no invented, expanded or translated text), the result must be valid CBPR+, the postcode must match the country's format, the country must match the evidence, the drafted message must still pass the XSD and only the creditor address may change. A failed check means a person decides. The blocked output is logged.
5. A named person approves the draft on the same approval screen, with the field-by-field diff and the reason.

**Live results** (`claude-sonnet-5-5`, run 4 on 2026-10-21, [full report](evals/results/live-eval-2026-10-21-run-39604108791.md), published as-is). All hard gates pass, with no failed model calls:

| Set | Correct drafts: rules → with Claude | Unsafe | Sent to a person |
|---|---|---|---|
| Address set (24) | 21 → **22 of 22** | 0 → **0** | 3 → 2 (conflict; no town) |
| Held-out (13, hand-written) | 7 → **10 of 12** | 0 → **0** | 6 → 2 (conflict; Costa Rica) |

Unsafe means a draft with the wrong town or country, or a draft where a person must decide. Claude structured Chile, Peru and a Mexican address with the postcode after the town, all of which the rules sent to a person. What it got wrong, published as-is:
- *H-C-CO-ABBR*: building number drafted as `# 71-21` instead of `71-21`. The text is in the original, so the invented-text check doesn't catch it; town and country are right. A person would fix it at approval.
- *H-C-CR-SENAS* (Costa Rica, "Del Parque Central de Heredia 300 metros sur"): the model's draft was **blocked** because the town "Heredia" also appears inside the directions line. CBPR+ forbids repeating structured data in address lines, but this is a landmark name, not a repeat, so the check is stricter than the rule. Safe direction, still a person. Not changed after seeing the held-out set; a fix would get a held-out v2.
- *C-NO-TOWN*: the model's draft was **blocked** (no town). The guardrail did its job.

The held-out cases use formats the rules weren't written for: reordered lines, abbreviations, umlauts, a missing country line, and Costa Rica's directions-style addresses. They were written by the same author as the rules, so treat them as a stress test, not an independent benchmark.

## Develop

```
pip install -e ".[dev]"
python -m pytest
```

Schemas in `schemas/` are the public ISO 20022 XSDs (`pacs.008.001.08`, `pacs.002.001.10`), sourced from [moov-io/fedwire20022](https://github.com/moov-io/fedwire20022) (Apache-2.0).

## License

MIT
