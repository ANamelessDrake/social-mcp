"""Meta error codes to plain-language messages, plus a token redactor.

The Graph API returns an `error` object with `code`, `error_subcode`, and a
`message` that is often terser than useful. We map the codes we see to
messages a human or an 8B model can act on, and we redact token-shaped
substrings from any outgoing error text as a defense in depth (DESIGN.md
section 11.2 and 13).
"""

import re

# Facebook user access tokens are long base64url-ish strings. "EAA" is the
# start of the base64-encoded signed token Meta mints for Facebook Login.
# Instagram and system-user tokens have different prefixes; redact any run
# that looks token-shaped (long, mostly URL-safe base64, no spaces).
_TOKEN_RE = re.compile(r"\b(EAA[A-Za-z0-9_\-]{20,}|[A-Za-z0-9_\-]{64,})\b")


def redact_tokens(text: str) -> str:
    """Replace token-shaped substrings with <redacted>."""
    return _TOKEN_RE.sub("<redacted>", text or "")


# Code (and optionally subcode) -> message. Subcode is checked first; missing
# subcodes fall back to the bare code key.
MESSAGES: dict[tuple[int, int | None], str] = {
    (4, None): "Meta app rate limit reached; backing off",
    (17, None): "User-level rate limit reached; backing off",
    (32, None): "Pages API user-token rate limit reached; backing off",
    (613, None): "Custom rate limit tripped; backing off",
    (613, 1996): "Inconsistent request volume; backing off",
    (
        190,
        None,
    ): "Meta token expired or revoked; the account needs reconnecting",
    (
        10,
        None,
    ): "This account did not grant the required scope at consent time; re-run admin.py connect",
    (
        100,
        None,
    ): "Meta rejected the request parameters as invalid",
    (
        100,
        2018001,
    ): "The Page token does not belong to this Page; the account needs reconnecting",
    (200, None): "Meta refused the request (permissions)",
    (368, None): "Content temporarily blocked by Meta's policy filters",
    (80001, None): "Pages business-use-case rate limit reached; backing off",
    (80002, None): "Instagram business-use-case rate limit reached; backing off",
}


def plain_message(code: int, subcode: int | None, raw_message: str = "") -> str:
    """Pick the mapped message, redacting the raw fallback if nothing matches."""
    if (code, subcode) in MESSAGES:
        return MESSAGES[(code, subcode)]
    if (code, None) in MESSAGES:
        return MESSAGES[(code, None)]
    return redact_tokens(raw_message or f"Meta returned error code {code}")


RETRYABLE_CODES = {4, 17, 32, 613, 80001, 80002}
TOKEN_INVALID_CODES = {190}
