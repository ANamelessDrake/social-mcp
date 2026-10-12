"""Policy engine tests (DESIGN.md section 7.5)."""

from types import SimpleNamespace

import pytest
from sharedModules import policy


def _draft(**fields) -> SimpleNamespace:
    base = {"account": "a", "kind": "text", "text": "", "link": "", "media": [], "schedule": ""}
    return SimpleNamespace(**{**base, **fields})


def test_banned_characters_catches_em_dash():
    pol = {"banned_characters": ["\u2014"]}
    draft = _draft(text="this is a test \u2014 oops")
    violations = policy.validate(draft, pol)
    assert len(violations) == 1
    assert violations[0].field == "banned_characters"
    assert "U+2014" in violations[0].message


def test_banned_characters_passes_without_the_char():
    pol = {"banned_characters": ["\u2014"]}
    draft = _draft(text="clean, no bad character here")
    assert policy.validate(draft, pol) == []


def test_banned_words_is_case_insensitive():
    pol = {"banned_words": ["Claude", "Anthropic"]}
    draft = _draft(text="shipped with claude today")
    violations = policy.validate(draft, pol)
    assert len(violations) == 1
    assert "Claude" in violations[0].message


def test_banned_words_scans_link_too():
    pol = {"banned_words": ["forbidden"]}
    draft = _draft(kind="link", text="see this", link="https://forbidden.example.com/path")
    assert any(v.field == "banned_words" for v in policy.validate(draft, pol))


def test_required_signoff_rejects_missing_suffix():
    pol = {"required_signoff": "- WILMA"}
    draft = _draft(text="hello there")
    violations = policy.validate(draft, pol)
    assert any(v.field == "required_signoff" for v in violations)


def test_required_signoff_accepts_trailing_signoff():
    pol = {"required_signoff": "- WILMA"}
    draft = _draft(text="hello there\n- WILMA")
    assert policy.validate(draft, pol) == []


def test_max_hashtags_counts_hash_occurrences():
    pol = {"max_hashtags": 2}
    draft = _draft(text="one #a two #b three #c")
    violations = policy.validate(draft, pol)
    assert any(v.field == "max_hashtags" for v in violations)


def test_max_hashtags_minus_one_disables_cap():
    pol = {"max_hashtags": -1}
    draft = _draft(text="#" * 50)
    assert policy.validate(draft, pol) == []


def test_allowed_link_domains_blocks_foreign_domain():
    pol = {"allowed_link_domains": ["justbardtech.com"]}
    draft = _draft(kind="link", text="see here", link="https://evil.example.com/x")
    violations = policy.validate(draft, pol)
    assert any(v.field == "allowed_link_domains" for v in violations)


def test_allowed_link_domains_accepts_subdomain():
    pol = {"allowed_link_domains": ["justbardtech.com"]}
    draft = _draft(kind="link", text="see here", link="https://blog.justbardtech.com/x")
    assert policy.validate(draft, pol) == []


def test_allowed_link_domains_scans_urls_in_text():
    pol = {"allowed_link_domains": ["justbardtech.com"]}
    draft = _draft(text="my site is https://other.example/x")
    violations = policy.validate(draft, pol)
    assert any(v.field == "allowed_link_domains" for v in violations)


def test_enforce_raises_on_first_violation():
    pol = {"banned_characters": ["\u2014"], "banned_words": ["Claude"]}
    draft = _draft(text="\u2014 Claude")
    with pytest.raises(policy.PolicyError) as exc:
        policy.enforce(draft, pol)
    # The first violation is the em-dash
    assert "banned_characters" in str(exc.value)


def test_enforce_is_a_noop_on_clean_drafts():
    pol = {"banned_words": ["bad"], "max_hashtags": 5}
    draft = _draft(text="this is fine #one #two")
    policy.enforce(draft, pol)


def test_warnings_from_formats_each_entry():
    pol = {"banned_words": ["foo"], "max_hashtags": 1}
    draft = _draft(text="foo #a #b")
    warnings = policy.warnings_from(policy.validate(draft, pol))
    assert len(warnings) == 2
    assert warnings[0].startswith("banned_words:")
    assert warnings[1].startswith("max_hashtags:")
