"""MCP tool registration.

One module per tool. Descriptions are written for the weakest caller (8B
local models per DESIGN.md section 8): imperative, explicit about when to
use the tool, flat string/int parameters, defaults for everything optional.
"""

from mcp.server.fastmcp import FastMCP

from . import (
    discard_draft,
    draft_post,
    get_draft,
    get_insights,
    get_post,
    list_accounts,
    list_comments,
    list_drafts,
    list_posts,
    publish_draft,
    schedule_draft,
    update_draft,
)


def register_all(mcp: FastMCP) -> None:
    # read ops (M1)
    mcp.tool()(list_accounts.list_accounts)
    mcp.tool()(list_posts.list_posts)
    mcp.tool()(get_post.get_post)
    mcp.tool()(list_comments.list_comments)
    mcp.tool()(get_insights.get_insights)
    # drafts + publishing (M2)
    mcp.tool()(draft_post.draft_post)
    mcp.tool()(get_draft.get_draft)
    mcp.tool()(list_drafts.list_drafts)
    mcp.tool()(update_draft.update_draft)
    mcp.tool()(discard_draft.discard_draft)
    mcp.tool()(publish_draft.publish_draft)
    mcp.tool()(schedule_draft.schedule_draft)
