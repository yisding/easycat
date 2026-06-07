"""Backward-compatible import path for EasyCat teaching recipes.

New reader-facing examples should import :mod:`easycat.recipes`. This
module remains for existing code that imports ``easycat.quick``.
"""

from __future__ import annotations

from easycat.recipes import _resolve_api_key as _resolve_api_key
from easycat.recipes import speak as speak
from easycat.recipes import transcribe_file as transcribe_file

__all__ = ["speak", "transcribe_file"]
