"""MCP server for the battlefield: read tools + allowlisted actions.

Any MCP client (this agent, Claude Code, an IDE) can investigate the system
through it. Run standalone with:  python mcp_server.py   (stdio transport)
"""

from mcp.server.fastmcp import FastMCP

import battlefield as bf

mcp = FastMCP("battlefield")

for fn in bf.READ_TOOLS:
    mcp.tool()(fn)


@mcp.tool()
def evidence_digest() -> str:
    """Pre-computed triage: key facts plus ranked hypotheses with suggested actions."""
    import json
    return json.dumps(bf.digest())


@mcp.tool()
def execute_action(action: str, args: dict) -> str:
    """Execute one allowlisted remediation. Only call after a human approved it."""
    clean, err = bf.validate_action(action, args)
    if err:
        return "REJECTED: " + err
    return bf.execute_action(action, clean)


if __name__ == "__main__":
    mcp.run()
