"""EasyCat pytest plugin.

Registered under ``[project.entry-points."pytest11"]`` so ``pytest``
picks it up automatically as soon as ``easycat`` is installed in the
test environment. The plugin registers the ``contract`` marker used by the
installable provider suites and ships a lightweight ``easycat_bundle`` fixture,
which proxies to :func:`easycat.debug.testing.load_bundle`.

Authors who want behavioural assertions import the helpers from
:mod:`easycat.debug.testing` directly. Beyond marker registration, the plugin
only makes bundle loading ergonomic (``def test_regression(easycat_bundle):``
instead of a boilerplate fixture in every test file).
"""

from __future__ import annotations

from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Register markers required by EasyCat's installable testing surfaces."""
    config.addinivalue_line(
        "markers",
        "contract: EasyCat provider, protocol, or bridge contract test",
    )


@pytest.fixture
def easycat_bundle():
    """Return a loader callable: ``bundle = easycat_bundle(path)``.

    Implemented as a factory fixture rather than a direct bundle
    instance so one test module can load multiple fixture bundles
    without re-declaring the fixture per path.
    """

    def _load(path: str | Path):
        from easycat.debug.testing import load_bundle

        return load_bundle(path)

    return _load
