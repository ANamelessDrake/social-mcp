"""Request-scoped account narrowing (DESIGN.md section 4.2).

A client that only cares about one account (say a brand-page agent that
should never touch the dev page) pins its connection to that account with a
header; the middleware stores it here and the registry narrows every alias
lookup, sweep, and listing to it.

This is a *scoping* switch, not a security boundary: the token still grants
the whole deployment, and a client could simply not send the header. What it
buys is that a single-purpose agent sees only what it needs, cannot
accidentally sweep an unrelated account into its context, and cannot talk
itself into one either, because the header is set by the client's
configuration and the model has no way to change it.

None means unrestricted: every account the deployment serves.
"""

from contextvars import ContextVar

_allowed: ContextVar[tuple[str, ...] | None] = ContextVar("social_mcp_scope", default=None)


def set_scope(aliases: tuple[str, ...] | None) -> None:
    _allowed.set(aliases)


def allowed_accounts() -> tuple[str, ...] | None:
    """Accounts this request may use, or None for all of them."""
    return _allowed.get()
