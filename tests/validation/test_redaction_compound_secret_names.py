"""Free-text redaction must agree with field-name redaction on compound secret names.

``redact_value`` masks the value of ``AWS_SECRET_ACCESS_KEY`` or ``secret_key``
when it appears as a mapping key, because ``secret`` is a whole segment of the
name. The free-text and command paths only recognise a secret keyword that is
immediately followed by ``=`` or ``:``, so the same ``NAME=value`` pair leaks
verbatim when it appears in a log line or command string.
"""

from __future__ import annotations

import pytest

from easycat.validation.redaction import redact_command, redact_text, redact_value

_VALUE = "wJalrXUtnFEMI-K7MDENG-bPxRfiCYzzzzzzzz"
_NAMES = ("AWS_SECRET_ACCESS_KEY", "secret_key", "SECRET_KEY", "private_key")

_known_leak = pytest.mark.xfail(
    strict=True,
    reason="text/command redaction only matches a secret keyword directly before '=' or ':'",
)


@pytest.mark.parametrize("name", _NAMES)
def test_field_name_redaction_masks_compound_secret_names(name: str) -> None:
    redacted = redact_value({name: _VALUE}, policy="secrets")

    assert _VALUE not in str(redacted)


@_known_leak
@pytest.mark.parametrize("name", _NAMES)
def test_redact_text_masks_compound_secret_assignment(name: str) -> None:
    redacted = redact_text(f"export {name}={_VALUE}", policy="secrets")

    assert _VALUE not in redacted


@_known_leak
@pytest.mark.parametrize("name", _NAMES)
def test_redact_command_masks_compound_secret_assignment(name: str) -> None:
    redacted = redact_command(f"{name}={_VALUE} python app.py", policy="secrets")

    assert _VALUE not in str(redacted)
