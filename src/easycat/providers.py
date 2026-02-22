"""Provider Protocol interfaces for EasyCat.

All providers are defined as typing.Protocol classes so that implementations
use structural subtyping (duck typing) rather than requiring inheritance.

Providers produce provider-scoped events (STTEvent, TTSEvent) via async
iterators. The Session is the single place that maps these to EasyCat events.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from easycat.audio_format import AudioChunk
from easycat.events import Event, STTEvent, TTSEvent

# ── STT Provider ───────────────────────────────────────────────────


@runtime_checkable
class STTProvider(Protocol):
    """Speech-to-text provider interface.

    Providers stream audio in via `send_audio` and produce `STTEvent` objects
    via the `events()` async iterator. Session consumes these and emits
    EasyCat-level STTPartial/STTFinal events. Providers never emit EasyCat
    events directly.
    """

    async def start_stream(self) -> None:
        """Begin a new STT stream session."""
        ...

    async def send_audio(self, chunk: AudioChunk) -> None:
        """Send an audio chunk to the active STT stream."""
        ...

    async def end_stream(self) -> None:
        """Signal that no more audio will be sent for the current stream."""
        ...

    def events(self) -> AsyncIterator[STTEvent]:
        """Return an async iterator of provider-scoped STT events."""
        ...


# ── TTS Provider ───────────────────────────────────────────────────


@runtime_checkable
class TTSProvider(Protocol):
    """Text-to-speech provider interface.

    Call `synthesize` with text to get an async iterator of TTSEvent objects.
    Session maps these to EasyCat-level TTSAudio/TTSMarkers events.
    """

    def synthesize(self, text: str) -> AsyncIterator[TTSEvent]:
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
class VADProvider(Protocol):
    """Voice activity detection provider interface.

    Process audio chunks and yield speech start/stop events.
    """

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        """Process an audio chunk and yield any VAD events (start/stop speaking)."""
        ...

    def configure(
        self,
        *,
        min_speech_duration_ms: int = 250,
        min_silence_duration_ms: int = 300,
        sensitivity: float = 0.5,
        pre_roll_ms: int = 100,
        post_roll_ms: int = 100,
    ) -> None:
        """Configure VAD thresholds and buffering parameters."""
        ...


# ── Noise Reducer ──────────────────────────────────────────────────


@runtime_checkable
class NoiseReducer(Protocol):
    """Noise reduction provider interface.

    Processes an audio chunk and returns a cleaned version.
    """

    async def process(self, chunk: AudioChunk) -> AudioChunk:
        """Process an audio chunk and return a noise-reduced version."""
        ...


# ── Transport ──────────────────────────────────────────────────────


@runtime_checkable
class Transport(Protocol):
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

    async def send_audio(self, chunk: AudioChunk) -> None:
        """Send an audio chunk to the remote end."""
        ...

    async def clear_audio(self) -> None:
        """Discard queued outbound audio (e.g. during barge-in).

        Transports that buffer outbound audio should drop pending data.
        The default implementation is a no-op for transports without
        outbound buffering.
        """
        ...


@runtime_checkable
class PlaybackAckTransport(Protocol):
    """Optional transport capability for explicit playback acknowledgements.

    Transports that support this can place a mark in their outbound playback
    queue and later emit an acknowledgement (for example via EventBus) when
    playback reaches that mark.
    """

    async def send_playback_mark(self, name: str | None = None) -> str:
        """Enqueue a playback mark and return the mark name used."""
        ...
