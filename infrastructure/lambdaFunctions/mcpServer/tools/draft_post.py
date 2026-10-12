"""Create a draft (DESIGN.md section 7).

Drafting and publishing are separate tools. `draft_post` validates the
request against provider limits and the account's content policy, stores
the draft, and returns a preview with warnings; it never publishes on its
own, unless the account's `publish_mode` permits it AND `auto_publish=1`
was passed, in which case the server chains to `publish_draft` as a
convenience (one tool call, one answer).

Per-account `publish_mode` decides who creates the approval record:

- `manual` (default): the operator approves via `admin.py approve`.
- `auto`: the server records an implicit approval immediately after
  policy validation.
- `auto_for`: implicit approval when `kind` is in the account's
  `auto_for_kinds`, manual otherwise.
"""

from __future__ import annotations

import os
import secrets as pysecrets
from datetime import UTC, datetime

from sharedModules import approval, dynamo, policy, registry
from sharedModules.errors import AccountError, CapabilityError
from sharedModules.models import MediaRef
from sharedModules.providers import get_provider
from sharedModules.providers.base import Capability

from . import publish_draft as pd
from ._common import PUBLISH_SCOPE


def draft_post(
    kind: str,
    account: str = "",
    text: str = "",
    link: str = "",
    media: str = "",
    is_ai_generated: int = 0,
    scheduled_time: str = "",
    auto_publish: int = 0,
) -> dict:
    """Create a draft of a post.

    Parameters are flat strings and ints so small models can drive the
    tool reliably. The server stores the draft under `draft_id`, which
    you then pass to `publish_draft` (manual accounts) or that chains
    to publish on its own (auto accounts, with `auto_publish=1`).

    - `kind` (required): one of 'text', 'link', 'image', 'multi_image',
      'video', 'reel'. Which kinds a provider supports is in
      `list_accounts().capabilities`.
    - `account`: alias from `list_accounts`. Omit when only one is
      configured.
    - `text`: caption or message. Policy rules may reject some content
      (see `list_accounts` for the account's mode and the policy block).
    - `link`: only for `kind='link'`; must point at an allowed domain
      when the account's policy sets one.
    - `media`: comma-separated media references for photo / multi_image /
      video / reel drafts. Each ref is either an https URL (Facebook
      fetches it; Instagram also accepts it) or an `s3://` key you got
      back from `stage_media` for this same account.
    - `is_ai_generated`: 1 to tag the post for Meta's AI content flag
      (Instagram only; set once on carousels).
    - `scheduled_time`: ISO 8601 UTC. Pass this to pre-fill the schedule
      field on the draft; it only has effect once `schedule_draft` is
      called.
    - `auto_publish`: 1 to chain to `publish_draft` immediately. Only
      works when the account's `publish_mode` permits it (auto, or
      auto_for with a matching kind); on manual, returns an error that
      names the mode.

    Returns `{ok, draft_id, kind, preview, warnings, approval, next}` on
    success. `warnings` lists non-blocking policy notes; blocking rules
    error out before the draft is stored. `approval` is 'auto' or
    'pending'; `next` tells the agent what to do next.
    """
    try:
        alias = registry.resolve_alias(account)
    except AccountError as e:
        return {"ok": False, "error": str(e)}

    kind_norm = (kind or "").strip().lower()
    if not kind_norm:
        return {
            "ok": False,
            "error": "kind is required (text, link, image, multi_image, video, reel)",
        }

    entry = registry.configured_accounts()[alias]
    provider = get_provider(alias)
    cap = _capability_for_kind(kind_norm)
    if cap and cap not in provider.capabilities:
        return {
            "ok": False,
            "error": (
                f"provider '{provider.name}' does not support kind '{kind_norm}'; "
                f"available kinds: {_available_kinds(provider)}"
            ),
        }

    media_refs = _parse_media(media)
    try:
        _basic_shape_check(kind_norm, text, link, media_refs)
    except CapabilityError as e:
        return {"ok": False, "error": str(e)}
    try:
        _check_s3_namespacing(alias, media_refs)
    except CapabilityError as e:
        return {"ok": False, "error": str(e)}
    limit = provider.limits
    if limit.max_text_length and len(text) > limit.max_text_length:
        return {
            "ok": False,
            "error": (
                f"text is {len(text)} characters; {provider.name} caps at "
                f"{limit.max_text_length}"
            ),
        }
    if limit.max_media_per_post and len(media_refs) > limit.max_media_per_post:
        return {
            "ok": False,
            "error": (
                f"{len(media_refs)} media items; {provider.name} caps at "
                f"{limit.max_media_per_post}"
            ),
        }

    draft_id = _new_draft_id()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    draft_dict = {
        "draft_id": draft_id,
        "account": alias,
        "kind": kind_norm,
        "text": text or "",
        "link": link or "",
        "media": [
            {"source": m.source, "key": m.key, "mime": m.mime, "etag": m.etag}
            for m in media_refs
        ],
        "schedule": scheduled_time or "",
        "is_ai_generated": bool(int(is_ai_generated or 0)),
        "status": "drafted",
        "created_at": now,
        "created_by": os.environ.get("AGENT_IDENTITY", "mcp_client"),
    }

    # Policy enforcement (DESIGN.md section 7.5)
    pol = entry.get("policy") or {}
    try:
        policy.enforce(_as_ns(draft_dict), pol)
    except policy.PolicyError as e:
        return {"ok": False, "error": f"policy: {e}"}
    draft_dict["policy_warnings"] = policy.warnings_from(policy.validate(_as_ns(draft_dict), pol))

    # publish_mode drives who writes the approval record (DESIGN.md section 7.4)
    mode = entry.get("publish_mode", "manual")
    auto_for_kinds = entry.get("auto_for_kinds") or []
    auto_approved = (mode == "auto") or (mode == "auto_for" and kind_norm in auto_for_kinds)

    want_publish = bool(int(auto_publish or 0))
    if want_publish and not auto_approved:
        return {
            "ok": False,
            "error": (
                f"account '{alias}' has publish_mode='{mode}'; "
                "auto_publish=1 is not allowed. Store the draft without "
                "auto_publish and run `python scripts/admin.py approve "
                f"--draft-id <id>` to approve, or flip the account to "
                "publish_mode='auto' in config and redeploy."
            ),
        }

    if auto_approved:
        draft_dict["status"] = "approved"
    dynamo.write_draft(draft_dict)

    if auto_approved:
        approval.record_approval(
            draft_id=draft_id,
            scope=PUBLISH_SCOPE,
            approved_by="server (auto mode)",
        )
        dynamo.append_audit(
            {
                "account": alias,
                "kind": "approve",
                "actor_identity": draft_dict["created_by"],
                "draft_id": draft_id,
                "result": "auto",
            }
        )

    dynamo.append_audit(
        {
            "account": alias,
            "kind": "draft",
            "actor_identity": draft_dict["created_by"],
            "draft_id": draft_id,
            "result": "ok",
        }
    )

    envelope = {
        "ok": True,
        "draft_id": draft_id,
        "account": alias,
        "kind": kind_norm,
        "preview": _preview(draft_dict),
        "warnings": draft_dict["policy_warnings"],
        "approval": "auto" if auto_approved else "pending",
        "publish_mode": mode,
        "next": (
            "call publish_draft or set auto_publish=1"
            if auto_approved
            else "operator must run `python scripts/admin.py approve --draft-id "
            + draft_id
            + "` before publish_draft will proceed"
        ),
    }

    if want_publish and auto_approved:
        pub = pd.publish_draft(draft_id=draft_id)
        envelope["publish"] = pub

    return envelope


