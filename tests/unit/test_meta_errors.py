from sharedModules.providers import metaErrors


def test_subcode_hit_wins_over_code():
    assert "does not belong to this Page" in metaErrors.plain_message(100, 2018001, "x")


def test_code_hit_when_no_subcode():
    assert "token expired" in metaErrors.plain_message(190, None, "x").lower()


def test_unknown_code_falls_back_to_redacted_raw():
    msg = metaErrors.plain_message(
        99999, None, "something failed EAAabcdefghijklmnopqrstuvwxyz0123456789"
    )
    assert "<redacted>" in msg
    assert "EAAabc" not in msg


def test_unknown_code_without_raw_names_the_code():
    assert "99999" in metaErrors.plain_message(99999, None, "")


def test_redactor_catches_long_bareword():
    long = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef"
    assert "<redacted>" in metaErrors.redact_tokens(f"error: {long}")


def test_retryable_set_matches_the_design():
    # DESIGN.md section 11.1
    assert metaErrors.RETRYABLE_CODES == {4, 17, 32, 613, 80001, 80002}
    assert 190 in metaErrors.TOKEN_INVALID_CODES
    assert 100 not in metaErrors.RETRYABLE_CODES
