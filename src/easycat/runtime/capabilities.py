"""Runtime capability protocols and helpers.

These protocols describe optional provider behavior that the runtime can use
without depending on concrete EasyCat implementation classes.
"""

from __future__ import annotations

from typing import Any, Protocol, cast, runtime_checkable


@runtime_checkable
class PlaybackAcknowledgements(Protocol):
    """Transport capability for explicit playback position marks."""

    async def send_playback_mark(self, name: str | None = None) -> str:
        """Enqueue a playback mark and return the mark name used."""
        ...


@runtime_checkable
class ClearAudioSupport(Protocol):
    """Transport capability for clearing buffered outbound audio."""

    async def clear_audio(self) -> None:
        """Discard queued outbound audio."""
        ...


@runtime_checkable
class TransportDeliveryReporting(Protocol):
    """Transport capability for deferred delivery accounting."""

    reports_audio_delivery: bool


@runtime_checkable
class IdentitySinkBinding(Protocol):
    """Transport capability for publishing caller identity updates."""

    def bind_identity_sink(self, sink: Any) -> None:
        """Register a callback that receives identity updates."""
        ...


@runtime_checkable
class PassthroughProvider(Protocol):
    """Marker for no-op or passthrough providers."""

    is_passthrough_provider: bool


@runtime_checkable
class HealthCheckable(Protocol):
    """Provider capability for active health checks."""

    async def health_check(self) -> bool:
        """Return True when the provider is healthy."""
        ...


@runtime_checkable
class AsyncCloseable(Protocol):
    """Provider capability for async close/teardown."""

    async def aclose(self) -> None:
        """Release async resources."""
        ...


@runtime_checkable
class Closeable(Protocol):
    """Provider capability for synchronous close/teardown."""

    def close(self) -> None:
        """Release resources."""
        ...


@runtime_checkable
class DefaultEchoCancellationPreference(Protocol):
    """Config/provider preference for transport-default echo cancellation."""

    default_echo_cancellation_enabled: bool


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


async def aclose_if_supported(provider: Any) -> None:
    """Close async resources when a provider exposes ``aclose``."""
    aclose = getattr(provider, "aclose", None)
    if callable(aclose):
        await aclose()


def default_echo_cancellation_enabled(provider_or_config: Any) -> bool:
    """Return a transport's default echo-cancellation preference."""
    return bool(getattr(provider_or_config, "default_echo_cancellation_enabled", False))
