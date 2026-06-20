"""``easycat.planning`` — the provider/capability planner (M6b).

The planner is the static, side-effect-free counterpart to ``create_session``:
it resolves all seven pipeline roles (``stt`` / ``tts`` / ``vad`` / ``transport``
/ ``agent`` / ``noise_reducer`` / ``echo_canceller``) from an ``EasyConfig`` (or
a manifest ``VoiceProfile``) and reports missing env vars / missing optional
extras / incompatible combos WITHOUT instantiating any provider or importing a
heavy SDK. It feeds ``easycat plan --json``, the ``/plan`` endpoint, and the
M6b ``/health/ready`` manifest-loaded + plan-no-blocking-errors checks.

Metadata sourcing (per the M6b spec):

* **stt / tts** — REUSE the STT/TTS :class:`~easycat._provider_catalog.ProviderCatalog`.
* **vad / transport / agent / noise_reducer / echo_canceller** — NET-NEW
  declarative metadata in :mod:`easycat.planning.transport_registry` (there is
  NO catalog for these five roles; capabilities are declared net-new).

The planner-vs-``create_session`` PARITY TEST is the required gate: the
manifest/plan readiness checks may only ship once parity is green.

This is a *submodule* export (``import easycat.planning``); it does NOT count
against the top-level ``easycat.__all__`` cap (only top-level ``VoiceApp`` does).
Import weight stays light: provider catalogs are imported LAZILY inside
:func:`build_provider_plan`, so ``import easycat.planning`` pulls no aiohttp and
no heavy provider SDK.
"""

from __future__ import annotations

from easycat.planning.provider_plan import (
    ProviderPlan,
    ProviderSelection,
    Role,
    build_provider_plan,
)
from easycat.planning.transport_registry import (
    EXTRA_PROBE_MODULE,
    NON_CATALOG_ROLES,
    RoleBackend,
)

__all__ = [
    "EXTRA_PROBE_MODULE",
    "NON_CATALOG_ROLES",
    "ProviderPlan",
    "ProviderSelection",
    "Role",
    "RoleBackend",
    "build_provider_plan",
]
