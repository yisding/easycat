"""``easycat.project`` — the ``easycat.toml`` manifest loader (M6a).

This package turns an ``easycat.toml`` into a typed, validated
:class:`ProjectManifest` that :class:`~easycat.server.VoiceServer` and the CLI
serve path consume. It is a *submodule* export (``import easycat.project``) and
does NOT count against the top-level ``easycat.__all__`` cap.

Milestone boundary (Phase 2 "Neo"):

* M6a (this milestone) adds the loader, the ``bearer-env:NAME`` env-reference
  grammar + the testable literal-secret rule (RAISES
  :data:`~easycat.errors.EASYCAT_E603`), the ``python:module:function`` agent
  resolver, the transport string shortcuts, and ``easycat.toml`` -> ``EasyConfig``
  profile conversion. The redacted dump (:meth:`ProjectManifest.to_redacted_dict`)
  ensures a resolved token never appears in logs / ``--json`` / ``/manifest``.
* M6b adds the provider planner (``easycat.planning``) and the readiness wiring;
  this package deliberately does NOT import the planner.

Import weight: validation pulls only ``tomllib`` + the SDK-free schema leaf and
``validation.redaction`` helpers. ``EasyConfig`` / ``create_session`` /
``BearerTokenAuth`` are imported lazily inside ``ProjectManifest`` methods, so
``import easycat.project`` pulls no heavy provider/runtime SDK.
"""

from __future__ import annotations

from easycat.project.loader import (
    DEFAULT_MANIFEST_NAME,
    ENV_MANIFEST_VAR,
    discover_manifest_path,
    load_manifest,
    parse_manifest,
)
from easycat.project.manifest import ProjectManifest, VoiceProjectManifest
from easycat.project.schema import (
    EnvReference,
    ProjectSection,
    ServerSection,
    VoiceProfile,
)

__all__ = [
    "DEFAULT_MANIFEST_NAME",
    "ENV_MANIFEST_VAR",
    "EnvReference",
    "ProjectManifest",
    "ProjectSection",
    "ServerSection",
    "VoiceProfile",
    "VoiceProjectManifest",
    "discover_manifest_path",
    "load_manifest",
    "parse_manifest",
]
