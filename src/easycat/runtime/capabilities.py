"""Runtime capability protocols and helpers.

These protocols describe optional provider behavior that the runtime can use
without depending on concrete EasyCat implementation classes.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from easycat.audio_format import AudioChunk
from easycat.runtime.scope import RuntimeScope

if TYPE_CHECKING:
    from easycat._dtmf_tones import DTMFPlaybackOutcome

PROTECTED_PLAYBACK_CHUNK_ATTR = "_easycat_protected_playback"
"""Chunk attribute that tags outbound audio with an active protected-playback token.

The router stamps ``setattr(chunk, PROTECTED_PLAYBACK_CHUNK_ATTR, token)`` on
every frame of a protected sequence (in-band DTMF tones) so a transport
holding untagged audio behind a deferred ``clear`` still sends those frames
immediately.
"""


@runtime_checkable
class PlaybackAcknowledgements(Protocol):
    """Transport capability for explicit playback position marks."""

    async def send_playback_mark(self, name: str | None = None) -> str:
        """Enqueue a playback mark and return the mark name used."""
        ...


@runtime_checkable
class ProtectedPlayback(Protocol):
    """Transport capability for audio that a barge-in ``clear`` must not cut.

    In-band DTMF tones are a key sequence: wiping half of them on the wire
    turns ``"1234#"`` into ``"12"``. A transport that implements this protocol
    defers outbound ``clear`` requests while a protected sequence plays, then
    writes the deferred clear once the far end has played it (so the clear
    only wipes audio queued *after* the tones).

    The expected call order is::

        # True: earlier audio left the buffer (played or discarded by a clear);
        # False: not confirmed (timed out, no stream is active, the stream
        # stopped or was replaced during the fence, or the fence mark could
        # not be sent), so earlier audio may still be queued.
        await t.wait_playout_fence(timeout_s=...)
        token = t.begin_protected_playback(max_duration_s=...)  # no await between
        ...send frames tagged with ``PROTECTED_PLAYBACK_CHUNK_ATTR = token``...
        done = await t.end_protected_playback(token)
        # done resolves "played" on the far-end ack of the completion mark,
        # "timeout" when the guard deadline passes first, or "disconnected"
        # when the mark cannot be sent, the stream ends or is replaced, or
        # the guard is aborted.

    ``abort_protected_playback`` releases the guard early (a rejected frame, or
    a forced session stop).

    A transport may also implement the OPTIONAL synchronous hook
    ``protected_playback_stream_id() -> object | None``, which returns the
    identity of the active stream (``None`` when no stream is active). It is
    not part of the required hooks checked by :func:`protected_playback`. When
    present, a burst is cancelled before any fence or frame if the identity
    changes between submission and arming the guard, so a burst queued behind
    earlier audio is never pressed on a replacement stream.
    """

    async def wait_playout_fence(self, *, timeout_s: float) -> bool:
        """Send a playout fence and wait until the far end echoes it.

        Returns ``True`` once everything queued before the fence has left the
        far end's buffer, either played or discarded by a ``clear`` (Twilio
        echoes pending marks when it clears), so ``True`` means the audio is no
        longer queued, not that it was heard. Returns ``False`` on timeout,
        when no stream is active, or when the stream stopped or was replaced
        during the wait (callers continue). ``clear`` requests issued while
        waiting are NOT deferred.
        """
        ...

    def begin_protected_playback(self, *, max_duration_s: float) -> object:
        """Arm the clear guard synchronously and return an opaque token.

        Raises :class:`RuntimeError` when no stream is active, and when the
        stream the preceding :meth:`wait_playout_fence` targeted is no longer
        the active one (it ended or was replaced during the fence), so a guard
        is never armed, and tones never sent, on a replacement stream. The
        guard self-expires after *max_duration_s* seconds.
        """
        ...

    async def end_protected_playback(
        self,
        token: object,
    ) -> asyncio.Future[DTMFPlaybackOutcome]:
        """Mark the end of the sequence and return a future for its outcome.

        The future resolves ``"played"`` once the far end acknowledges the
        whole sequence. It can also resolve with a non-played outcome:
        ``"timeout"`` when the guard's *max_duration_s* deadline passes first,
        or ``"disconnected"`` when the end marker cannot be sent, the stream
        ends or is replaced, or the guard is aborted.
        """
        ...

    def abort_protected_playback(self, token: object | None) -> None:
        """Release one guard early, or every guard when *token* is ``None``."""
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


def protected_playback(provider: Any) -> ProtectedPlayback | None:
    """Return the protected-playback capability when the transport supports it.

    Duck-typed like :func:`playback_acknowledgements`: every hook must be
    callable, so a transport that implements only part of the protocol is
    treated as not supporting it.
    """
    hooks = (
        "wait_playout_fence",
        "begin_protected_playback",
        "end_protected_playback",
        "abort_protected_playback",
    )
    if provider is not None and all(callable(getattr(provider, hook, None)) for hook in hooks):
        return cast(ProtectedPlayback, provider)
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


async def rollback_warmup_if_supported(provider: Any) -> None:
    """Release resources opened by warmup while keeping the provider reusable."""
    rollback = getattr(provider, "rollback_warmup", None)
    if not callable(rollback):
        return

    result = rollback()
    if inspect.isawaitable(result):
        await result


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
