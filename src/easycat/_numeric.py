"""Small numeric predicates shared by public configuration boundaries."""

from __future__ import annotations

import math
from typing import TypeGuard


def is_finite_number(value: object) -> TypeGuard[int | float]:
    """Return whether *value* is a finite built-in number, excluding booleans.

    ``math.isfinite`` converts integers to C doubles and raises
    :class:`OverflowError` for arbitrarily large integers. Public validators
    should reject those inputs with their documented ``ValueError`` instead of
    leaking that implementation detail.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False
