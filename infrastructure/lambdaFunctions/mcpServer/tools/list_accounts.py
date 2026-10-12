import os

from sharedModules import registry
from sharedModules.errors import AccountError
from sharedModules.providers import get_provider


def list_accounts() -> dict:
    """List the social accounts you can use.

    Use this first to learn the account names for the other tools. Each
    entry shows the provider (facebook or instagram), the display name, the
    publish mode for that account (manual = every post needs human approval
    via the admin CLI; auto = you can publish without approval; auto_for =
    some kinds auto, others manual), whether publishing is enabled on that
    account at all, and the capabilities the provider supports (text, link,
    image, multi_image, video, reel, schedule, edit, delete, comments,
    insights).

    This is the complete set available to you: this connection may be pinned
    to a subset of the accounts the server hosts, and anything not listed
    here cannot be reached, so do not guess or retry other names.
    """
    try:
        rows: list[dict] = []
        for alias in sorted(registry.configured_accounts()):
            record = registry.account_record(alias)
            try:
                provider = get_provider(alias)
                capabilities = sorted(c.value for c in provider.capabilities)
            except AccountError:
                capabilities = []
            rows.append(
                {
                    "account": alias,
                    "provider": record["provider"],
                    "display_name": record.get("display_name", ""),
                    "status": record["status"],
                    "publish_enabled": bool(record.get("publish_enabled", False)),
                    "publish_mode": record.get("publish_mode", "manual"),
                    "auto_for_kinds": list(record.get("auto_for_kinds", [])),
                    "profile_edits_enabled": bool(record.get("profile_edits_enabled", False)),
                    "scopes": record["scopes"],
                    "capabilities": capabilities,
                }
            )
    except AccountError as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "accounts": rows,
        "publishing_enabled": os.environ.get("PUBLISHING_ENABLED", "false") == "true",
    }
