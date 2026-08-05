"""Dependency-free credential predicates shared by config and validation."""

from __future__ import annotations

from typing import TypeGuard


def has_usable_credential(value: object) -> TypeGuard[str]:
    """Return whether ``value`` is a non-blank string credential."""
    return isinstance(value, str) and bool(value.strip())
