from __future__ import annotations

import pytest

from easycat.validation.redaction import redact_text


@pytest.mark.xfail(
    strict=True,
    reason="_HOME_PATH_RE only matches a home path after whitespace, '=', ':' or a quote",
)
@pytest.mark.parametrize(
    "text",
    [
        "see (/home/bob/x.py)",
        "file `/Users/alice/x.py`",
        "paths [/home/bob/x.py]",
        "a,/home/bob/x.py",
        "<path>/home/bob/x.py</path>",
    ],
)
def test_pii_redaction_hides_home_directory_user_after_punctuation(text: str) -> None:
    redacted = redact_text(text, policy="pii")

    assert "bob" not in redacted
    assert "alice" not in redacted


def test_pii_redaction_hides_home_directory_user_after_space() -> None:
    # Control: the same path is redacted when whitespace precedes it.
    assert redact_text("loaded from /home/bob/x.py", policy="pii") == "loaded from ~/x.py"
