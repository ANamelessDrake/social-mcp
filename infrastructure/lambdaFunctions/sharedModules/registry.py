"""Account registry: the static roster from config merged with dynamic state.

The ACCOUNTS env var (JSON, from config/{env}.json) says which accounts
exist and what their publish posture is; the DynamoDB ACCOUNT item says
whether each one has a live grant. Aliases are the only account names
tools ever see.
"""

import json
import os
from functools import lru_cache

from sharedModules import dynamo, scope
from sharedModules.errors import AccountError


@lru_cache(maxsize=1)
def all_accounts() -> dict[str, dict]:
    """Every account this deployment serves, from config."""
    entries = json.loads(os.environ.get("ACCOUNTS", "[]"))
    return {entry["alias"]: entry for entry in entries}


def configured_accounts() -> dict[str, dict]:
    """The roster as the current caller sees it (DESIGN.md section 4.2).

    A client that pinned its connection to one account (sharedModules/scope.py)
    sees only that one, and every alias lookup, the all-account sweep, and
    list_accounts route through here, so the narrowing happens in one place
    rather than being trusted to each tool.
    """
    boxes = all_accounts()
    allowed = scope.allowed_accounts()
    if allowed is None:
        return boxes
    return {alias: entry for alias, entry in boxes.items() if alias in allowed}


def resolve_alias(account: str) -> str:
    """Map a tool's account parameter to a configured alias.

    An empty value resolves to the sole configured account, so single-account
    deployments never need the parameter.
    """
    boxes = configured_accounts()
    if not boxes:
        raise AccountError("no accounts are configured for this deployment")
    if not account:
        if len(boxes) == 1:
            return next(iter(boxes))
        raise AccountError(
            "multiple accounts are configured; pass account="
            + " or ".join(sorted(boxes))
        )
    if account not in boxes:
        raise AccountError(
            f"unknown account '{account}'; configured accounts: " + ", ".join(sorted(boxes))
        )
    return account


def resolve_sweep_aliases(account: str) -> list[str]:
    """Fan-out resolution for list_posts (DESIGN.md section 6).

    Empty means every configured account: monitoring several accounts is one
    call, not one call per account. A named account narrows to just that one.
    """
    boxes = configured_accounts()
    if not boxes:
        raise AccountError("no accounts are configured for this deployment")
    if not account:
        return sorted(boxes)
    return [resolve_alias(account)]


def account_record(alias: str) -> dict:
    """Config entry merged with the account's DynamoDB item.

    status is 'not_connected' until `admin.py connect` has run for the alias.
    """
    record = dict(configured_accounts()[alias])
    item = dynamo.get_account(alias)
    if item is None:
        record.update(
            {
                "status": "not_connected",
                "account_id": "",
                "display_name": "",
                "scopes": [],
            }
        )
    else:
        record.update(
            {
                "status": item.get("status", "active"),
                "account_id": item.get("account_id", ""),
                "display_name": item.get("display_name", ""),
                "scopes": list(item.get("scopes", [])),
                "page_token_ct": item.get("page_token_ct"),
                "user_token_ct": item.get("user_token_ct"),
                "linked_ig_account": item.get("linked_ig_account", ""),
                "linked_page": item.get("linked_page", ""),
            }
        )
    return record


def require_scope(alias: str, scope_name: str) -> None:
    """Fail a tool call loudly when the account did not grant a scope.

    The server knows exactly which scope is missing, so error messages name
    it and the `admin.py connect` capability that would add it.
    """
    record = account_record(alias)
    if record.get("status") != "active":
        raise AccountError(
            f"account '{alias}' is not connected; run "
            f"`python scripts/admin.py connect --alias {alias}`"
        )
    if scope_name not in set(record.get("scopes", [])):
        raise AccountError(
            f"account '{alias}' did not grant '{scope_name}' at consent time; "
            f"re-run `admin.py connect --alias {alias}` to add it"
        )