# ---------- helpers ----------


_KIND_TO_CAP = {
    "text": Capability.TEXT,
    "link": Capability.LINK,
    "image": Capability.IMAGE,
    "multi_image": Capability.MULTI_IMAGE,
    "video": Capability.VIDEO,
    "reel": Capability.REEL,
}


def _capability_for_kind(kind: str) -> Capability | None:
    return _KIND_TO_CAP.get(kind)


def _available_kinds(provider) -> str:
    return ", ".join(sorted(k for k, cap in _KIND_TO_CAP.items() if cap in provider.capabilities))


def _parse_media(raw: str) -> list[MediaRef]:
    if not raw:
        return []
    out: list[MediaRef] = []
    for item in [s.strip() for s in raw.split(",") if s.strip()]:
        source = "s3" if item.startswith("s3://") else "url"
        out.append(MediaRef(source=source, key=item, mime="", etag=""))
    return out


def _basic_shape_check(kind: str, text: str, link: str, media_refs: list[MediaRef]) -> None:
    if kind == "text" and not text:
        raise CapabilityError("text kind requires a non-empty 'text' parameter")
    if kind == "link" and not link:
        raise CapabilityError("link kind requires the 'link' parameter")
    if kind in {"image", "video", "reel"} and not media_refs:
        raise CapabilityError(f"{kind} kind requires at least one media URL in 'media'")
    if kind == "multi_image" and len(media_refs) < 2:
        raise CapabilityError("multi_image requires two or more media URLs in 'media'")


def _check_s3_namespacing(alias: str, media_refs: list[MediaRef]) -> None:
    """Reject an s3 ref whose key is not under this account's staging prefix.

    `stage_media` writes keys as `s3://<bucket>/staging/<alias>/<ulid>/...`;
    we parse the ref and check the alias segment matches the draft's
    account, so one account cannot reference another's staged bytes
    (DESIGN.md section 9).
    """
    expected = f"/staging/{alias}/"
    for ref in media_refs:
        if ref.source != "s3":
            continue
        key = ref.key or ""
        if not key.startswith("s3://"):
            raise CapabilityError(f"s3 media ref must start with s3://: got {key!r}")
        # drop scheme + bucket; what remains starts with the path
        without_scheme = key[len("s3://"):]
        _, slash, path = without_scheme.partition("/")
        if not slash or not path:
            raise CapabilityError(f"malformed s3 media ref: {key!r}")
        if f"/{path}".find(expected) != 0:
            raise CapabilityError(
                f"s3 media ref {key!r} is not under this account's staging "
                f"prefix ({expected}); call stage_media against account "
                f"'{alias}' and reuse the returned key"
            )


def _new_draft_id() -> str:
    now = datetime.now(UTC)
    return f"{int(now.timestamp()*1000):013d}{pysecrets.token_hex(4)}"


def _as_ns(draft_dict: dict):
    from types import SimpleNamespace

    return SimpleNamespace(**draft_dict)


def _preview(draft: dict) -> dict:
    """Model-facing preview of the stored draft."""
    text = draft.get("text", "") or ""
    return {
        "text": text if len(text) <= 300 else text[:299] + "…",
        "link": draft.get("link", ""),
        "media_count": len(draft.get("media", [])),
        "scheduled_time": draft.get("schedule", ""),
    }
