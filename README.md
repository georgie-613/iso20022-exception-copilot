# ISO 20022 Exception Copilot

An open-source ISO 20022 MCP server and an exception-triage copilot for rejected cross-border payments. The copilot drafts a fix or a recall, a human approves every action, and every step is logged.

**Synthetic data only.** No real bank messages, templates, BICs or customer data. Every BIC uses a test-BIC location code (second character `0`), so none can be a live production BIC.

## Status

| Piece | State |
|---|---|
| Seeded synthetic case generator (60 cases, XSD-validated) | ✅ Day 1 |
| MCP server: `parse_message`, `validate_message` | ✅ Day 2 |
| MCP server: `explain_reject`, `draft_repair` | ⏳ next |
| Exception Copilot agent + human approval gate + audit log | ⏳ |
| Eval runner (accuracy, false-repair rate, routing precision) | ⏳ |

## Scope (v1)

- **Rail:** cross-border correspondent payments (CBPR+-style), `pacs.008.001.08` with `pacs.002.001.10` status reports.
- **Lane A — rejects (14 reason codes):** RC01, CH16, DT01, RR02, RR03, AC01, AC04, BE01, AM04, AM05, AC06, AG01, RR04, MS03.
- **Lane B — recalls (4 reasons):** DUPL, CUST, TECH, FRAD — candidates for a `camt.056` recall.

### The repair boundary

The copilot may only draft **formatting or enrichment** fixes from data the bank already holds. Everything else goes to a person:

| Action | When | Examples |
|---|---|---|
| `repair` | Intended value is unambiguous or in our own master data | RC01 (BIC recoverable from directory), CH16, DT01, RR02 (debtor address from KYC master) |
| `outreach` | Only the client knows the truth — never guess | AC01, AC04, BE01, RR03, AM04 |
| `escalate` | Compliance, fraud, unspecified or judgment calls — always human | RR04, MS03, AC06, AG01, AM05, FRAD |
| `recall` | Client-requested recall, drafted for approval | DUPL, CUST, TECH |

AC01 is deliberately `outreach` even when a digit transposition looks obvious: guessing an account number is how money goes to the wrong person.

## The case set

```
python -m gen.build --seed 42 --out cases
```

Writes 60 cases: 50 for evaluation (40 Lane A, 10 Lane B, every code covered) and 10 demo cases that are never scored. Each case folder has:

- `pacs008.xml` — the original payment (passes the XSD, except CH16 cases by design)
- `pacs002.xml` — the status report (`RJCT` + reason code, or `ACSC` for recalls)
- `label.json` — the answer key: expected action, fields the repair may touch, expected value
- `request.txt` — Lane B only: the client's recall request

Output is byte-identical for a given seed.

## The MCP server

Read-only tools (annotated `readOnlyHint`, never change or send anything):

| Tool | Does |
|---|---|
| `parse_message` | Reads a pacs.008 or pacs.002 into structured fields. Works on schema-invalid messages, so a CH16 reject is still explainable. |
| `validate_message` | XSD check plus business rules: BIC directory, business day, required debtor/creditor address, IBAN mod-97. Every finding carries `reason_code_hint`, the ISO reason code it would most likely trigger. |

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

## Develop

```
pip install -e ".[dev]"
python -m pytest
```

Schemas in `schemas/` are the public ISO 20022 XSDs (`pacs.008.001.08`, `pacs.002.001.10`), sourced from [moov-io/fedwire20022](https://github.com/moov-io/fedwire20022) (Apache-2.0).

## License

MIT
