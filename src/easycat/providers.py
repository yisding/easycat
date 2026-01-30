"""Provider Protocol interfaces for EasyCat.

All providers are defined as typing.Protocol classes so that implementations
use structural subtyping (duck typing) rather than requiring inheritance.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from easycat.audio_format import AudioChunk
from easycat.events import Event

# ── STT Provider ───────────────────────────────────────────────────


@runtime_checkable
class STTProvider(Protocol):
    """Speech-to-text provider interface.

    Providers stream audio in via `send_audio` and yield transcript events
    (STTPartial / STTFinal) from the async iterator returned by `start_stream`.
    """

    async def start_stream(self) -> AsyncIterator[Event]:
        """Begin a new STT stream session and return an iterator of transcript events."""
        ...

    async def send_audio(self, chunk: AudioChunk) -> None:
        """Send an audio chunk to the active STT stream."""
        ...

    async def end_stream(self) -> None:
        """Signal that no more audio will be sent for the current stream."""
        ...


# ── TTS Provider ───────────────────────────────────────────────────


@runtime_checkable
class TTSProvider(Protocol):
    """Text-to-speech provider interface.

    Call `synthesize` with text to get an async iterator of audio chunks.
    Call `stop` or `cancel` to halt synthesis (e.g. on barge-in).
    """

    def synthesize(self, text: str) -> AsyncIterator[AudioChunk]:
        """Synthesize text into streaming audio chunks."""
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
