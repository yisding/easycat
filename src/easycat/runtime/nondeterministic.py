"""Canonical set of journal-record fields that are non-deterministic.

Lives at runtime scope because both :mod:`easycat.stages.base` and
:mod:`easycat.runtime.replay` need it and neither can import from the
other without a circular dependency.  Keeping this one-line-per-field
set in its own module keeps the lazy import graph acyclic.
"""

from __future__ import annotations

NONDETERMINISTIC_FIELDS: frozenset[str] = frozenset(
    {
        "timing.wall_ns",
        "timing.cpu_ns",
        "timing.mono_ns",
        "recorded_at_monotonic_ns",
        "recorded_at_utc",
        "cursor.entered_at",
        "cursor.exited_at",
    }
)

__all__ = ["NONDETERMINISTIC_FIELDS"]
