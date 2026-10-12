"""MCP tool registration.

One module per tool. Descriptions are written for the weakest caller (8B
local models per DESIGN.md section 8): imperative, explicit about when to
use the tool, flat string/int parameters, defaults for everything optional.
"""

from mcp.server.fastmcp import FastMCP

from . import (
    check_comments,
    delete_post,
    discard_draft,
    draft_post,
    edit_post,
    get_draft,
    get_insights,
    get_post,
    hide_comment,
    list_accounts,
    list_comments,
    list_drafts,
    list_posts,
    publish_draft,
    reply_to_comment,
    schedule_draft,
    stage_media,
    update_draft,
    wait_for_comments,
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
    # comments + edit + delete (M3)
    mcp.tool()(edit_post.edit_post)
    mcp.tool()(delete_post.delete_post)
    mcp.tool()(reply_to_comment.reply_to_comment)
    mcp.tool()(hide_comment.hide_comment)
    # media staging + instagram publishing (M5)
    mcp.tool()(stage_media.stage_media)
    # comment webhooks + long-poll (M7)
    mcp.tool()(check_comments.check_comments)
    mcp.tool()(wait_for_comments.wait_for_comments)
