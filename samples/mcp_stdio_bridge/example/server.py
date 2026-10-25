"""A stdio-only MCP server — the kind the bridge exists for.

``mcp.run()`` with no transport speaks stdio. Deployed through Launchpad's
"MCP server" wizard with **stdio server** on (command ``python``, args
``server.py``), the platform wraps it with ``mcp_stdio_bridge.py`` so the
runtime serves streamable HTTP on ``0.0.0.0:8000/mcp``.
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("unit-convert")


@mcp.tool()
def celsius_to_fahrenheit(celsius: float) -> float:
    """Convert a temperature from degrees Celsius to degrees Fahrenheit."""
    return celsius * 9 / 5 + 32


@mcp.tool()
def km_to_miles(km: float) -> float:
    """Convert a distance from kilometres to miles."""
    return km * 0.621371


if __name__ == "__main__":
    mcp.run()  # stdio
