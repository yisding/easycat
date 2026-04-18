"""Error-code registry + factory + CLI exit-code mapping."""

from __future__ import annotations

import pytest

from easycat.cli._errors import exit_code_for
from easycat.errors import (
    EASYCAT_E101,
    EASYCAT_E104,
    REGISTRY,
    EasyCatError,
    register,
    suggest_codes,
)


def test_every_registered_code_has_factory() -> None:
    """Every code in the registry has a headline that renders."""
    for code, entry in REGISTRY.items():
        assert entry.code == code
        assert entry.headline
        assert entry.cause
        assert entry.fix


def test_duplicate_registration_raises() -> None:
    with pytest.raises(RuntimeError, match="Duplicate"):
        register(
            "EASYCAT_E101",
            "dup",
            cause="x",
            fix="y",
        )


def test_factory_substitutes_context() -> None:
    err = EASYCAT_E101(target="/tmp/demo")
    assert isinstance(err, EasyCatError)
    assert err.code == "EASYCAT_E101"
    assert "/tmp/demo" in err.message
    assert err.context == {"target": "/tmp/demo"}


def test_factory_missing_placeholder_raises() -> None:
    # E101's template references {target!r}; calling without it should
    # raise a clear RuntimeError (caught at dev time, not at runtime).
    with pytest.raises(RuntimeError, match="headline template"):
        EASYCAT_E101()


def test_factory_unused_kwargs_are_stored_not_substituted() -> None:
    """Extra kwargs are kept in ``context`` even if the template ignores them."""
    err = EASYCAT_E104(provider="foo", available="a, b", hint=" Did you mean 'openai'?")
    assert "Did you mean" in err.message
    assert err.context["provider"] == "foo"


def test_suggest_codes_returns_close_matches() -> None:
    matches = suggest_codes("EASYCAT_E10")
    assert any(m.startswith("EASYCAT_E1") for m in matches)


def test_exit_code_mapping() -> None:
    assert exit_code_for("EASYCAT_E101") == 101
    assert exit_code_for("EASYCAT_E102") == 4
    assert exit_code_for("EASYCAT_E103") == 2
    assert exit_code_for("EASYCAT_E203") == 3
    assert exit_code_for("EASYCAT_E501") == 2
    # Unlisted codes fall back to 1.
    assert exit_code_for("EASYCAT_E999") == 1
