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
used. ``register_hypothesis_profiles()`` is a no-op when Hypothesis is
not installed, so importing this module and wiring it into ``conftest.py``
never breaks a minimal environment. That safety does *not* extend to
collection: the ``*_property.py`` modules import Hypothesis at module
load, so any environment that collects them (the default / ``quick``
slice) must have Hypothesis installed -- it ships in the ``dev`` group and
is added explicitly to the wheel-only release-validation workflow.
"""

from __future__ import annotations

import os


def register_hypothesis_profiles() -> None:
    """Register and load the active Hypothesis profile (no-op if absent)."""
    try:
        from hypothesis import HealthCheck, settings
        from hypothesis.errors import InvalidArgument
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
    # An unregistered HYPOTHESIS_PROFILE (typo, or a slice name like "nightly")
    # would otherwise raise at conftest import time and abort collection of the
    # *entire* suite, not just the property tests. Fall back to "dev" instead.
    requested = os.environ.get("HYPOTHESIS_PROFILE", "dev")
    try:
        settings.load_profile(requested)
    except InvalidArgument:
        import warnings

        warnings.warn(
            f"Unknown HYPOTHESIS_PROFILE={requested!r}; falling back to 'dev' "
            "(valid profiles: dev, ci).",
            stacklevel=2,
        )
        settings.load_profile("dev")
