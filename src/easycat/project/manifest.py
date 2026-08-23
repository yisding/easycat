"""``ProjectManifest`` — the parsed ``easycat.toml`` value object (M6a).

``ProjectManifest`` is the typed, validated result of loading an ``easycat.toml``
(the parsing/discovery lives in :mod:`easycat.project.loader`). It exposes three
things downstream callers need:

* :meth:`to_easyconfig` — convert a selected profile to an ``EasyConfig`` (the
  per-connection config the serve helpers build sessions from), resolving the
  ``python:module:function`` agent reference and the transport string shortcut.
* :meth:`resolve_auth` — resolve the ``[server] auth`` env reference to a
  :class:`~easycat.server.auth.BearerTokenAuth` (or ``None``), reading the token
  from the environment at call time so it never lives on the manifest.
* :meth:`to_redacted_dict` — a JSON-safe dump for logs / ``--json`` / ``/manifest``
  that routes every value through ``redact_value`` and shows the ``bearer-env:NAME``
  reference (never a resolved token).

The deliberately-named type is ``ProjectManifest`` — the single manifest name
(not a second ambiguous ``Manifest`` or ``VoiceProjectManifest``).

Import weight: ``EasyConfig``/``create_session``/``BearerTokenAuth`` are imported
LAZILY inside the methods that need them, so importing this module (and the
loader's validation path) pulls no heavy SDK — honoring the "validate without
importing heavy provider/runtime SDKs" loader responsibility.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from hmac import compare_digest
from importlib import import_module
from typing import TYPE_CHECKING, Any

from easycat.errors import EASYCAT_E602, EASYCAT_E605, EasyCatError
from easycat.project.schema import (
    TRANSPORT_PRESET,
    ProjectSection,
    ServerSection,
    VoiceProfile,
)
from easycat.validation.redaction import redact_value

if TYPE_CHECKING:
    from pathlib import Path

    from easycat.config import EasyConfig
    from easycat.server.auth import BearerTokenAuth

# The agent-reference grammar: ``python:module.path:attribute``. The attribute
# may itself be dotted (``python:pkg.mod:Outer.factory``) so a nested factory is
# reachable. The resolver imports lazily and tolerates either a zero-arg factory
# (called to build the agent) or an already-constructed agent object.
_PYTHON_AGENT_PREFIX = "python:"
_REDACTED_HOST = "[REDACTED_HOST]"


def _resolve_python_agent(reference: str) -> Any:
    """Resolve a ``python:module:attribute`` agent reference.

    The reference is imported lazily. When the resolved attribute is callable it
    is invoked with no arguments to build the agent (matching the
    ``create_agent`` factory convention); otherwise the attribute itself is used
    as the agent object. Any import/attribute/call failure RAISES
    :data:`~easycat.errors.EASYCAT_E605` with the underlying detail.
    """
    body = reference[len(_PYTHON_AGENT_PREFIX) :]
    module_path, separator, attribute_path = body.partition(":")
    if not separator or not module_path or not attribute_path:
        raise EASYCAT_E605(
            reference=reference,
            detail="expected the grammar 'python:module.path:attribute'",
        )
    try:
        module = import_module(module_path)
    except ImportError as exc:
        raise EASYCAT_E605(
            reference=reference, detail=f"could not import {module_path!r}: {exc}"
        ) from exc
    target: Any = module
    for part in attribute_path.split("."):
        try:
            target = getattr(target, part)
        except AttributeError as exc:
            # Name the failing SEGMENT, not the whole dotted path: for
            # ``Outer.factory`` where ``Outer`` resolves but ``factory`` does
            # not, "has no attribute 'Outer.factory'" is misleading.
            raise EASYCAT_E605(
                reference=reference,
                detail=f"{module_path!r} attribute path {attribute_path!r} failed at {part!r}",
            ) from exc
    if callable(target):
        try:
            return target()
        except Exception as exc:
            raise EASYCAT_E605(
                reference=reference,
                detail=f"calling the factory raised {type(exc).__name__}: {exc}",
            ) from exc
    return target


@dataclass(frozen=True)
class ProjectManifest:
    """A parsed, validated ``easycat.toml``.

    ``profiles`` maps each ``[voice.<name>]`` profile name to its
    :class:`~easycat.project.schema.VoiceProfile`. ``source_path`` is the
    absolute path the manifest was loaded from (relative profile paths resolve
    against its directory).
    """

    project: ProjectSection
    server: ServerSection
    profiles: Mapping[str, VoiceProfile]
    source_path: Path | None = None

    def profile(self, name: str) -> VoiceProfile:
        """Return the named profile or RAISE :data:`EASYCAT_E602`."""
        try:
            return self.profiles[name]
        except KeyError:
            available = ", ".join(sorted(self.profiles)) or "(none)"
            raise EASYCAT_E602(
                path=str(self.source_path or "easycat.toml"),
                problem=f"unknown profile {name!r}; available: {available}",
            )

    # ── EasyConfig conversion ────────────────────────────────────────

    def to_easyconfig(
        self,
        profile: str = "default",
        *,
        resolve_agent: bool = True,
    ) -> EasyConfig:
        """Convert ``profile`` to an :class:`~easycat.config.EasyConfig`.

        Resolves the ``python:module:function`` agent reference (unless
        ``resolve_agent=False``, used by validation/planner paths that must not
        import heavy provider SDKs) and the transport string shortcut to its
        preset. The ``stt``/``tts``/``vad``/``debug`` shortcut strings are
        forwarded verbatim — ``EasyConfig`` owns provider resolution.
        """
        from easycat.config import EasyConfig

        spec = self.profile(profile)
        kwargs: dict[str, Any] = {}
        if spec.agent is not None and resolve_agent:
            kwargs["agent"] = self.resolve_agent(profile)
        for field_name in ("stt", "tts", "debug"):
            value = getattr(spec, field_name)
            if value is not None:
                kwargs[field_name] = value
        # Resolve VAD here so manifest failures remain EASYCAT_E602-scoped while
        # still allowing installed ``easycat.vad_providers`` entry points.
        if spec.vad is not None:
            kwargs["vad"] = self._coerce_vad(spec.vad, profile)

        preset = TRANSPORT_PRESET[spec.transport]
        if preset == "browser":
            return EasyConfig.browser(**kwargs)
        if preset == "phone":
            # Both phone transports bind the one-time stream token through the
            # same ``bearer-env:NAME`` contract; only the secret's env var and
            # the transport config differ per provider.
            token_env_var = (
                "TELNYX_STREAM_TOKEN_SECRET"
                if spec.transport == "telnyx"
                else "TWILIO_STREAM_TOKEN_SECRET"
            )
            if spec.token is None:
                raise EASYCAT_E602(
                    path=str(self.source_path or "easycat.toml"),
                    problem=(
                        f"phone profile {profile!r} requires a token reference; "
                        f"set token = 'bearer-env:{token_env_var}'"
                    ),
                )
            token = spec.token.resolve(dict(os.environ))
            if spec.transport == "telnyx":
                from easycat.transports.telnyx_media import TelnyxTransportConfig

                kwargs["transport"] = TelnyxTransportConfig(
                    stream_token_validator=lambda candidate: compare_digest(candidate, token)
                )
                return EasyConfig.phone(provider="telnyx", **kwargs)
            from easycat.transports.twilio_media import TwilioTransportConfig

            kwargs["transport"] = TwilioTransportConfig(
                stream_token_validator=lambda candidate: compare_digest(candidate, token)
            )
            return EasyConfig.phone(**kwargs)
        if preset == "mic":
            return EasyConfig.mic(**kwargs)
        # websocket: bare EasyConfig with the websocket transport.
        from easycat.transports.websocket import WebSocketTransportConfig

        return EasyConfig(transport=WebSocketTransportConfig(), **kwargs)

    @staticmethod
    def _coerce_vad(shortcut: str, profile: str) -> Any:
        """Resolve a built-in or registered VAD shortcut.

        Keeping resolution in this manifest boundary preserves the structured
        :data:`EASYCAT_E602` error contract for unknown providers while entry
        points make third-party VADs name-selectable.
        """
        from easycat.vad import parse_vad_string

        try:
            return parse_vad_string(shortcut)
        except (EasyCatError, ValueError) as exc:
            raise EASYCAT_E602(
                path=f"[voice.{profile}]",
                problem=f"vad {shortcut!r} is not a known provider: {exc}",
            )

    def resolve_agent(self, profile: str = "default") -> Any:
        """Resolve the profile's ``python:module:function`` agent reference."""
        spec = self.profile(profile)
        if spec.agent is None:
            raise EASYCAT_E602(
                path=str(self.source_path or "easycat.toml"),
                problem=f"[voice.{profile}] has no agent to resolve",
            )
        if not spec.agent.startswith(_PYTHON_AGENT_PREFIX):
            raise EASYCAT_E605(
                reference=spec.agent,
                detail="agent references must use the 'python:module:attribute' grammar",
            )
        return _resolve_python_agent(spec.agent)

    # ── Auth resolution ──────────────────────────────────────────────

    def resolve_auth(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        allow_query_token: bool = False,
    ) -> BearerTokenAuth | None:
        """Resolve ``[server] auth`` to a :class:`BearerTokenAuth` or ``None``.

        The token is read from ``environ`` (defaulting to ``os.environ``) at call
        time — it is never stored on the manifest, so a dumped/echoed manifest
        cannot leak it. Returns ``None`` when no ``auth`` is configured.

        ``allow_query_token`` (default OFF) is forwarded to the built policy so a
        server constructed from this manifest can opt the ``?token=`` query auth
        on (the bundled browser WS client depends on it because browsers cannot
        set handshake headers).
        """
        if self.server.auth is None:
            return None
        from easycat.server.auth import BearerTokenAuth

        env = dict(environ) if environ is not None else dict(os.environ)
        token = self.server.auth.resolve(env)
        return BearerTokenAuth(token=token, allow_query_token=allow_query_token)

    # ── Redacted dump ────────────────────────────────────────────────

    def to_redacted_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dump that never contains a resolved token.

        Two layers protect this dump:

        #. By construction it carries ONLY the ``bearer-env:NAME`` reference for
           every ``auth``/``token`` field — the resolved token is never stored
           on the manifest, so it cannot be present here to begin with.
        #. Every value still routes through ``redact_value`` as a mandatory
           safety net, so even a malformed/literal-looking value would be
           replaced before it reaches a log / ``--json`` / ``/manifest`` surface.

        The reference is surfaced under an explicit ``*_ref`` key (NAME is a
        public env var name, not a secret) so ``redact_value``'s key-name policy
        does not over-redact the useful, safe reference into an opaque
        placeholder.

        The bind ``host`` can carry private addresses or internal DNS names in
        deployments where ``/manifest`` is reachable, so the public dump uses
        an explicit placeholder instead of exposing topology. The
        secret-bearing fields (``auth``/``token``) stay redacted: only the
        ``bearer-env:NAME`` reference is ever surfaced, never a resolved token.
        """
        raw: dict[str, Any] = {
            "project": {"name": self.project.name},
            "server": {
                "host": _REDACTED_HOST,
                "port": self.server.port,
                "max_sessions": self.server.max_sessions,
                "auth_ref": self.server.auth.reference if self.server.auth else None,
            },
            "profiles": {
                name: self._profile_to_dict(spec) for name, spec in sorted(self.profiles.items())
            },
        }
        if self.source_path is not None:
            raw["source_path"] = str(self.source_path)
        redacted: dict[str, Any] = redact_value(raw)  # type: ignore[assignment]
        return redacted

    @staticmethod
    def _profile_to_dict(spec: VoiceProfile) -> dict[str, Any]:
        return {
            "transport": spec.transport,
            "agent": spec.agent,
            "stt": spec.stt,
            "tts": spec.tts,
            "vad": spec.vad,
            "debug": spec.debug,
            "path": spec.path,
            "stream_url": spec.stream_url,
            # Only the env reference — never a resolved token. Surfaced under the
            # ``auth_ref`` key (consistent with the server's ``auth_ref``) so the
            # safe ``bearer-env:NAME`` reference is not over-redacted by the
            # ``token``-key-name policy in ``redact_value``.
            "auth_ref": spec.token.reference if spec.token else None,
        }


__all__ = [
    "ProjectManifest",
]
