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
from .validator import validate_message as _validate

server = MCPServer(
    name="iso20022",
    instructions=(
        "Tools for ISO 20022 payment messages (pacs.008.001.08, pacs.002.001.10). "
        "All tools are read-only. Use parse_message to read a message and "
        "validate_message to find defects mapped to likely ISO reason codes."
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


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
