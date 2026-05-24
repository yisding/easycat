"""Helpers for optional dependencies and extras."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from types import ModuleType


def require_module(
    module_name: str,
    *,
    extra: str | None = None,
    purpose: str | None = None,
) -> ModuleType:
    """Import and return a module or raise a clear missing-extra error."""
    if module_name not in sys.modules and importlib.util.find_spec(module_name) is None:
        hint = f" Install with: uv add easycat[{extra}]." if extra else ""
        label = purpose or module_name
        raise ImportError(f"{label} requires the {module_name} package.{hint}")
    try:
        return importlib.import_module(module_name)
    except OSError as exc:
        hint = f" Install with: uv add easycat[{extra}]." if extra else ""
        label = purpose or module_name
        raise ImportError(f"{label} could not load {module_name}: {exc}.{hint}") from exc
