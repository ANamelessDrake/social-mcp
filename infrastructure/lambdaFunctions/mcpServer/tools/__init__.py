"""MCP tool registration.

One module per tool. Descriptions are written for the weakest caller (8B
local models per DESIGN.md section 8): imperative, explicit about when to
use the tool, flat string/int parameters, defaults for everything optional.
"""

from mcp.server.fastmcp import FastMCP

from . import get_insights, get_post, list_accounts, list_comments, list_posts


def register_all(mcp: FastMCP) -> None:
    mcp.tool()(list_accounts.list_accounts)
    mcp.tool()(list_posts.list_posts)
    mcp.tool()(get_post.get_post)
    mcp.tool()(list_comments.list_comments)
    mcp.tool()(get_insights.get_insights)
