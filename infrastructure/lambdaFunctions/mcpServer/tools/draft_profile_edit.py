"""Stage a profile edit for operator approval (DESIGN.md section 7.7).

Profile edits touch an already-live account surface, so `publish_mode:
auto` does not cover them by default. Operator approval via
`admin.py approve-profile-edit --alias <alias>` is the only path that
writes an approval record, unless the account's
`auto_for_kinds` list includes `profile_edit`, in which case the server
records the approval here. Edits always re-run policy at publish time.

The tool parses a short, flat key=value string (one pair per line, or
comma-separated) so the smallest callable models can drive it. Only
known writable fields are accepted; everything else is rejected with a
specific error.
"""

from __future__ import annotations

import os

from sharedModules import approval, dynamo, policy, registry
from sharedModules.errors import AccountError, CapabilityError
from sharedModules.providers import get_provider
from sharedModules.providers.base import Capability

# Writable field names shared with sharedModules.approval._PROFILE_WRITABLE_FIELDS.
# Kept narrow so the model cannot smuggle unknown keys through to a provider.
_ALLOWED_FIELDS = frozenset({"display_name", "about", "description", "website", "phone", "emails"})


def draft_profile_edit(
    display_name: str = "",
    about: str = "",
    description: str = "",
    website: str = "",
    phone: str = "",
    emails: str = "",
    account: str = "",
) -> dict:
    """Stage a partial profile update.

    Only non-empty parameters are included in the patch, so pass just the
    fields you want to change. `emails` is a comma-separated list.

    - `account`: alias from `list_accounts`. Omit when only one account
      is configured.
    - `display_name`: Facebook ignores this (Page name changes require
      Meta's name-change review workflow); Bluesky applies it.
    - `about`, `description`: short/long bio. Facebook maps `about` to
      the Page's About line and `description` to the longer field;
      Bluesky uses `description`.
    - `website`: a single URL; if the account's policy sets
      `allowed_link_domains`, the host must be on that list.
    - `phone`: Facebook only (Instagram and Bluesky ignore it).
    - `emails`: comma-separated email addresses; Facebook joins them
      back with commas when posting.

    Returns `{ok, account, patch, content_hash, approval, next}`. The
    content_hash binds to the staged patch; a re-stage invalidates any
    pending approval. `approval` is 'auto' if the account auto-approves
    `profile_edit`, otherwise 'pending' with instructions.

    Fails with a specific error on an empty patch, an unknown provider
    capability, or a policy violation.
    """
    try:
        alias = registry.resolve_alias(account)
    except AccountError as e:
        return {"ok": False, "error": str(e)}

    provider = get_provider(alias)
    if Capability.PROFILE_EDIT not in provider.capabilities:
        return {
            "ok": False,
            "error": (
                f"provider '{provider.name}' does not support profile edits via API; "
                "edit the profile in the native app or management console"
            ),
        }

    entry = registry.configured_accounts()[alias]
    if not entry.get("profile_edits_enabled", False):
        return {
            "ok": False,
            "error": (
                f"account '{alias}' has profile_edits_enabled: false; "
                "enable it in config and redeploy to allow profile edits"
            ),
        }

    patch = _build_patch(
        display_name=display_name,
        about=about,
        description=description,
        website=website,
        phone=phone,
        emails=emails,
    )
    if not patch:
        return {
            "ok": False,
            "error": (
                "at least one of display_name, about, description, website, phone, "
                "or emails must be non-empty"
            ),
        }

    pol = entry.get("policy") or {}
    try:
        policy.enforce_profile_patch(patch, pol)
    except policy.PolicyError as e:
        return {"ok": False, "error": f"policy: {e}"}

    actor = os.environ.get("AGENT_IDENTITY", "mcp_client")
    try:
        dynamo.write_staged_profile_edit(alias, patch, actor)
    except CapabilityError as e:
        return {"ok": False, "error": str(e)}
    dynamo.delete_approval_record(f"profile:{alias}")

    mode = entry.get("publish_mode", "manual")
    auto_for_kinds = entry.get("auto_for_kinds") or []
    auto_approved = (mode == "auto") or (mode == "auto_for" and "profile_edit" in auto_for_kinds)
    if auto_approved:
        approval.record_profile_approval(alias, patch, approved_by="server (auto mode)")

    dynamo.append_audit(
        {
            "account": alias,
            "kind": "profile_edit",
            "actor_identity": actor,
            "result": "auto" if auto_approved else "staged",
        }
    )

    content_hash = approval.profile_hash(alias, patch)
    envelope = {
        "ok": True,
        "account": alias,
        "patch": patch,
        "content_hash": content_hash,
        "approval": "auto" if auto_approved else "pending",
    }
    if auto_approved:
        envelope["next"] = f"call publish_profile_edit(account='{alias}') to apply this change"
    else:
        envelope["next"] = (
            "operator must run `python scripts/admin.py approve-profile-edit "
            f"--alias {alias}` before the edit reaches the provider"
        )
    return envelope


def _build_patch(
    *,
    display_name: str,
    about: str,
    description: str,
    website: str,
    phone: str,
    emails: str,
) -> dict:
    """Narrow kwargs to a patch dict containing only truthy writable fields."""
    patch: dict = {}
    if display_name:
        patch["display_name"] = display_name
    if about:
        patch["about"] = about
    if description:
        patch["description"] = description
    if website:
        patch["website"] = website
    if phone:
        patch["phone"] = phone
    email_list = [e.strip() for e in (emails or "").split(",") if e.strip()]
    if email_list:
        patch["emails"] = email_list
    # Defense in depth: should be impossible from the kwargs, but makes the
    # invariant explicit for future callers.
    for key in list(patch):
        if key not in _ALLOWED_FIELDS:
            raise AssertionError(f"profile patch produced unknown key {key!r}")
    return patch
