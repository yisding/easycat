"""Small numeric predicates shared by public configuration boundaries."""

from __future__ import annotations

import math
from numbers import Real
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


def is_finite_real(value: object) -> TypeGuard[float]:
    """Return whether *value* is a finite real number, excluding booleans.

    The :class:`numbers.Real` counterpart of :func:`is_finite_number`, for
    validators that also accept ``Fraction`` or NumPy scalars. An integer too
    large for a C double is rejected instead of raising :class:`OverflowError`.
    """
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False
