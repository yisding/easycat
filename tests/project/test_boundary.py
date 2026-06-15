"""M6a boundary guards for ``easycat.project``.

M6a ships the manifest loader ONLY: it must not import the (M6b) planner, and
validating a manifest must not import the heavy provider/runtime SDK (the
"validate without importing heavy SDKs" loader responsibility).
"""

from __future__ import annotations

import sys


def test_importing_project_does_not_import_planner() -> None:
    for name in list(sys.modules):
        if name == "easycat.project" or name.startswith("easycat.project."):
            del sys.modules[name]
    sys.modules.pop("easycat.planning", None)

    import easycat.project  # noqa: F401

    leaked = [name for name in sys.modules if name.startswith("easycat.planning")]
    assert leaked == []


def test_validating_manifest_does_not_import_create_session() -> None:
    # Validation (parse_manifest with resolve_agent deferred) must not pull the
    # session factory or aiohttp/heavy SDK. Drop the markers, validate, assert.
    for name in ("easycat.config._factory",):
        sys.modules.pop(name, None)

    from easycat.project import parse_manifest

    manifest = parse_manifest(
        {
            "server": {"auth": "bearer-env:EASYCAT_SERVE_TOKEN"},
            "voice": {"default": {"transport": "webrtc", "agent": "python:app:create_agent"}},
        }
    )
    assert manifest.server.auth is not None
    # Parsing + the redacted dump stay SDK-free.
    manifest.to_redacted_dict()
    assert "easycat.config._factory" not in sys.modules
