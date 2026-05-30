"""Property-based tests for validation/report secret redaction.

Redaction protects cassettes and validation artifacts from leaking
secrets, URLs, phone numbers, request ids, and home paths. The two
critical invariants: redaction is idempotent (re-redacting a redacted
string is a fixed point, so repeated serialization never corrupts the
output), and any explicit runtime secret passed to
``redact_runtime_secrets`` is fully removed from the output.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from easycat.validation.report import redact_runtime_secrets, redact_text

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
