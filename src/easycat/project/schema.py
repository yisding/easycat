"""Manifest schema types + the testable env-reference/secret contract (M6a).

This module is the leaf of the ``easycat.project`` package: it declares the
dataclasses that mirror an ``easycat.toml`` (``ProjectSection`` / ``ServerSection``
/ ``VoiceProfile``) and the two enforced contracts the manifest loader leans on:

* the ``bearer-env:NAME`` env-reference grammar for every ``auth``/``token``
  field (a literal-looking secret RAISES :data:`~easycat.errors.EASYCAT_E603`,
  reusing ``redaction._SECRET_RE`` / ``contains_unredacted_sensitive_text`` so a
  newly registered secret shape is detected without touching this file); and
* the transport string shortcuts (``webrtc`` / ``websocket`` / ``twilio`` /
  ``local``) — the net-new declarative source for the manifest's ``transport``
  role (transport has no provider catalog; see the M6b planner note).

Import weight: this module pulls only ``dataclasses`` / ``re`` / ``typing`` and
(for the secret rule) the leaf ``easycat.validation.redaction`` helpers. It does
NOT import ``EasyConfig``/``create_session`` or any heavy provider SDK, so the
loader can validate a manifest without importing the runtime — exactly the
"validate without importing heavy SDKs" loader responsibility.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from easycat.errors import EASYCAT_E602, EASYCAT_E603, EASYCAT_E604
from easycat.validation.redaction import contains_unredacted_sensitive_text

# The env-reference grammar shared by every ``auth``/``token`` field. ``NAME``
# is a user-chosen env var identifier (the canonical example uses
# ``EASYCAT_SERVE_TOKEN`` for CLI parity, but any valid identifier is accepted).
_BEARER_ENV_PREFIX = "bearer-env:"
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# A redacted placeholder for any echoed/dumped reference. The loader never emits
# a resolved token; a dump shows the ``bearer-env:NAME`` reference verbatim
# (NAME is not a secret) and resolved values are dropped entirely.
REDACTED_AUTH_PLACEHOLDER = "[REDACTED_SECRET]"

# Transport string shortcuts — the net-new declarative metadata for the manifest
# ``transport`` role (M6a). Each maps a shortcut to the EasyConfig preset path
# the profile converts to. ``ProjectManifest.to_easyconfig`` consumes this;
# ``manifest.py`` owns the conversion so this leaf stays SDK-free.
TransportShortcut = Literal["webrtc", "websocket", "twilio", "telnyx", "local"]
TRANSPORT_SHORTCUTS: frozenset[str] = frozenset(
    {"webrtc", "websocket", "twilio", "telnyx", "local"}
)

# The EasyConfig preset each transport shortcut converts to. ``websocket`` has no
# dedicated preset (it builds a bare ``EasyConfig`` with the websocket transport),
# so it maps to ``None`` and the converter special-cases it.
TRANSPORT_PRESET: dict[str, str | None] = {
    "webrtc": "browser",
    "twilio": "phone",
    "telnyx": "phone",
    "local": "mic",
    "websocket": None,
}


@dataclass(frozen=True)
class EnvReference:
    """A resolved ``bearer-env:NAME`` reference.

    ``reference`` keeps the original ``bearer-env:NAME`` string (safe to echo —
    NAME is not a secret); ``env_var`` is the parsed variable name. The resolved
    token value is NEVER stored on this object so it cannot leak through an
    echoed/dumped manifest.
    """

    reference: str
    env_var: str

    def resolve(self, environ: dict[str, str]) -> str:
        """Return the token from ``environ`` or RAISE :data:`EASYCAT_E604`."""
        token = environ.get(self.env_var)
        if not token:
            raise EASYCAT_E604(reference=self.reference, var=self.env_var)
        return token


def parse_auth_reference(value: object, *, field_name: str) -> EnvReference:
    """Parse an ``auth``/``token`` field into an :class:`EnvReference`.

    Enforces the testable secret contract:

    * a non-string, or a string missing the ``bearer-env:`` prefix, or one whose
      value looks like a literal secret (per ``redaction._SECRET_RE`` /
      :func:`contains_unredacted_sensitive_text`) RAISES
      :data:`~easycat.errors.EASYCAT_E603`;
    * a malformed env-var name RAISES :data:`~easycat.errors.EASYCAT_E603` too.

    The token value is resolved later (at load time, against the environment) by
    :meth:`EnvReference.resolve` — never here.
    """
    if not isinstance(value, str) or not value.startswith(_BEARER_ENV_PREFIX):
        # A bare literal (or a non-string) — reject. Treat anything that is not
        # a valid env reference as a potential literal secret.
        raise EASYCAT_E603(field=field_name)
    name = value[len(_BEARER_ENV_PREFIX) :]
    if not _ENV_NAME_RE.match(name):
        raise EASYCAT_E603(field=field_name)
    # Belt-and-suspenders: even a well-formed reference must not smuggle a
    # literal secret in the NAME position (e.g. ``bearer-env:sk-...``). Reuse the
    # shared detector so a newly registered secret shape is caught here too.
    if contains_unredacted_sensitive_text(value):
        raise EASYCAT_E603(field=field_name)
    return EnvReference(reference=value, env_var=name)


@dataclass(frozen=True)
class ProjectSection:
    """The ``[project]`` table."""

    name: str | None = None


@dataclass(frozen=True)
class ServerSection:
    """The ``[server]`` table — process policy for :class:`VoiceServer`.

    ``auth`` is the parsed env reference (or ``None`` for an unauthenticated
    server); the resolved token never lives here. The remaining fields mirror
    :class:`~easycat.server.config.VoiceServerConfig` process policy.
    """

    host: str = "127.0.0.1"
    port: int = 8080
    max_sessions: int = 64
    auth: EnvReference | None = None


@dataclass(frozen=True)
class VoiceProfile:
    """A single ``[voice.<name>]`` profile.

    ``transport`` is a validated shortcut (one of :data:`TRANSPORT_SHORTCUTS`).
    ``agent`` is the raw reference string (resolved lazily by the
    ``python:module:function`` resolver at conversion time). ``stt``/``tts``/
    ``vad`` are provider shortcut strings forwarded to ``EasyConfig`` verbatim.
    ``token`` is the parsed env reference for transports that carry one
    (e.g. a twilio stream token); the resolved value never lives here.
    """

    name: str
    transport: TransportShortcut
    agent: str | None = None
    stt: str | None = None
    tts: str | None = None
    vad: str | None = None
    debug: str | None = None
    path: str | None = None
    stream_url: str | None = None
    token: EnvReference | None = None


def validate_transport(value: object, *, profile: str) -> TransportShortcut:
    """Return ``value`` as a validated transport shortcut or RAISE E602."""
    if not isinstance(value, str) or value not in TRANSPORT_SHORTCUTS:
        available = ", ".join(sorted(TRANSPORT_SHORTCUTS))
        raise EASYCAT_E602(
            path=f"[voice.{profile}]",
            problem=(f"transport {value!r} is not a known shortcut; use one of: {available}"),
        )
    return value  # type: ignore[return-value]


__all__ = [
    "REDACTED_AUTH_PLACEHOLDER",
    "TRANSPORT_PRESET",
    "TRANSPORT_SHORTCUTS",
    "EnvReference",
    "ProjectSection",
    "ServerSection",
    "TransportShortcut",
    "VoiceProfile",
    "parse_auth_reference",
    "validate_transport",
]
