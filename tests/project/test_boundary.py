"""M6a boundary guards for ``easycat.project``.

M6a ships the manifest loader ONLY: it must not import the (M6b) planner, and
validating a manifest must not import the heavy provider/runtime SDK (the
"validate without importing heavy SDKs" loader responsibility).
"""

from __future__ import annotations

import subprocess
import sys


def test_importing_project_does_not_import_planner() -> None:
    # Run in a FRESH subprocess (like the siblings in
    # ``tests/planning/test_boundary.py``) so the check observes a true
    # module-load. Doing this in-process would require evicting + re-importing
    # ``easycat.project.schema``, which mints a fresh ``VoiceProfile`` class and
    # would break ``isinstance`` for any sibling holding the original class.
    code = (
        "import sys; import easycat.project; "
        "print([m for m in sys.modules if m.startswith('easycat.planning')])"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]", result.stdout


def test_validating_manifest_does_not_import_create_session() -> None:
    # Validation (parse_manifest with resolve_agent deferred) must not pull the
    # session factory or aiohttp/heavy SDK. Run in a FRESH subprocess (like the
    # sibling above): popping ``easycat.config._factory`` from ``sys.modules``
    # in-process and letting it be re-imported mints a DUPLICATE module object,
    # which silently breaks ``monkeypatch.setattr``-based provider patching in
    # unrelated downstream tests (e.g. the quickstart e2e create_vad patch).
    code = (
        "import sys\n"
        "from easycat.project import parse_manifest\n"
        "manifest = parse_manifest({\n"
        "    'server': {'auth': 'bearer-env:EASYCAT_SERVE_TOKEN'},\n"
        "    'voice': {'default': {'transport': 'webrtc', "
        "'agent': 'python:app:create_agent'}},\n"
        "})\n"
        "assert manifest.server.auth is not None\n"
        "manifest.to_redacted_dict()\n"  # the redacted dump stays SDK-free too
        "loaded = sorted(m for m in sys.modules if m.startswith('easycat.config'))\n"
        "assert 'easycat.config._factory' not in sys.modules, loaded\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.returncode == 0, result.stderr
