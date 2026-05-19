"""Sonilo MCP server — exposes Sonilo's /v1/* API as MCP tools over stdio."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Sonilo")


def main() -> None:
    """Run the MCP server over stdio transport."""
    print("Starting Sonilo MCP server", flush=True)
    mcp.run()


if __name__ == "__main__":
    main()
