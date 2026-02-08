"""Helpers for optional dependencies and extras."""

from __future__ import annotations

import importlib
import importlib.util
from types import ModuleType


def require_module(
    module_name: str,
    *,
    extra: str | None = None,
    purpose: str | None = None,
) -> ModuleType:
    """Import and return a module or raise a clear missing-extra error."""
    if importlib.util.find_spec(module_name) is None:
        hint = f" Install with: uv add easycat[{extra}]." if extra else ""
        label = purpose or module_name
        raise ImportError(f"{label} requires the {module_name} package.{hint}")
    return importlib.import_module(module_name)
