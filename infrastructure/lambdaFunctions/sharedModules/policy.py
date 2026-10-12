"""Per-account content policy (DESIGN.md section 7.5).

Policy is config, not code: each account's `policy` block is loaded from
config/{env}.json and enforced at `draft_post` time, then again in the
Publisher immediately before the Meta POST so a config change between
approval and publish is honored.

Checks:

- `banned_characters`: list of forbidden Unicode code points (strings).
- `banned_words`: substring matches, case-insensitive.
- `required_signoff`: literal that must end the text (empty = no requirement).
- `max_hashtags`: integer cap; -1 means no cap.
- `allowed_link_domains`: when non-empty, link posts (and any URL embedded in
  the text) must point at one of these domains.

`validate` returns the list of violations for the preview;
`enforce` raises PolicyError naming the first blocking one so the model can
self-correct.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


class PolicyError(Exception):
    """A policy violation blocked a draft or publish."""


@dataclass
class PolicyViolation:
    field: str  # "banned_characters" | "banned_words" | ...
    message: str


_URL_RE = re.compile(r"https?://[^\s)>\]]+", re.IGNORECASE)


def _domain(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    # Strip :port, keep host
    return netloc.split(":", 1)[0]


def _text_bits(draft) -> str:
    """Collect everything the policy inspects as one string."""
    text = getattr(draft, "text", "") or ""
    link = getattr(draft, "link", "") or ""
    if link:
        return f"{text}\n{link}"
    return text


def validate(draft, policy: dict) -> list[PolicyViolation]:
    """Collect every violation; non-raising, for preview."""
    violations: list[PolicyViolation] = []
    text = getattr(draft, "text", "") or ""
    link = getattr(draft, "link", "") or ""
    scan = _text_bits(draft)

    banned_chars = policy.get("banned_characters") or []
    for ch in banned_chars:
        if ch and ch in scan:
            violations.append(
                PolicyViolation(
                    field="banned_characters",
                    message=(
                        f"text contains a banned character (U+{ord(ch):04X}); "
                        "remove it before approving"
                    ),
                )
            )

    banned_words = policy.get("banned_words") or []
    lowered = scan.lower()
    for word in banned_words:
        if word and word.lower() in lowered:
            violations.append(
                PolicyViolation(
                    field="banned_words",
                    message=f"text mentions '{word}', which is banned on this account",
                )
            )

    signoff = (policy.get("required_signoff") or "").strip()
    if signoff and not text.rstrip().endswith(signoff):
        violations.append(
            PolicyViolation(
                field="required_signoff",
                message=f"text does not end with the required sign-off '{signoff}'",
            )
        )

    max_hashtags = int(policy.get("max_hashtags", -1))
    if max_hashtags >= 0:
        count = text.count("#")
        if count > max_hashtags:
            violations.append(
                PolicyViolation(
                    field="max_hashtags",
                    message=(
                        f"text has {count} hashtags; this account caps at {max_hashtags}"
                    ),
                )
            )

    allowed = [d.lower() for d in (policy.get("allowed_link_domains") or []) if d]
    if allowed:
        urls = _URL_RE.findall(scan)
        for url in urls:
            dom = _domain(url)
            if not dom:
                continue
            if not any(dom == a or dom.endswith("." + a) for a in allowed):
                violations.append(
                    PolicyViolation(
                        field="allowed_link_domains",
                        message=(
                            f"link to '{dom}' is not on this account's domain allowlist"
                        ),
                    )
                )
        if link:
            dom = _domain(link)
            if dom and not any(dom == a or dom.endswith("." + a) for a in allowed):
                violations.append(
                    PolicyViolation(
                        field="allowed_link_domains",
                        message=(
                            f"link to '{dom}' is not on this account's domain allowlist"
                        ),
                    )
                )

    return violations


def enforce(draft, policy: dict) -> None:
    """Raise on the first violation; the message names what to change."""
    violations = validate(draft, policy)
    if violations:
        first = violations[0]
        raise PolicyError(f"{first.field}: {first.message}")


def warnings_from(violations: list[PolicyViolation]) -> list[str]:
    """Flatten violations for a draft preview (list of strings)."""
    return [f"{v.field}: {v.message}" for v in violations]
