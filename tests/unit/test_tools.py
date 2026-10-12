"""Smoke test: the FastMCP app's tool/list carries every registered name.

Catches a `register_all` regression the moment any tool stops being
registered, without exercising each tool individually.
"""

import json
import os

import pytest

M1_TOOLS = {
    "list_accounts",
    "list_posts",
    "get_post",
    "list_comments",
    "get_insights",
}
M2_TOOLS = {
    "draft_post",
    "get_draft",
    "list_drafts",
    "update_draft",
    "discard_draft",
    "publish_draft",
    "schedule_draft",
}
M3_TOOLS = {
    "edit_post",
    "delete_post",
    "reply_to_comment",
    "hide_comment",
}
M5_TOOLS = {
    "stage_media",
}
M7_TOOLS = {
    "check_comments",
    "wait_for_comments",
}
EXPECTED = M1_TOOLS | M2_TOOLS | M3_TOOLS | M5_TOOLS | M7_TOOLS


@pytest.fixture
def app_module(monkeypatch):
    monkeypatch.setenv("DEV_BEARER_TOKEN", "test")
    monkeypatch.setenv("PUBLISHING_ENABLED", "false")
    os.environ.setdefault(
        "ACCOUNTS",
        json.dumps(
            [
                {
                    "alias": "main-page",
                    "provider": "facebook",
                    "publish_enabled": False,
                    "publish_mode": "manual",
                    "auto_for_kinds": [],
                    "policy": {},
                }
            ]
        ),
    )
    import importlib.util
    import sys
    from pathlib import Path

    # conftest adds mcpServer and publisher to sys.path, so `import app`
    # resolves to whichever landed first. Load the mcpServer one by
    # file path so this test is unambiguous either way.
    root = Path(__file__).resolve().parents[2]
    path = root / "infrastructure" / "lambdaFunctions" / "mcpServer" / "app.py"
    spec = importlib.util.spec_from_file_location("mcp_server_app", str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules["mcp_server_app"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_tools_list_covers_every_milestone(app_module):
    """FastMCP keeps the registry inside `_tool_manager`; walk it."""
    tool_manager = getattr(app_module.mcp, "_tool_manager", None)
    assert tool_manager is not None, "FastMCP internals shape changed"
    names = set(tool_manager._tools.keys()) if hasattr(tool_manager, "_tools") else set()
    # Fallback: FastMCP exposes list_tools() async; some versions store
    # names at `tool_manager.tools`.
    if not names and hasattr(tool_manager, "tools"):
        names = set(tool_manager.tools.keys())
    missing = EXPECTED - names
    assert not missing, f"missing registered tools: {sorted(missing)}"
