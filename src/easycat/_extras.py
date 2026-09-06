"""Helpers for optional dependencies and extras."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from types import ModuleType

PORTAUDIO_INSTALL_FIX = (
    "Install PortAudio first (Debian/Ubuntu: `sudo apt-get install libportaudio2`; "
    "macOS: `brew install portaudio`), then retry."
)


def _extra_install_hint(extra: str | None) -> str:
    if extra is None:
        return ""
    return (
        f" Install with: uv add 'easycat[{extra}]'. "
        f"From the EasyCat repo, use: uv sync --extra {extra} --group dev."
    )


def _coded_extra_error(exc: ImportError, extra: str | None) -> ImportError:
    """Tag a missing-extra ``ImportError`` with ``EASYCAT_E202`` (type unchanged).

    The message and the exception TYPE are untouched — every ``except
    ImportError`` in the tree and in user code keeps working — so the only
    change is that startup now names the same code ``easycat plan --json``'s
    ``issues`` and ``easycat doctor``'s ``extra_<name>`` row report for a
    missing selected extra. Unlike ``SystemExit``, ``ImportError`` has no
    load-bearing ``.code``, so :func:`~easycat.errors._attach_error_code` is the
    right mechanism here. ``easycat.errors`` is a stdlib-only leaf and the
    import lives in the failure branch, so ``import easycat._extras`` gains no
    weight.

    Untagged when no extra is named (there is nothing to install) and for the
    PortAudio ``OSError`` branch, where the extra IS installed and the system
    library is not — doctor reports that condition as ``EASYCAT_E209``.
    """
    if extra is not None:
        from easycat.errors import EASYCAT_E202, _attach_error_code

        _attach_error_code(exc, EASYCAT_E202(extra=extra))
    return exc


def require_module(
    module_name: str,
    *,
    extra: str | None = None,
    purpose: str | None = None,
) -> ModuleType:
    """Import and return a module or raise a clear missing-extra error."""
    if module_name not in sys.modules and importlib.util.find_spec(module_name) is None:
        hint = _extra_install_hint(extra)
        label = purpose or module_name
        raise _coded_extra_error(
            ImportError(f"{label} requires the {module_name} package.{hint}"), extra
        )
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        # The package itself is present (find_spec succeeded above) but importing it
        # failed because one of its own dependencies is missing or broken. This is
        # common for optional extras that pull native/transitive deps.
        hint = _extra_install_hint(extra)
        label = purpose or module_name
        raise _coded_extra_error(
            ImportError(
                f"{label} could not import {module_name} "
                f"(a dependency failed to load): {exc}.{hint}"
            ),
            extra,
        ) from exc
    except OSError as exc:
        portaudio = module_name == "sounddevice"
        hint = f" {PORTAUDIO_INSTALL_FIX}" if portaudio else _extra_install_hint(extra)
        label = purpose or module_name
        raise _coded_extra_error(
            ImportError(f"{label} could not load {module_name}: {exc}.{hint}"),
            None if portaudio else extra,
        ) from exc
