"""Regression tests for the shared env-flag truthiness helper.

All boolean opt-in env flags route their parse through
:func:`easycat._env.is_truthy` so ``"0"``/``"false"``/``"no"``/``"off"`` are
consistently falsy and any other non-empty value is truthy. This asserts each
independent reader agrees with the shared helper for the canonical value set —
in particular ``_emergency_export_enabled``, which previously armed only on the
exact string ``"1"`` and silently ignored ``"true"``/``"yes"``.
"""

from __future__ import annotations

import pytest

from easycat._env import is_truthy


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", False),
        ("false", False),
        ("1", True),
        ("true", True),
        ("yes", True),
    ],
)
def test_is_truthy_matches_across_flag_readers(
    value: str, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert is_truthy(value) is expected

    from easycat.cli import serve
    from easycat.config import _factory
    from easycat.debugger import _autolaunch, dev

    monkeypatch.setenv("EASYCAT_DEV", value)
    assert serve._dev_mode_enabled() is expected
    assert dev.dev_mode_opted_in() is expected

    monkeypatch.setenv("EASYCAT_DEBUGGER_AUTOLAUNCH", value)
    assert _autolaunch._autolaunch_opted_in(False) is expected

    monkeypatch.setenv("EASYCAT_EMERGENCY_EXPORT", value)

    class _Cfg:
        observability = None

    assert _factory._emergency_export_enabled(_Cfg()) is expected
