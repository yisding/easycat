"""Hypothesis profile registration for the EasyCat test suite.

Imported once from ``tests/conftest.py`` so the profiles are registered
before any property test is collected. Two profiles are provided:

* ``dev`` (default) -- deterministic and fast: a fixed ``derandomize``
  seed so a green local run stays green and failures are reproducible,
  with a modest example budget so the suite stays snappy.
* ``ci`` -- thorough: no per-example deadline (CI machines are noisy and
  the resample/redaction functions touch optional native backends) and a
  larger example budget to find more edge cases.

Select a profile with the ``HYPOTHESIS_PROFILE`` environment variable
(e.g. ``HYPOTHESIS_PROFILE=ci uv run pytest``). When unset, ``dev`` is
used. Importing this module is a no-op if Hypothesis is not installed,
so the suite still collects in a minimal environment.
"""

from __future__ import annotations

import os


def register_hypothesis_profiles() -> None:
    """Register and load the active Hypothesis profile (no-op if absent)."""
    try:
        from hypothesis import HealthCheck, settings
    except ImportError:
        return

    settings.register_profile(
        "dev",
        max_examples=100,
        derandomize=True,
        deadline=None,
        print_blob=True,
    )
    settings.register_profile(
        "ci",
        max_examples=500,
        deadline=None,
        print_blob=True,
        suppress_health_check=[HealthCheck.too_slow],
    )
    settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
