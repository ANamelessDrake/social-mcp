"""Policy engine for profile edits (M9, DESIGN.md section 7.7)."""

import pytest
from sharedModules import policy


def test_banned_characters_catches_em_dash_in_about():
    pol = {"banned_characters": ["\u2014"]}
    violations = policy.validate_profile_patch({"about": "oops \u2014 banned"}, pol)
    assert len(violations) == 1
    assert violations[0].field == "banned_characters"
    assert "profile.about" in violations[0].message


def test_banned_words_scanned_per_field():
    pol = {"banned_words": ["Claude"]}
    violations = policy.validate_profile_patch({"display_name": "built by Claude"}, pol)
    assert any(
        v.field == "banned_words" and "profile.display_name" in v.message for v in violations
    )


def test_website_against_allowed_link_domains_rejects_foreign_domain():
    pol = {"allowed_link_domains": ["justbardtech.com"]}
    violations = policy.validate_profile_patch({"website": "https://evil.example.com/path"}, pol)
    assert any(v.field == "allowed_link_domains" for v in violations)


def test_website_against_allowed_link_domains_accepts_subdomain():
    pol = {"allowed_link_domains": ["justbardtech.com"]}
    assert policy.validate_profile_patch({"website": "https://blog.justbardtech.com/x"}, pol) == []


def test_phone_and_emails_are_not_text_checked():
    """phone and emails are not scanned for banned_words/characters.

    If an operator wants to block an email domain they do it through the
    policy on `allowed_link_domains` instead, because an email string
    cannot be banned by a word check without breaking most tenants'
    legitimate addresses.
    """
    pol = {"banned_words": ["example"], "banned_characters": ["@"]}
    assert (
        policy.validate_profile_patch(
            {"phone": "+1-212-example-0001", "emails": ["who@example.com"]}, pol
        )
        == []
    )


def test_enforce_raises_on_first_violation():
    pol = {"banned_words": ["Claude"]}
    with pytest.raises(policy.PolicyError) as exc:
        policy.enforce_profile_patch({"about": "built with Claude"}, pol)
    assert "banned_words" in str(exc.value)
    assert "profile.about" in str(exc.value)


def test_enforce_is_a_noop_on_clean_patch():
    pol = {"banned_characters": ["\u2014"], "allowed_link_domains": ["justbardtech.com"]}
    policy.enforce_profile_patch(
        {
            "about": "hello world",
            "description": "longer bio",
            "website": "https://justbardtech.com",
        },
        pol,
    )


def test_empty_patch_passes():
    policy.enforce_profile_patch({}, {"banned_words": ["anything"]})
