"""Runtime capability protocols and helpers.

These protocols describe optional provider behavior that the runtime can use
without depending on concrete EasyCat implementation classes.
"""

from __future__ import annotations

import inspect
from typing import Any, Protocol, cast, runtime_checkable

from easycat.audio_format import AudioChunk
from easycat.runtime.scope import RuntimeScope


@runtime_checkable
class PlaybackAcknowledgements(Protocol):
    """Transport capability for explicit playback position marks."""

    async def send_playback_mark(self, name: str | None = None) -> str:
        """Enqueue a playback mark and return the mark name used."""
        ...


@runtime_checkable
class HealthCheckable(Protocol):
    """Provider capability for active health checks."""

    async def health_check(self) -> bool:
        """Return True when the provider is healthy."""
        ...


@runtime_checkable
class Warmupable(Protocol):
    """Provider capability for startup warmup work."""

    def warmup(self) -> Any:
        """Prime provider resources before first user traffic."""
        ...


@runtime_checkable
class RuntimeScopeBindable(Protocol):
    """Provider capability for attaching background work to a lifecycle tree."""

    def set_runtime_scope(self, parent: RuntimeScope, *, name: str) -> None:
        """Attach provider-owned runtime work beneath *parent*."""
        ...


def is_passthrough_provider(provider: Any) -> bool:
    """Return True when a provider explicitly marks itself as no-op/passthrough."""
    return bool(getattr(provider, "is_passthrough_provider", False))


def is_active_provider(provider: Any) -> bool:
    """Return True for a configured provider that is not marked passthrough."""
    return provider is not None and not is_passthrough_provider(provider)


def playback_acknowledgements(provider: Any) -> PlaybackAcknowledgements | None:
    """Return the playback-ack capability when supported."""
    if callable(getattr(provider, "send_playback_mark", None)):
        return cast(PlaybackAcknowledgements, provider)
    return None


def transport_reports_audio_delivery(provider: Any) -> bool:
    """Return whether the transport publishes deferred delivery events."""
    return bool(getattr(provider, "reports_audio_delivery", False))


def pending_playout_ms(provider: Any) -> float | None:
    """Return queued speaker-playout milliseconds when the transport reports it.

    Transports with a local playout buffer (e.g. ``LocalTransport``) can expose
    ``pending_playout_ms()`` so ``await_drain`` does not report the bot drained
    while the speaker buffer is still non-empty. Returns ``None`` for transports
    that lack the hook, keeping the check a strict no-op for them.
    """
    hook = getattr(provider, "pending_playout_ms", None)
    if callable(hook):
        return float(hook())
    return None


def drain_aec_reference_frames(provider: Any) -> list[AudioChunk | bytes] | None:
    """Drain the transport's playback-time far-end (speaker) reference frames.

    Transports that capture a far-end reference at playback time (e.g.
    ``LocalTransport`` and the WebRTC outbound source) can expose
    ``drain_aec_reference_frames()`` returning and draining typed
    :class:`AudioChunk` reference frames accumulated since the last call.
    ``AudioRouter`` drains them before the near-end mic frame so the echo
    canceller sees far-end audio before the matching near-end audio.

    Raw ``bytes`` remain accepted for backward compatibility with third-party
    transports that implemented the original optional hook; the router must
    infer their format from the near-end stream. Returns ``None`` for
    transports that lack the hook, keeping the check a strict no-op for them.
    """
    hook = getattr(provider, "drain_aec_reference_frames", None)
    if callable(hook):
        return list(hook())
    return None


async def clear_audio_if_supported(provider: Any) -> None:
    """Clear outbound audio only when the transport supports it."""
    clear_audio = getattr(provider, "clear_audio", None)
    if callable(clear_audio):
        await clear_audio()


def bind_identity_sink_if_supported(provider: Any, sink: Any) -> bool:
    """Bind an identity sink when the transport exposes that capability."""
    bind_identity_sink = getattr(provider, "bind_identity_sink", None)
    if callable(bind_identity_sink):
        bind_identity_sink(sink)
        return True
    return False


def health_checkable(provider: Any) -> HealthCheckable | None:
    """Return the health-check capability when supported."""
    if callable(getattr(provider, "health_check", None)):
        return cast(HealthCheckable, provider)
    return None


def warmupable(provider: Any) -> Warmupable | None:
    """Return the warmup capability when supported."""
    if callable(getattr(provider, "warmup", None)):
        return cast(Warmupable, provider)
    return None


async def warmup_if_supported(provider: Any) -> bool:
    """Run ``provider.warmup()`` when exposed, accepting sync or async hooks."""
    warmup_provider = warmupable(provider)
    if warmup_provider is None:
        return False

    result = warmup_provider.warmup()
    if inspect.isawaitable(result):
        await result
    return True


async def aclose_if_supported(provider: Any) -> None:
    """Close async resources when a provider exposes ``aclose``."""
    aclose = getattr(provider, "aclose", None)
    if callable(aclose):
        await aclose()


async def close_if_supported(provider: Any) -> None:
    """Close provider resources when ``aclose`` or ``close`` is exposed."""
    close = getattr(provider, "aclose", None)
    if not callable(close):
        close = getattr(provider, "close", None)
    if not callable(close):
        return

    result = close()
    if inspect.isawaitable(result):
        await result


def default_echo_cancellation_enabled(provider_or_config: Any) -> bool:
    """Return a transport's default echo-cancellation preference."""
    return bool(getattr(provider_or_config, "default_echo_cancellation_enabled", False))
