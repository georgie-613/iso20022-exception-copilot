"""ISO 20022 MCP server.

    python -m iso20022_mcp.server      # stdio transport

Tools are read-only: they parse and check messages, they never change or send
anything. Actions (repairs, recalls) are drafted elsewhere and need a human.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .parser import ParseError, parse
from .triage import draft_repair as _draft_repair
from .triage import explain_reject as _explain_reject
from .validator import validate_message as _validate

server = MCPServer(
    name="iso20022",
    instructions=(
        "Tools for ISO 20022 payment messages (pacs.008.001.08, pacs.002.001.10). "
        "All tools are read-only. Use parse_message to read a message and "
        "validate_message to find defects mapped to likely ISO reason codes, "
        "explain_reject to classify a pacs.002 reject under the reason-code policy, and "
        "draft_repair to propose a fix for a repairable reject. draft_repair only ever returns "
        "a DRAFT for human approval; it never sends anything."
    ),
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                            openWorldHint=False)


@server.tool(annotations=READ_ONLY)
def parse_message(xml: str) -> dict[str, Any]:
    """Parse a pacs.008 or pacs.002 XML message into structured fields.

    Works on schema-invalid messages too. Returns an `error` key instead of
    raising if the input is not a supported, well-formed ISO 20022 message.
    """
    try:
        return parse(xml)
    except ParseError as e:
        return {"error": str(e)}


@server.tool(annotations=READ_ONLY)
def validate_message(xml: str) -> dict[str, Any]:
    """Validate a pacs.008 or pacs.002 message against the ISO 20022 XSD and
    business rules (BIC directory, business day, required addresses, IBAN check).

    Each finding carries `reason_code_hint`, the ISO reason code it would most
    likely cause. No findings on a rejected payment means the cause is outside
    the message (closed account, funds, compliance) and needs a human.
    """
    return _validate(xml)


@server.tool(annotations=READ_ONLY)
def explain_reject(pacs002_xml: str, pacs008_xml: str | None = None) -> dict[str, Any]:
    """Explain a pacs.002 status report in plain language and classify it under the
    reason-code policy: repair, outreach (client must supply the truth) or escalate
    (always human). Pass the original pacs.008 to attach evidence: a code the
    policy calls repairable is escalated if its defect is not visible in the message.
    """
    return _explain_reject(pacs002_xml, pacs008_xml)


@server.tool(annotations=READ_ONLY)
def draft_repair(pacs008_xml: str, pacs002_xml: str) -> dict[str, Any]:
    """Draft a repaired pacs.008 for a repairable reject (RC01, CH16, DT01, RR02).

    Returns status DRAFT_PENDING_APPROVAL with a field-level diff and the repaired
    XML, or REFUSED with the reason and where to route it. Enforced in code: only
    policy-allowed fields may change, the defect must be evidenced, and the draft
    must pass validation. Nothing is sent; a human approves every draft.
    """
    return _draft_repair(pacs008_xml, pacs002_xml)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
