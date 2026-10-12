"""Approval hashing + verification (DESIGN.md section 7.2).

The approval record is the gate the Publisher consults immediately before
the Meta POST; the hash binds to the exact text and media, so a draft edit
after approval invalidates the record silently and the next publish surfaces
a specific error naming the drift. This is enforced server-side regardless
of per-account `publish_mode` (section 7.4): in `auto` and matching
`auto_for` modes the server records the implicit approval at `draft_post`
time; in `manual` mode it is `admin.py approve` that writes it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict

from sharedModules import dynamo
from sharedModules.errors import AccountError


class ApprovalError(Exception):
    """The current draft hash does not match a recorded approval."""


def _canonical_media(draft) -> list[dict]:
    """Order matters for media (position 0 vs 1 in a carousel), so no sort."""
    rows: list[dict] = []
    for m in getattr(draft, "media", []) or []:
        if hasattr(m, "etag") and hasattr(m, "mime"):
            rows.append(
                {
                    "etag": getattr(m, "etag", "") or "",
                    "mime": getattr(m, "mime", "") or "",
                }
            )
        elif isinstance(m, dict):
            rows.append({"etag": m.get("etag", "") or "", "mime": m.get("mime", "") or ""})
        else:
            rows.append({"etag": str(m), "mime": ""})
    return rows


def hash_draft(draft) -> str:
    """SHA-256 of canonical sorted-key UTF-8 JSON (DESIGN.md section 7.2).

    Media rows are kept in input order; everything else is sorted so field
    reordering never changes the hash.
    """
    payload = {
        "account": getattr(draft, "account", ""),
        "kind": getattr(draft, "kind", ""),
        "text": getattr(draft, "text", "") or "",
        "link": getattr(draft, "link", "") or "",
        "media": _canonical_media(draft),
        "schedule": getattr(draft, "schedule", "") or "",
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def record_approval(draft_id: str, scope: str, approved_by: str) -> dict:
    """Hash the current draft and persist the approval row.

    Overwrites any existing approval record for the draft, which is the
    right thing: a re-approval after an edit should supersede the previous
    stale record, not duplicate it.
    """
    draft = dynamo.read_draft(draft_id)
    if draft is None:
        raise AccountError(f"draft '{draft_id}' not found")
    content_hash = _hash_item(draft)
    dynamo.write_approval(
        draft_id=draft_id,
        content_hash=content_hash,
        approved_by=approved_by,
        scope=scope,
    )
    return {"draft_id": draft_id, "content_hash": content_hash, "approved_by": approved_by}


def verify_approval(draft_id: str, scope: str) -> dict:
    """Re-hash the current draft and compare constant-time to the record.

    Raises ApprovalError on mismatch; the message names what to do next.
    """
    draft = dynamo.read_draft(draft_id)
    if draft is None:
        raise AccountError(f"draft '{draft_id}' not found")
    approval = dynamo.read_approval(draft_id)
    if approval is None:
        raise ApprovalError(
            f"draft '{draft_id}' has no approval record; "
            f"run `python scripts/admin.py approve --draft-id {draft_id}`"
        )
    if approval.get("scope") != scope:
        raise ApprovalError(
            f"approval scope is '{approval.get('scope')}', not '{scope}'; "
            f"re-approve with the correct scope"
        )
    current = _hash_item(draft)
    stored = approval.get("content_hash", "")
    if not hmac.compare_digest(current, stored):
        raise ApprovalError(
            f"draft '{draft_id}' was edited after approval; "
            f"re-run `python scripts/admin.py approve --draft-id {draft_id}`"
        )
    return approval


def invalidate_approval(draft_id: str) -> None:
    """Called by update_draft; silently drops the record on edit."""
    dynamo.delete_approval(draft_id)


# -------------------- M3 scopes: edit / delete / reply --------------------
#
# Each scope's hash binds to the operator-approved payload (the staged
# change), so the Publisher reads what was approved, not what the agent
# re-sent. The hide scope is a *standing* per-account approval and
# therefore has no payload hash.


def hash_edit(text: str) -> str:
    """Hash of a staged edit's text."""
    blob = json.dumps({"text": text or ""}, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def hash_delete(post_id: str, reason: str) -> str:
    """Hash of a staged delete's target + reason."""
    payload = {"post_id": post_id or "", "reason": reason or ""}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def hash_reply(text: str) -> str:
    """Hash of a staged reply's text."""
    blob = json.dumps({"text": text or ""}, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def record_edit_approval(post_id: str, text: str, approved_by: str) -> dict:
    content_hash = hash_edit(text)
    dynamo.write_approval_record(
        key=f"edit:{post_id}",
        content_hash=content_hash,
        approved_by=approved_by,
        scope=f"edit:{post_id}",
    )
    return {"post_id": post_id, "content_hash": content_hash}


def record_delete_approval(post_id: str, reason: str, approved_by: str) -> dict:
    content_hash = hash_delete(post_id, reason)
    dynamo.write_approval_record(
        key=f"delete:{post_id}",
        content_hash=content_hash,
        approved_by=approved_by,
        scope=f"delete:{post_id}",
        payload={"reason": reason or ""},
    )
    return {"post_id": post_id, "content_hash": content_hash}


def record_reply_approval(comment_id: str, text: str, approved_by: str) -> dict:
    content_hash = hash_reply(text)
    dynamo.write_approval_record(
        key=f"reply:{comment_id}",
        content_hash=content_hash,
        approved_by=approved_by,
        scope=f"reply:{comment_id}",
    )
    return {"comment_id": comment_id, "content_hash": content_hash}


def verify_edit_approval(post_id: str, current_text: str) -> dict:
    approval = dynamo.read_approval_record(f"edit:{post_id}")
    if approval is None:
        raise ApprovalError(
            f"edit of '{post_id}' has no approval record; "
            f"run `python scripts/admin.py approve-edit --post-id {post_id}`"
        )
    current = hash_edit(current_text)
    stored = approval.get("content_hash", "")
    if not hmac.compare_digest(current, stored):
        raise ApprovalError(
            f"edit of '{post_id}' was changed after approval; "
            f"re-run `python scripts/admin.py approve-edit --post-id {post_id}`"
        )
    return approval


def verify_delete_approval(post_id: str, current_reason: str) -> dict:
    approval = dynamo.read_approval_record(f"delete:{post_id}")
    if approval is None:
        raise ApprovalError(
            f"delete of '{post_id}' has no approval record; "
            f"run `python scripts/admin.py approve-delete --post-id {post_id}`"
        )
    current = hash_delete(post_id, current_reason)
    stored = approval.get("content_hash", "")
    if not hmac.compare_digest(current, stored):
        raise ApprovalError(
            f"delete of '{post_id}' was changed after approval; "
            f"re-run `python scripts/admin.py approve-delete --post-id {post_id}`"
        )
    return approval


def verify_reply_approval(comment_id: str, current_text: str) -> dict:
    approval = dynamo.read_approval_record(f"reply:{comment_id}")
    if approval is None:
        raise ApprovalError(
            f"reply to '{comment_id}' has no approval record; "
            f"run `python scripts/admin.py approve-reply --comment-id {comment_id}`"
        )
    current = hash_reply(current_text)
    stored = approval.get("content_hash", "")
    if not hmac.compare_digest(current, stored):
        raise ApprovalError(
            f"reply to '{comment_id}' was changed after approval; "
            f"re-run `python scripts/admin.py approve-reply --comment-id {comment_id}`"
        )
    return approval


def verify_hide_approval(account: str) -> dict:
    """The hide scope is a standing per-account approval."""
    standing = dynamo.read_hide_standing(account)
    if standing is None:
        raise ApprovalError(
            f"account '{account}' has no standing hide approval; "
            f"run `python scripts/admin.py approve-hide --account {account}`"
        )
    return standing


def _hash_item(draft) -> str:
    """Hash a draft dict (as it comes from Dynamo) or a Draft dataclass."""
    if hasattr(draft, "__dataclass_fields__"):
        obj = _DraftView(**asdict(draft))
    else:
        obj = _DraftView(**_fill(draft))
    return hash_draft(obj)


def _fill(draft: dict) -> dict:
    """Narrow a dict to the fields hash_draft reads."""
    return {
        "account": draft.get("account", ""),
        "kind": draft.get("kind", ""),
        "text": draft.get("text", "") or "",
        "link": draft.get("link", "") or "",
        "media": draft.get("media", []) or [],
        "schedule": draft.get("schedule", "") or "",
    }


class _DraftView:
    """Minimal shape for hash_draft; a dataclass asdict() always yields this."""

    def __init__(self, account="", kind="", text="", link="", media=None, schedule="", **_rest):
        self.account = account
        self.kind = kind
        self.text = text
        self.link = link
        self.media = media or []
        self.schedule = schedule
