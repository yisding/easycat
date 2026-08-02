"""Property-based tests for validation/report secret redaction.

Redaction protects cassettes and validation artifacts from leaking
secrets, URLs, phone numbers, request ids, and home paths. The two
critical invariants: redaction is idempotent (re-redacting a redacted
string is a fixed point, so repeated serialization never corrupts the
output), and any explicit runtime secret passed to
``redact_runtime_secrets`` is fully removed from the output.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from easycat._provider_catalog import ProviderCatalog
from easycat.validation import redaction as redaction_module
from easycat.validation.redaction import (
    REDACTED_SECRET,
    contains_unredacted_sensitive_text,
    redact_command,
    redact_runtime_secrets,
    redact_runtime_secrets_in_file,
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


def test_redact_text_prefilter_covers_midword_bearer_policy() -> None:
    assert redact_text("clientBearer abc123") == f"clientBearer {REDACTED_SECRET}"


@pytest.mark.parametrize(
    "pattern",
    [pattern for pattern, _replacement in redaction_module._TEXT_REDACTIONS],
)
@given(data=st.data())
def test_text_redaction_trigger_is_policy_superset(
    pattern: re.Pattern[str],
    data: st.DataObject,
) -> None:
    value = data.draw(st.from_regex(pattern, fullmatch=True))
    assert redaction_module._TEXT_REDACTION_TRIGGER_RE.search(value) is not None


def test_redact_text_preserves_ordinary_text() -> None:
    for value in (
        "ordinary transcript without sensitive material",
        "partial transcript word 123",
    ):
        assert redact_text(value) is value


def test_secrets_policy_preserves_replay_content() -> None:
    value = (
        "call +1 (415) 555-0123; visit https://acme.example/orders; "
        "request req_abcdef123; path /Users/alice/report.pdf"
    )

    assert redact_text(value, policy="secrets") == value
    assert redact_value(value, "transcript", policy="secrets") == value


def test_secrets_policy_still_redacts_credentials() -> None:
    assert (
        redact_text(
            "Authorization: Bearer sk-testsecret123456",
            policy="secrets",
        )
        == f"Authorization: {REDACTED_SECRET}"
    )
    assert redact_value(
        {"transcript": "order 1234567890", "api_key": "short"},
        policy="secrets",
    ) == {
        "api_key": REDACTED_SECRET,
        "transcript": "order 1234567890",
    }


def test_secrets_policy_scrubs_credentials_inside_urls() -> None:
    password = "hunter" + "2"
    authority = "".join(("alice", ":", password, "@", "acme.example"))
    value = "".join(
        (
            "https://",
            authority,
            "/orders?order=1234567890&X-Amz-Signature=signed-value",
        )
    )

    redacted = redact_text(value, policy="secrets")

    redacted_authority = "".join(("alice", ":", REDACTED_SECRET, "@", "acme.example"))
    assert redacted == (
        f"https://{redacted_authority}/orders?order=1234567890&X-Amz-Signature={REDACTED_SECRET}"
    )

    assert redact_text(
        "https://maps.example/route?key=maps-secret-value&sig=signed-value",
        policy="secrets",
    ) == (f"https://maps.example/route?key={REDACTED_SECRET}&sig={REDACTED_SECRET}")


def test_redact_text_rejects_unknown_policy() -> None:
    with pytest.raises(ValueError, match="Unknown redaction policy"):
        redact_text("value", policy="everything")  # type: ignore[arg-type]


def test_secret_key_classification_cache_is_bounded() -> None:
    redaction_module._is_secret_value_key.cache_clear()
    try:
        for index in range(512):
            redact_value("ordinary value", f"field_{index}")
        cache_info = redaction_module._is_secret_value_key.cache_info()
        assert cache_info.maxsize == 256
        assert cache_info.currsize == 256
    finally:
        redaction_module._is_secret_value_key.cache_clear()


# Redaction placeholders emitted by the policy (``[REDACTED_SECRET]``,
# ``[REDACTED_URL]``, ...). A generated secret can be a substring of a
# placeholder (e.g. the literal ``"REDACTED"``), so "secret not in output"
# is falsifiable by construction; the real guarantee is that no occurrence
# of the secret survives *outside* the placeholders themselves.
_PLACEHOLDER_RE = re.compile(r"\[REDACTED_[A-Z_]+\]")


@given(
    prefix=st.text(max_size=20),
    secret=st.text(
        alphabet=st.characters(min_codepoint=33, max_codepoint=126),
        min_size=8,
        max_size=24,
    ),
    suffix=st.text(max_size=20),
)
# Regression: a secret that is a substring of the placeholder must count as
# removed once it has been replaced by ``[REDACTED_SECRET]``.
@example(prefix="", secret="REDACTED", suffix="")
def test_runtime_secret_is_removed(prefix: str, secret: str, suffix: str) -> None:
    haystack = f"{prefix} {secret} {suffix}"
    redacted = redact_runtime_secrets(haystack, [secret])
    # The explicit secret literal must not survive anywhere in the output;
    # strip the placeholders first so a secret spelling out a placeholder
    # fragment does not read its own replacement as a leak.
    assert secret not in _PLACEHOLDER_RE.sub("", redacted)


@given(value=_REDACTION_ALPHABET)
def test_redact_runtime_secrets_without_secrets_matches_redact_text(
    value: str,
) -> None:
    # With no explicit secrets, it reduces to the base regex redaction.
    assert redact_runtime_secrets(value, None) == redact_text(value)


def test_runtime_secret_redacts_xml_escaped_junit_spelling(tmp_path: Path) -> None:
    secret = 'token&value<with>"quotes"'
    escaped = xml_escape(secret, {'"': "&quot;", "'": "&apos;"})
    path = tmp_path / "junit.xml"
    path.write_text(f"<failure>{escaped}</failure>", encoding="utf-8")

    assert redact_runtime_secrets_in_file(path, (secret,), artifact_format="text")

    redacted = path.read_text(encoding="utf-8")
    assert secret not in redacted
    assert escaped not in redacted
    assert REDACTED_SECRET in redacted


@pytest.mark.parametrize("artifact_format", ["json", "jsonl"])
def test_malformed_structured_artifact_scrubs_raw_and_json_escaped_secrets(
    tmp_path: Path,
    artifact_format: str,
) -> None:
    secret = 'plain-"runtime\\token-value'
    escaped_secret = json.dumps(secret)[1:-1]
    path = tmp_path / f"malformed.{artifact_format}"
    path.write_text(
        f'raw={secret}\n{{"credential_echo": "{escaped_secret}" trailing\n',
        encoding="utf-8",
    )

    assert redact_runtime_secrets_in_file(
        path,
        (secret,),
        artifact_format=artifact_format,  # type: ignore[arg-type]
    )

    redacted = path.read_text(encoding="utf-8")
    assert secret not in redacted
    assert escaped_secret not in redacted
    assert redacted.count(REDACTED_SECRET) == 2


def test_non_utf8_artifact_scrubs_raw_and_json_escaped_secret_bytes(
    tmp_path: Path,
) -> None:
    secret = 'plain-"runtime\\token-value'
    raw_secret = secret.encode("utf-8")
    escaped_secret = json.dumps(secret)[1:-1].encode("utf-8")
    path = tmp_path / "malformed.jsonl"
    path.write_bytes(b"\xffraw=" + raw_secret + b"\nescaped=" + escaped_secret)

    assert not redact_runtime_secrets_in_file(path, (secret,), artifact_format="jsonl")

    redacted = path.read_bytes()
    assert raw_secret not in redacted
    assert escaped_secret not in redacted
    assert redacted.count(REDACTED_SECRET.encode("utf-8")) == 2


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


def test_shared_detector_tracks_dynamically_registered_provider_domains() -> None:
    class Provider:
        pass

    class Config:
        pass

    catalog = ProviderCatalog(specs={}, kind="TEST")
    catalog.register(
        "custom",
        Provider,
        Config,
        api_domains=("api.custom-provider.invalid",),
    )

    assert contains_unredacted_sensitive_text("https://api.custom-provider.invalid/v1/transcribe")


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
