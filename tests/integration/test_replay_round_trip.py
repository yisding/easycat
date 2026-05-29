"""Cross-workstream round-trip (AC4.23).

Captures a real session turn through the stage stack, exports a debug
bundle, reloads it in the same process, and asserts that replay
surfaces through every WS1 / WS2 / WS3 / WS4 seam:

- the bundle carries the journal and artifact blobs WS1 produced,
- ``RunBundle.load()`` rebuilds the same records without talking to a
  live Session,
- ``bundle.replay(ReplaySpec(fidelity=ARTIFACT, timing='fast'))`` walks
  every record through ``ReplayRunner`` with ``REPLAY_IGNORE_FIELDS``
  masked, and
- ``bundle.replay_audio()`` reconstructs the outbound TTS byte stream
  byte-for-byte from the captured ``tts_frame`` records.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from easycat._turn_context import TurnContext
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.cancel import CancelToken
from easycat.debug.bundle import RunBundle
from easycat.debug.export import export_debug_bundle
from easycat.events import STTEvent, STTEventType, TTSEvent, TTSEventType
from easycat.runtime.artifacts import InMemoryArtifactStore
from easycat.runtime.journal import InMemoryRingBuffer
from easycat.runtime.replay import ReplayFidelity, ReplaySpec
from easycat.session._session import Session
from easycat.session._types import SessionConfig
from easycat.turn_manager import TurnManagerConfig


def _chunk(data: bytes = b"\x01\x02\x03\x04") -> AudioChunk:
    return AudioChunk(data=data, format=PCM16_MONO_16K)


class _StubTransport:
    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:  # type: ignore[override]
        if False:
            yield _chunk()

    async def send_audio(self, chunk: AudioChunk) -> bool:
        return True

    async def clear_audio(self) -> None:
        pass

    def version_info(self) -> dict[str, str]:
        return {"provider": "stub-transport"}


class _StubSTT:
    async def start_stream(self) -> None:
        pass

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def commit_segment(self) -> bool:
        return False

    async def end_stream(self) -> None:
        pass

    async def events(self) -> AsyncIterator[STTEvent]:  # type: ignore[override]
        yield STTEvent(type=STTEventType.FINAL, text="hello world")


class _ScriptedTTS:
    """TTS that yields two fixed audio frames for every payload."""

    CHUNK_A = b"AAAA" * 4
    CHUNK_B = b"BBBB" * 4

    @property
    def supports_ssml(self) -> bool:
        return False

    async def synthesize(self, payload: Any) -> AsyncIterator[TTSEvent]:  # type: ignore[override]
        yield TTSEvent(
            type=TTSEventType.AUDIO,
            audio=AudioChunk(data=self.CHUNK_A, format=PCM16_MONO_16K),
        )
        yield TTSEvent(
            type=TTSEventType.AUDIO,
            audio=AudioChunk(data=self.CHUNK_B, format=PCM16_MONO_16K),
        )

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass


class _EchoAgent:
    async def run(self, text: str) -> str:
        return f"reply: {text}"


def _build_session() -> tuple[Session, InMemoryRingBuffer, InMemoryArtifactStore]:
    journal = InMemoryRingBuffer(capacity=512)
    artifact_store = InMemoryArtifactStore()
    session = Session(
        SessionConfig(
            stt=_StubSTT(),
            tts=_ScriptedTTS(),
            transport=_StubTransport(),
            agent=_EchoAgent(),
            journal=journal,
            artifact_store=artifact_store,
            turn_manager_config=TurnManagerConfig(end_of_turn_silence_ms=1),
            enable_vad=False,
        )
    )
    session._turn = TurnContext("turn-rt", CancelToken())
    return session, journal, artifact_store


@pytest.mark.asyncio
async def test_ws4_cross_workstream_round_trip(tmp_path):
    """AC4.23 — capture, export, load, replay, diff."""
    session, journal, artifact_store = _build_session()

    # Drive one complete streaming turn end-to-end.  That produces stage
    # records for STT, agent, and TTS, plus tts_frame records with
    # output_refs into the artifact store.
    await session._run_streaming_agent("hello", token=None)

    # Export a bundle and reload it in the same process.
    bundle_path = tmp_path / "round_trip.zip"
    export_debug_bundle(session, str(bundle_path))
    bundle = RunBundle.load(bundle_path)

    # 1) The bundle's NDJSON survives round-trip: every live record
    # shows up in the loaded journal under the same sequence.
    live_seqs = {r.sequence for r in journal.read()}
    loaded_seqs = {int(r.get("sequence") or 0) for r in bundle.records()}
    assert live_seqs and live_seqs.issubset(loaded_seqs)

    # 2) ReplayRunner walks the records end-to-end under ARTIFACT
    # fidelity with fast timing; no side effects, no raises.
    result = bundle.replay(ReplaySpec(fidelity=ReplayFidelity.ARTIFACT, timing="fast"))
    assert result.fidelity_label is ReplayFidelity.ARTIFACT
    assert not result.side_effecting
    assert result.frames, "replay produced no frames"

    # 3) TTSStage.replay over the loaded cassette reproduces the
    # outbound byte stream byte-for-byte.
    tts_cassette = bundle.cassette_for_stage("tts")
    from easycat.stages.tts import TTSStage

    replay_bytes = TTSStage(_ScriptedTTS()).replay(
        ReplaySpec(fidelity=ReplayFidelity.ARTIFACT), tts_cassette
    )
    assert replay_bytes == _ScriptedTTS.CHUNK_A + _ScriptedTTS.CHUNK_B

    # 4) ``bundle.replay_audio`` is the high-level variant of the same
    # reconstruction and must agree.
    audio_chunks = bundle.replay_audio()
    concatenated = b"".join(chunk.data for chunk in audio_chunks)
    assert concatenated == _ScriptedTTS.CHUNK_A + _ScriptedTTS.CHUNK_B

    # 5) Manifest provider versions include the transport (which
    # implements ``version_info``) — this is the WS1 hook surviving
    # through WS4 export.
    assert "transport" in bundle.manifest.provider_versions
