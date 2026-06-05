"""Tests for optional-dependency helpers in easycat._extras."""

from __future__ import annotations

import sys
import types

import pytest

from easycat._extras import require_module


def _assert_extra_hint(message: str, extra: str) -> None:
    assert f"uv add 'easycat[{extra}]'" in message
    assert f"uv sync --extra {extra}" in message


def test_require_module_returns_installed_module() -> None:
    assert require_module("json").__name__ == "json"


def test_require_module_missing_top_level_raises_with_hint() -> None:
    with pytest.raises(ImportError) as exc_info:
        require_module(
            "easycat_definitely_missing_pkg",
            extra="webrtc",
            purpose="WebRTC transport",
        )
    msg = str(exc_info.value)
    assert "WebRTC transport requires" in msg
    _assert_extra_hint(msg, "webrtc")


def test_require_module_transitive_import_error_wrapped_with_hint(monkeypatch) -> None:
    """A present module whose own import raises ModuleNotFoundError must still
    surface the helpful extra hint rather than propagating the raw error."""
    mod_name = "easycat_fake_extra_module"

    # Make find_spec see the module as present.
    spec = types.SimpleNamespace(name=mod_name)
    monkeypatch.setattr(
        "easycat._extras.importlib.util.find_spec",
        lambda name: spec if name == mod_name else None,
    )

    def fake_import_module(name: str):
        if name == mod_name:
            raise ModuleNotFoundError("No module named 'native_dep'")
        raise AssertionError(name)

    monkeypatch.setattr(
        "easycat._extras.importlib.import_module",
        fake_import_module,
    )

    with pytest.raises(ImportError) as exc_info:
        require_module(mod_name, extra="webrtc", purpose="WebRTC transport")

    msg = str(exc_info.value)
    assert "WebRTC transport could not import" in msg
    assert "dependency failed to load" in msg
    _assert_extra_hint(msg, "webrtc")
    assert isinstance(exc_info.value.__cause__, ModuleNotFoundError)


def test_require_module_os_error_wrapped(monkeypatch) -> None:
    mod_name = "easycat_fake_oserror_module"
    spec = types.SimpleNamespace(name=mod_name)
    monkeypatch.setattr(
        "easycat._extras.importlib.util.find_spec",
        lambda name: spec if name == mod_name else None,
    )

    def fake_import_module(name: str):
        raise OSError("cannot load shared object")

    monkeypatch.setattr(
        "easycat._extras.importlib.import_module",
        fake_import_module,
    )

    with pytest.raises(ImportError) as exc_info:
        require_module(mod_name, extra="webrtc")
    msg = str(exc_info.value)
    assert "could not load" in msg
    _assert_extra_hint(msg, "webrtc")

    # Avoid leaking the fake module into sys.modules for other tests.
    sys.modules.pop(mod_name, None)
