"""A minimal MCP server for Launchpad's "MCP server" deploy (byoc, protocol=mcp).

AgentCore Runtime's MCP contract: streamable HTTP on 0.0.0.0:8000/mcp. Launchpad
puts the runtime behind the workspace Gateway, so agents call these tools as
``<target name>___lookup_order`` / ``<target name>___refund_policy``.
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(host="0.0.0.0", stateless_http=True)

ORDERS = {
    "A-1001": {"status": "shipped", "carrier": "SF Express", "eta": "2026-10-16"},
    "A-1002": {"status": "processing", "carrier": None, "eta": "2026-10-18"},
}


@mcp.tool()
def lookup_order(order_id: str) -> dict:
    """Look up one order's status, carrier and estimated delivery date by order id."""
    order = ORDERS.get(order_id.strip().upper())
    if order is None:
        return {"order_id": order_id, "found": False}
    return {"order_id": order_id, "found": True, **order}


@mcp.tool()
def refund_policy(days_since_delivery: int) -> str:
    """Whether an order delivered this many days ago can still be returned for a refund."""
    if days_since_delivery <= 7:
        return "Eligible: full refund within 7 days of delivery (policy R-01)."
    if days_since_delivery <= 30:
        return "Eligible for exchange only, 8-30 days after delivery (policy R-02)."
    return "Not eligible: more than 30 days since delivery (policy R-03)."


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
