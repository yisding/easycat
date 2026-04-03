"""Tests for the require_module helper."""

from __future__ import annotations

import json

import pytest

from easycat.extras import require_module


def test_require_module_returns_installed_module() -> None:
    mod = require_module("json")
    assert mod is json


def test_require_module_raises_for_missing() -> None:
    with pytest.raises(ImportError):
        require_module("nonexistent_module_xyz_42")


def test_require_module_error_includes_extra_hint() -> None:
    with pytest.raises(ImportError, match=r"uv add easycat\[special\]"):
        require_module("nonexistent_module_xyz_42", extra="special")


def test_require_module_error_includes_purpose() -> None:
    with pytest.raises(ImportError, match="Audio processing"):
        require_module("nonexistent_module_xyz_42", purpose="Audio processing")
