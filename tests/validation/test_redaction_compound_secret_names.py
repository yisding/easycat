"""Free-text redaction must agree with field-name redaction on compound secret names.

``redact_value`` masks the value of ``AWS_SECRET_ACCESS_KEY`` or ``secret_key``
when it appears as a mapping key, because ``secret`` is a whole segment of the
name. The free-text and command paths must mask the same ``NAME=value`` pair
when it appears in a log line or command string (gh 1289).
"""

from __future__ import annotations

import pytest

from easycat.validation.redaction import redact_command, redact_text, redact_value

_VALUE = "wJalrXUtnFEMI-K7MDENG-bPxRfiCYzzzzzzzz"
_NAMES = ("AWS_SECRET_ACCESS_KEY", "secret_key", "SECRET_KEY", "private_key")


@pytest.mark.parametrize("name", _NAMES)
def test_field_name_redaction_masks_compound_secret_names(name: str) -> None:
    redacted = redact_value({name: _VALUE}, policy="secrets")

    assert _VALUE not in str(redacted)


@pytest.mark.parametrize("name", _NAMES)
def test_redact_text_masks_compound_secret_assignment(name: str) -> None:
    redacted = redact_text(f"export {name}={_VALUE}", policy="secrets")

    assert _VALUE not in redacted


@pytest.mark.parametrize("name", _NAMES)
def test_redact_command_masks_compound_secret_assignment(name: str) -> None:
    redacted = redact_command(f"{name}={_VALUE} python app.py", policy="secrets")

    assert _VALUE not in str(redacted)


@pytest.mark.parametrize(
    "text",
    [
        "monkey=banana",
        "monkey_name=banana",
        "token_count=5",
        "record_key=cp_42",
        "max_tokens=100",
        "sort_keys=true",
        "token = 'bearer-env:NAME'",
    ],
)
def test_redact_text_leaves_non_secret_compound_names_alone(text: str) -> None:
    assert redact_text(text, policy="secrets") == text


def test_redact_text_masks_quoted_compound_secret_key() -> None:
    assert redact_text('{"secret_key": "a b"}', policy="secrets") == (
        '{"secret_key": "[REDACTED_SECRET]"}'
    )


@pytest.mark.parametrize("name", ["API_KEYS", "passwords", "client_credentials"])
def test_redact_text_masks_plural_and_credential_assignments(name: str) -> None:
    assert _VALUE not in redact_text(f"{name}={_VALUE}", policy="secrets")
