"""M6a boundary guards for ``easycat.project``.

M6a ships the manifest loader ONLY: it must not import the (M6b) planner, and
validating a manifest must not import the heavy provider/runtime SDK (the
"validate without importing heavy SDKs" loader responsibility).
"""

from __future__ import annotations

import sys


def test_importing_project_does_not_import_planner() -> None:
    # Snapshot every module we are about to evict so we can restore it. A bare
    # delete + re-import mints a fresh ``easycat.project.schema`` (and a fresh
    # ``VoiceProfile`` class object); leaving that in ``sys.modules`` corrupts a
    # sibling test that built a ``VoiceProfile`` from the original class
    # (``isinstance`` then fails on the duplicate). Restore the originals so this
    # guard stays isolated.
    evicted: dict[str, object] = {}
    for name in list(sys.modules):
        is_project = name == "easycat.project" or name.startswith("easycat.project.")
        is_planning = name.startswith("easycat.planning")
        # Drop ALL planning modules (top-level AND submodules) so leftover state
        # from a sibling test cannot make a fresh ``easycat.project`` import look
        # like it pulled the planner. M6b adds ``easycat.planning.*`` submodules,
        # so popping only the top-level name is no longer enough.
        if is_project or is_planning:
            evicted[name] = sys.modules.pop(name)

    try:
        import easycat.project  # noqa: F401

        leaked = [name for name in sys.modules if name.startswith("easycat.planning")]
        assert leaked == []
    finally:
        # Restore the original module objects so sibling tests keep the class
        # identities they imported at collection time.
        for name in list(sys.modules):
            if name == "easycat.project" or name.startswith("easycat.project."):
                del sys.modules[name]
            elif name.startswith("easycat.planning"):
                del sys.modules[name]
        sys.modules.update(evicted)


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
