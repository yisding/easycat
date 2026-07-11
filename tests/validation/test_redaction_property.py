"""Property-based tests for validation/report secret redaction.

Redaction protects cassettes and validation artifacts from leaking
secrets, URLs, phone numbers, request ids, and home paths. The two
critical invariants: redaction is idempotent (re-redacting a redacted
string is a fixed point, so repeated serialization never corrupts the
output), and any explicit runtime secret passed to
``redact_runtime_secrets`` is fully removed from the output.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from easycat.validation.redaction import (
    REDACTED_SECRET,
    contains_unredacted_sensitive_text,
    redact_command,
    redact_runtime_secrets,
    redact_text,
    redact_value,
)

# A character set rich in the characters the redactors key off of (the
# letters spelling sk-/sess-/key-/tok-, req_/resp_, http(s)://, bearer,
# authorization, plus separators) so the generator organically assembles
# strings that exercise the URL / secret / request-id / phone branches.
# ``st.text`` requires single-character alphabet elements, so tokens are
# spelled out rather than passed as multi-char literals.
_REDACTION_ALPHABET = st.text(
    alphabet=st.sampled_from(list("skestoyrphbnaiquXYZ0123 -_/:.@+()=")),
    max_size=80,
)


@given(value=_REDACTION_ALPHABET)
def test_redact_text_is_idempotent(value: str) -> None:
    once = redact_text(value)
    # Re-redacting must reach a fixed point: no placeholder gets mangled.
    assert redact_text(once) == once


@given(value=st.text(max_size=200))
def test_redact_text_is_idempotent_arbitrary(value: str) -> None:
    once = redact_text(value)
    assert redact_text(once) == once


@pytest.mark.parametrize(
    ("value", "redacted_marker"),
    [
        ("https://api.openai.com/v1", "[REDACTED_URL]"),
        ("Authorization: Bearer short-secret", "[REDACTED_SECRET]"),
        ("bearer short-secret", "[REDACTED_SECRET]"),
        ("token=short-secret", "[REDACTED_SECRET]"),
        ("eyJabcdefghijk.abcdefghijk.abcdefghijk", "[REDACTED_SECRET]"),
        ("sk-abcdefghijkl", "[REDACTED_SECRET]"),
        ("request_abcdef", "[REDACTED_REQUEST_ID]"),
        ("+1 (415) 555-0123", "[REDACTED_PHONE]"),
        ("failed in /Users/alice/project", "~"),
    ],
)
def test_redact_text_prefilter_covers_every_pattern_family(
    value: str,
    redacted_marker: str,
) -> None:
    assert redacted_marker in redact_text(value)


def test_redact_text_preserves_ordinary_text() -> None:
    for value in (
        "ordinary transcript without sensitive material",
        "partial transcript word 123",
    ):
        assert redact_text(value) is value


@given(
    prefix=st.text(max_size=20),
    secret=st.text(
        alphabet=st.characters(min_codepoint=33, max_codepoint=126),
        min_size=8,
        max_size=24,
    ),
    suffix=st.text(max_size=20),
)
def test_runtime_secret_is_removed(prefix: str, secret: str, suffix: str) -> None:
    haystack = f"{prefix} {secret} {suffix}"
    redacted = redact_runtime_secrets(haystack, [secret])
    # The explicit secret literal must not survive anywhere in the output.
    assert secret not in redacted


@given(value=_REDACTION_ALPHABET)
def test_redact_runtime_secrets_without_secrets_matches_redact_text(
    value: str,
) -> None:
    # With no explicit secrets, it reduces to the base regex redaction.
    assert redact_runtime_secrets(value, None) == redact_text(value)


def test_key_based_redaction_catches_short_secret_values() -> None:
    payload = {
        "api_key": "short",
        "headers": {
            "Authorization": "Bearer short-token",
            "xi-api-key": "xi-short",
        },
        "auth": {
            "credential_env_var": "OPENAI_API_KEY",
            "credential_env_var_present": True,
        },
    }

    assert redact_value(payload) == {
        "api_key": REDACTED_SECRET,
        "auth": {
            "credential_env_var": "OPENAI_API_KEY",
            "credential_env_var_present": True,
        },
        "headers": {
            "Authorization": REDACTED_SECRET,
            "xi-api-key": REDACTED_SECRET,
        },
    }


def test_unsafe_text_fields_use_domain_specific_placeholders() -> None:
    assert redact_value("customer said hello", "transcript") == "[REDACTED_TRANSCRIPT]"
    assert redact_value("system prompt", "prompt") == "[REDACTED_PROMPT]"
    assert redact_value("provider output", "generated_provider_text") == (
        "[REDACTED_PROVIDER_TEXT]"
    )


def test_shared_detector_flags_cassette_sensitive_patterns() -> None:
    assert contains_unredacted_sensitive_text("Authorization: Bearer sk-testsecret123456")
    assert contains_unredacted_sensitive_text("https://api.openai.com/v1/audio")
    assert not contains_unredacted_sensitive_text("https://api.openai.test/v1/audio")
    assert not contains_unredacted_sensitive_text("[REDACTED_SECRET] [REDACTED_URL]")


def test_redact_command_redacts_split_secret_flags() -> None:
    assert redact_command(["easycat", "validate", "--api-key", "short"]) == [
        "easycat",
        "validate",
        "--api-key",
        REDACTED_SECRET,
    ]
    assert redact_command("easycat validate --api-key=short") == (
        f"easycat validate --api-key={REDACTED_SECRET}"
    )
