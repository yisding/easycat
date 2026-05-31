"""Top-level configuration and session factories for EasyCat.

The "super easy" surface lives in :mod:`easycat.config.easy` (the
:class:`EasyConfig` / :class:`TextSessionConfig` dataclasses + the telephony
config trio) and :mod:`easycat.config._factory` (the
:func:`create_session` / :func:`create_text_session` factories). This package
``__init__`` is a thin re-export hub so ``easycat.config.X`` keeps working —
it deliberately does NOT import :mod:`easycat.config._telephony_wiring` at
module scope so touching :class:`EasyConfig` never pulls in the outbound
telephony stack.
"""

from __future__ import annotations

from ._factory import (
    _create_transport,
    _emit_provider_versions,
    _install_record_to_hook,
    _safe_config_ns,
    _transport_factories,
    create_session,
    create_text_session,
)
from .easy import (
    _VALID_MCP_SCHEMES,
    EasyConfig,
    EasyConfigError,
    OutboundCallConfig,
    TelephonyConfig,
    TextSessionConfig,
    TransportConfig,
    VoicemailDetectionConfig,
    _AgentSessionConfig,
    _inject_agent_runtime,
    _provider_display_name,
    _resolve_easycat_log_level,
    _validate_common,
)

# Public surface — what an app imports from ``easycat.config`` (and what the
# package root re-exports). The private symbols above stay importable for
# internal call sites and tests but are intentionally kept off ``__all__``.
__all__ = [
    "EasyConfig",
    "EasyConfigError",
    "OutboundCallConfig",
    "TelephonyConfig",
    "TextSessionConfig",
    "VoicemailDetectionConfig",
    "create_session",
    "create_text_session",
]


def _create_telephony_helpers(*args: object, **kwargs: object):
    """Thin re-export of the telephony helper builder.

    Kept here (rather than imported at module scope) so importing
    ``easycat.config`` never loads the outbound telephony stack. Internal
    callers and tests reach it via ``easycat.config._create_telephony_helpers``;
    it returns the typed :class:`~easycat.config._telephony_wiring.TelephonyHelpers`.
    """
    from ._telephony_wiring import create_telephony_helpers

    return create_telephony_helpers(*args, **kwargs)  # type: ignore[arg-type]


def __getattr__(name: str):
    """Pass ``OutboundCallManager`` through to the factory's PEP 562 attr.

    The lazy attribute itself lives on :mod:`easycat.config._factory` (so the
    telephony wiring resolves and tests patch it there). This pass-through
    keeps ``easycat.config.OutboundCallManager`` resolvable without importing
    the telephony stack at ``easycat.config`` import time.
    """
    if name == "OutboundCallManager":
        from . import _factory

        return _factory.OutboundCallManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
