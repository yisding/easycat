"""Tests for optional-dependency helpers in easycat._extras."""

from __future__ import annotations

import sys
import types

import pytest

from easycat._extras import require_module


def _assert_extra_hint(message: str, extra: str) -> None:
    assert f"uv add 'easycat[{extra}]'" in message
    assert f"From the EasyCat repo, use: uv sync --extra {extra} --group dev" in message


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


def test_sounddevice_load_error_names_portaudio_system_package(monkeypatch) -> None:
    def raise_portaudio_error(_name: str) -> None:
        raise OSError("PortAudio library not found")

    monkeypatch.setattr(
        "easycat._extras.importlib.util.find_spec",
        lambda name: types.SimpleNamespace(name=name),
    )
    monkeypatch.setattr(
        "easycat._extras.importlib.import_module",
        raise_portaudio_error,
    )

    with pytest.raises(ImportError) as exc_info:
        require_module("sounddevice", extra="local", purpose="LocalTransport audio I/O")

    message = str(exc_info.value)
    assert "PortAudio library not found" in message
    assert "sudo apt-get install libportaudio2" in message
    assert "brew install portaudio" in message
    assert "uv add 'easycat[local]'" not in message


# ── DX2 PR2: startup names the same code as plan and doctor ───────────


def test_require_module_missing_extra_carries_e202(monkeypatch) -> None:
    """E-7: a missing selected extra raises the code doctor already reports.

    The exception TYPE and its message are unchanged — only ``code``/``context``
    and a note are attached — so every ``except ImportError`` in the tree and in
    user code keeps working while ``easycat plan --json``'s ``issues``,
    ``easycat doctor``'s ``extra_webrtc`` row, and the startup raise all name
    ``EASYCAT_E202``.
    """
    monkeypatch.setattr("easycat._extras.importlib.util.find_spec", lambda _name: None)

    with pytest.raises(ImportError) as exc_info:
        require_module("easycat_missing_aiortc_probe", extra="webrtc", purpose="WebRTC transport")

    assert type(exc_info.value) is ImportError
    assert exc_info.value.code == "EASYCAT_E202"
    assert exc_info.value.context["extra"] == "webrtc"
    assert "EASYCAT_E202" in " ".join(getattr(exc_info.value, "__notes__", ()))
    _assert_extra_hint(str(exc_info.value), "webrtc")


def test_require_module_without_an_extra_is_not_coded(monkeypatch) -> None:
    """No extra is named, so there is nothing to install and nothing to code."""
    monkeypatch.setattr("easycat._extras.importlib.util.find_spec", lambda _name: None)

    with pytest.raises(ImportError) as exc_info:
        require_module("easycat_missing_uncoded_probe")

    assert not hasattr(exc_info.value, "code")


def test_sounddevice_load_error_is_not_tagged_as_a_missing_extra(monkeypatch) -> None:
    """The ``local`` extra IS installed; doctor reports this as EASYCAT_E209."""
    monkeypatch.setattr(
        "easycat._extras.importlib.util.find_spec",
        lambda name: types.SimpleNamespace(name=name),
    )

    def raise_portaudio_error(_name: str) -> None:
        raise OSError("PortAudio library not found")

    monkeypatch.setattr("easycat._extras.importlib.import_module", raise_portaudio_error)

    with pytest.raises(ImportError) as exc_info:
        require_module("sounddevice", extra="local", purpose="LocalTransport audio I/O")

    assert not hasattr(exc_info.value, "code")
    assert "sudo apt-get install libportaudio2" in str(exc_info.value)
