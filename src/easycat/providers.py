"""Provider Protocol interfaces for EasyCat.

All providers are defined as typing.Protocol classes so that implementations
use structural subtyping (duck typing) rather than requiring inheritance.

Providers produce provider-scoped events (STTEvent, TTSEvent) via async
iterators. The Session is the single place that maps these to EasyCat events.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from easycat.audio_format import AudioChunk
from easycat.events import Event, STTEvent, TTSEvent

if TYPE_CHECKING:
    from easycat.tts.input import TTSInput

# ── Versioned Provider ─────────────────────────────────────────────


@runtime_checkable
class VersionedProvider(Protocol):
    """Shared contract for providers that report their version info.

    The framework's journal records a ``provider_versions`` event by calling
    ``version_info()`` on every wired provider. Implementing this method is a
    de-facto part of the provider contract; mixing this Protocol into each
    provider interface makes that dependency explicit and type-checkable.

    Field conventions
    -----------------
    The returned mapping is free-form, but for postmortem diagnosis to be
    legible across providers the following keys carry an agreed meaning:

    - ``provider``: stable short name of the provider (e.g. ``"openai"``).
    - ``model``: the model that actually produced the output for this
      session. For providers that connect with a separate "connection"
      model distinct from the transcription/generation model, also report
      that under ``connection_model`` rather than dropping it.
    - ``api_version``: the provider HTTP/WS API version (e.g. ``"v1"``).
    - ``sdk_version``: the version of the transport library the *active
      mode* actually uses — ``"websockets"`` when the provider streams over
      a WebSocket, ``"httpx"`` when it issues HTTP requests. Providers with
      a runtime mode switch must mirror the branch they will actually take.
    """

    def version_info(self) -> dict[str, str]:
        """Return a mapping of version metadata for this provider."""
        ...


# ── STT Provider ───────────────────────────────────────────────────


@runtime_checkable
class STTProvider(VersionedProvider, Protocol):
    """Speech-to-text provider interface.

    Providers stream audio in via `send_audio` and produce `STTEvent` objects
    via the `events()` async iterator. Session consumes these and emits
    EasyCat-level STTPartial/STTFinal events. Providers never emit EasyCat
    events directly.

    Optional teardown hook
    ----------------------
    Session invokes an ``async def aclose(self) -> None`` (or sync/async
    ``close``) on the provider during ``stop()``/``close`` via
    ``runtime.capabilities.close_if_supported``. It is *not* a required
    member of this Protocol (so stubs like ``NoopSTT`` stay conformant and
    ``isinstance`` checks keep working), but providers that hold a socket
    or an ``httpx`` client should implement it to release those resources
    on session teardown rather than relying on garbage collection.
    """

    async def start_stream(self) -> None:
        """Begin a new STT stream session."""
        ...

    async def send_audio(self, chunk: AudioChunk) -> None:
        """Send an audio chunk to the active STT stream."""
        ...

    async def commit_segment(self) -> bool:
        """Finalize the current STT segment without ending the stream.

        Returns ``True`` when the provider accepted a segment commit request.
        Providers that do not support segmented commits should return ``False``.
        """
        ...

    async def end_stream(self) -> None:
        """Signal that no more audio will be sent for the current stream."""
        ...

    def events(self) -> AsyncIterator[STTEvent]:
        """Return an async iterator of provider-scoped STT events."""
        ...


# ── TTS Provider ───────────────────────────────────────────────────


@runtime_checkable
class TTSProvider(VersionedProvider, Protocol):
    """Text-to-speech provider interface.

    Call `synthesize` with text to get an async iterator of TTSEvent objects.
    Session maps these to EasyCat-level TTSAudio/TTSMarkers events. Note that
    ``TTSMarkers`` is a best-effort, debug-only event carrying the provider's
    *native* alignment shape (not a normalized cross-provider schema); see
    :class:`~easycat.events.TTSMarkers` for the contract.

    Optional teardown hook
    ----------------------
    Session invokes an ``async def aclose(self) -> None`` (or sync/async
    ``close``) on the provider during ``stop()``/``close`` via
    ``runtime.capabilities.close_if_supported``. It is *not* a required
    member of this Protocol (so stubs like ``NoopTTS`` stay conformant and
    ``isinstance`` checks keep working), but providers that hold a socket
    or an ``httpx`` client (e.g. ``OpenAITTS``, ``ElevenLabsTTS``) should
    implement it to release those resources on session teardown rather
    than relying on garbage collection.
    """

    @property
    def supports_ssml(self) -> bool:
        """Whether this provider accepts SSML input natively."""
        ...

    def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        """Synthesize text into streaming TTSEvent objects."""
        ...

    async def stop(self) -> None:
        """Gracefully stop the current synthesis."""
        ...

    async def cancel(self) -> None:
        """Immediately cancel synthesis and discard pending output."""
        ...


# ── VAD Provider ───────────────────────────────────────────────────


@runtime_checkable
class VADProvider(VersionedProvider, Protocol):
    """Voice activity detection provider interface.

    Process audio chunks and yield speech start/stop events.
    """

    def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        """Process an audio chunk and yield any VAD events (start/stop speaking)."""
        ...

    def configure(
        self,
        *,
        min_speech_duration_ms: int = 250,
        min_silence_duration_ms: int = 150,
        sensitivity: float = 0.5,
    ) -> None:
        """Configure VAD thresholds."""
        ...


# ── Noise Reducer ──────────────────────────────────────────────────


@runtime_checkable
class NoiseReducer(VersionedProvider, Protocol):
    """Noise reduction provider interface.

    Processes an audio chunk and returns a cleaned version.
    """

    async def process(self, chunk: AudioChunk) -> AudioChunk:
        """Process an audio chunk and return a noise-reduced version."""
        ...


# ── Echo Canceller ────────────────────────────────────────────────


@runtime_checkable
class EchoCanceller(VersionedProvider, Protocol):
    """Echo cancellation provider interface.

    Processes near-end (mic) audio and accepts far-end (speaker) reference.
    """

    async def process(self, chunk: AudioChunk) -> AudioChunk:
        """Process a near-end audio chunk and return an echo-cancelled version."""
        ...

    def feed_reference(self, chunk: AudioChunk) -> None:
        """Feed a far-end audio chunk as the AEC reference signal."""
        ...


# ── Transport ──────────────────────────────────────────────────────


@runtime_checkable
class TransportLike(Protocol):
    """Narrow structural contract for an already-constructed transport.

    This mirrors :class:`Transport`'s audio/connection surface but deliberately
    omits :meth:`VersionedProvider.version_info`. It exists so that the identity
    discrimination in ``_create_transport`` (distinguishing a pre-built transport
    instance from a transport *config*) does not silently reject third-party
    transports that satisfy the audio contract but predate ``version_info()``.
    """

    async def connect(self) -> None:
        """Establish the transport connection."""
        ...

    async def disconnect(self) -> None:
        """Close the transport connection."""
        ...

    def receive_audio(self) -> AsyncIterator[AudioChunk]:
        """Return an async iterator that yields incoming audio chunks."""
        ...

    async def send_audio(self, chunk: AudioChunk) -> bool:
        """Send an audio chunk to the remote end."""
        ...


@runtime_checkable
class Transport(VersionedProvider, Protocol):
    """Audio transport interface for sending/receiving audio.

    Handles connection lifecycle and bidirectional audio streaming.
    """

    async def connect(self) -> None:
        """Establish the transport connection."""
        ...

    async def disconnect(self) -> None:
        """Close the transport connection."""
        ...

    def receive_audio(self) -> AsyncIterator[AudioChunk]:
        """Return an async iterator that yields incoming audio chunks."""
        ...

    async def send_audio(self, chunk: AudioChunk) -> bool:
        """Send an audio chunk to the remote end.

        Returns ``True`` when the chunk was accepted for delivery and
        ``False`` when it was silently dropped (transport disconnected,
        no active peer, etc.).
        """
        ...

    async def clear_audio(self) -> None:
        """Discard queued outbound audio (e.g. during barge-in).

        Transports that buffer outbound audio should drop pending data.
        This method is optional: transports without outbound buffering may
        omit it entirely. The Session invokes it only through
        ``runtime.capabilities.clear_audio_if_supported()``, which skips
        transports that don't implement it.
        """
        ...
