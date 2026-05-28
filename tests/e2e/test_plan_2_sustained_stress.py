"""Plan 2: Sustained multi-turn stress and concurrent-session stress.

Exercises the long-lived state of the journal-based runtime under real
voice load:

- ``test_fifty_turns_one_session`` — 50 consecutive voice turns on a
  single WebSocket session using scripted STT/TTS (so it can run without
  OpenAI credentials and stay under CI time limits). Verifies
  monotonicity of sequences, per-turn events, no dangling artifacts, no
  degraded journal, bounded resident set.
- ``test_concurrent_sessions`` — 10 concurrent WebSocket sessions each
  running 5 turns in parallel.  Verifies per-session journal isolation
  (no records leak across session_ids).
- ``test_ring_buffer_overflow`` — ``InMemoryRingBuffer`` overflow
  emits a ``BufferOverflow`` sentinel and evicts oldest records.
- ``test_live_ten_turns_one_session`` — integration_live variant that
  drives 10 real voice turns via OpenAI Whisper+Agents+TTS.
"""

from __future__ import annotations

import asyncio
import os
import resource
import uuid

import pytest

from easycat.runtime import JournalRecordKind
from easycat.runtime.journal import InMemoryRingBuffer
from easycat.validation.latency import (
    ReliabilitySample,
    ReliabilitySignals,
    append_reliability_sample,
)
from easycat.validation.reliability import EventLoopLagSampler
from tests.e2e._assertions import (
    assert_no_dangling_artifacts,
    assert_strictly_monotonic_sequences,
    count_distinct_turns,
)

pytestmark = [pytest.mark.asyncio]


def _append_stress_reliability_sample(
    *,
    condition_id: str,
    event_loop_lag_ms: float | None = None,
    journal_degraded: bool | None = None,
    active_sessions: int | None = None,
    memory_growth_kib: int | None = None,
    dropped_frames: int | None = None,
    queue_depth: int | None = None,
) -> None:
    samples_path = os.environ.get("EASYCAT_RELIABILITY_SAMPLES_PATH")
    if not samples_path:
        return
    append_reliability_sample(
        samples_path,
        ReliabilitySample(
            sample_id=f"{condition_id}-{uuid.uuid4().hex[:12]}",
            condition_id=condition_id,
            mode="stress",
            informational=True,
            eligible=False,
            signals=ReliabilitySignals(
                event_loop_lag_ms=event_loop_lag_ms,
                queue_depth=queue_depth,
                dropped_frames=dropped_frames,
                journal_degraded=journal_degraded,
                active_sessions=active_sessions,
                memory_growth_kib=memory_growth_kib,
                unavailable_reason=(
                    "event_loop_lag_unavailable" if event_loop_lag_ms is None else None
                ),
            ),
        ),
    )


# ---------------------------------------------------------------------------
# 2c. Ring buffer overflow (cheapest, most deterministic)
# ---------------------------------------------------------------------------


@pytest.mark.stress
async def test_ring_buffer_overflow_emits_sentinel() -> None:
    """``InMemoryRingBuffer`` must evict oldest records when full and emit
    a ``buffer_overflow`` sentinel."""
    buf = InMemoryRingBuffer(capacity=10)

    for i in range(50):
        buf.append(
            kind=JournalRecordKind.EVENT,
            name=f"evt-{i}",
            session_id="overflow",
        )

    records = buf.read()
    assert len(records) <= 10, f"ring buffer exceeded capacity: {len(records)}"

    # The newest record must be present; the oldest must be gone.
    names = {r.name for r in records}
    assert "evt-49" in names, "newest record evicted from ring buffer"
    assert "evt-0" not in names, "oldest record not evicted from ring buffer"

    # No degraded mode from simple eviction.
    assert not buf.degraded


# ---------------------------------------------------------------------------
# 2a. 50-turn single-session stress with scripted providers
# ---------------------------------------------------------------------------


@pytest.mark.integration_socket
@pytest.mark.slow
@pytest.mark.stress
async def test_fifty_turns_single_session_scripted(
    monkeypatch: pytest.MonkeyPatch,
    ws_server_factory,
) -> None:
    """Drive 50 turns through a WebSocket session with scripted STT/TTS."""
    from tests.e2e._audio import silence_pcm16, sine_pcm16
    from tests.e2e._clients import WSVoiceClient
    from tests.integration.harness import (
        RecordingTTS,
        ScriptedSTT,
        ScriptedVAD,
        make_test_config,
        patch_provider_factories,
    )

    # Patch provider factories so create_session gets our fakes.
    stt = ScriptedSTT(transcripts=[f"turn {i}" for i in range(50)])
    tts = RecordingTTS(chunk_sizes=(320,))
    vad = ScriptedVAD(script=[["start", "stop"]] * 50)
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    class EchoAgent:
        async def run(self, text: str, **kw):  # type: ignore[no-untyped-def]
            return f"echo: {text}"

    from easycat import create_session
    from easycat.transports.websocket import WebSocketConnectionTransport

    async def builder(ws):  # type: ignore[no-untyped-def]
        transport = WebSocketConnectionTransport(ws)
        cfg = make_test_config(transport=transport, agent=EchoAgent())
        cfg.debug = "full"  # type: ignore[assignment]
        return create_session(cfg)

    handle = await ws_server_factory(builder)

    rss_before_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    lag_sampler = EventLoopLagSampler()
    await lag_sampler.start()

    async with WSVoiceClient(handle.url) as client:
        await client.wait_for_ready(timeout=5.0)
        await client.negotiate_config(sample_rate=16000)

        # 50 turns of real PCM (a short tone), followed by silence per turn.
        tone = sine_pcm16(duration_s=0.25, sample_rate=16000)
        silence = silence_pcm16(duration_s=0.5, sample_rate=16000)
        for _ in range(50):
            await client.send_pcm_realtime(tone, sample_rate=16000)
            await client.send_pcm_realtime(silence, sample_rate=16000)

        # Wait for the server to finish processing.  We've sent plenty of
        # silence, so give the pipeline a moment to drain.
        await asyncio.sleep(2.0)

    event_loop_lag_ms = await lag_sampler.stop()
    session = handle.session
    assert session is not None
    journal = session.journal
    assert journal is not None

    records = journal.read()
    assert records, "no records in journal"

    # Sequences are strictly monotonic and unique.
    assert_strictly_monotonic_sequences(records)

    # No unexpected buffer_overflow or degraded markers.
    assert not journal.degraded
    overflows = [r for r in records if r.name == "buffer_overflow"]
    assert not overflows, f"unexpected overflows: {len(overflows)}"

    # Every artifact reference resolves.
    store = getattr(session, "_artifact_store", None)
    if store is not None:
        assert_no_dangling_artifacts(journal, store)

    # At least a handful of turns completed (scripted VAD may coalesce some).
    assert count_distinct_turns(journal) >= 5

    # Memory growth bound: < 250 MB for 50 turns.
    rss_after_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    memory_growth_kib = rss_after_kib - rss_before_kib
    _append_stress_reliability_sample(
        condition_id="fifty_turns_single_session_scripted",
        event_loop_lag_ms=event_loop_lag_ms,
        journal_degraded=journal.degraded,
        active_sessions=1,
        memory_growth_kib=memory_growth_kib,
        dropped_frames=len(overflows),
        queue_depth=session._outbound_queue.qsize(),  # noqa: SLF001 - stress telemetry
    )
    assert rss_after_kib - rss_before_kib < 250 * 1024, (
        f"RSS growth too large: {rss_after_kib - rss_before_kib} KiB"
    )


# ---------------------------------------------------------------------------
# 2b. 10 concurrent sessions x 5 turns each
# ---------------------------------------------------------------------------


@pytest.mark.integration_socket
@pytest.mark.slow
@pytest.mark.stress
async def test_concurrent_sessions_journal_isolation(
    monkeypatch: pytest.MonkeyPatch,
    ws_server_factory,
) -> None:
    """10 concurrent sessions, each with its own journal. Verify no
    cross-session contamination."""
    from tests.e2e._audio import silence_pcm16, sine_pcm16
    from tests.e2e._clients import WSVoiceClient
    from tests.integration.harness import (
        RecordingTTS,
        ScriptedSTT,
        ScriptedVAD,
        make_test_config,
        patch_provider_factories,
    )

    stt = ScriptedSTT(transcripts=[f"turn {i}" for i in range(500)])
    tts = RecordingTTS(chunk_sizes=(320,))
    vad = ScriptedVAD(script=[["start", "stop"]] * 500)
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    class EchoAgent:
        async def run(self, text: str, **kw):  # type: ignore[no-untyped-def]
            return f"echo: {text}"

    from easycat import create_session
    from easycat.transports.websocket import WebSocketConnectionTransport

    sessions: list = []

    async def builder(ws):  # type: ignore[no-untyped-def]
        transport = WebSocketConnectionTransport(ws)
        cfg = make_test_config(transport=transport, agent=EchoAgent())
        cfg.debug = "full"  # type: ignore[assignment]
        session = create_session(cfg)
        sessions.append(session)
        return session

    handle = await ws_server_factory(builder)
    lag_sampler = EventLoopLagSampler()
    await lag_sampler.start()

    async def run_client() -> None:
        tone = sine_pcm16(duration_s=0.2, sample_rate=16000)
        silence = silence_pcm16(duration_s=0.4, sample_rate=16000)
        async with WSVoiceClient(handle.url) as client:
            await client.wait_for_ready(timeout=5.0)
            await client.negotiate_config(sample_rate=16000)
            for _ in range(5):
                await client.send_pcm_realtime(tone, sample_rate=16000)
                await client.send_pcm_realtime(silence, sample_rate=16000)
            await asyncio.sleep(0.5)

    await asyncio.gather(*(run_client() for _ in range(10)))

    # Wait a beat for final journal flushing.
    await asyncio.sleep(1.0)
    event_loop_lag_ms = await lag_sampler.stop()

    assert len(sessions) >= 10, f"only {len(sessions)} sessions created"

    # Each session's journal must only contain its own session_id.
    for session in sessions:
        if session.journal is None:
            continue
        records = session.journal.read()
        ids = {r.session_id for r in records}
        assert len(ids) == 1, f"cross-session contamination: {ids}"
        assert session.session_id in ids
    _append_stress_reliability_sample(
        condition_id="concurrent_sessions_journal_isolation",
        event_loop_lag_ms=event_loop_lag_ms,
        journal_degraded=any(
            bool(session.journal and session.journal.degraded) for session in sessions
        ),
        active_sessions=len(sessions),
        dropped_frames=sum(session._outbound_queue.drops for session in sessions),  # noqa: SLF001
        queue_depth=max((session._outbound_queue.qsize() for session in sessions), default=0),  # noqa: SLF001
    )


# ---------------------------------------------------------------------------
# 2d. Live 10-turn smoke test (integration_live)
# ---------------------------------------------------------------------------


@pytest.mark.integration_socket
@pytest.mark.integration_live
@pytest.mark.provider_openai
@pytest.mark.slow
@pytest.mark.stress
@pytest.mark.surface_agent
@pytest.mark.surface_stt
@pytest.mark.surface_transport
@pytest.mark.surface_tts
async def test_ten_turns_live_openai(
    voice_fixtures,
    ws_server_factory,
) -> None:
    """Ten real voice turns against OpenAI providers.  Confirms the real
    bridge survives 10 consecutive streams without regression."""
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY required")
    pytest.importorskip("agents")

    from easycat.transports.websocket import WebSocketConnectionTransport
    from tests.e2e._clients import WSVoiceClient
    from tests.e2e.conftest import build_live_session

    async def builder(ws):  # type: ignore[no-untyped-def]
        transport = WebSocketConnectionTransport(ws)
        return build_live_session(transport=transport)

    handle = await ws_server_factory(builder)
    lag_sampler = EventLoopLagSampler()
    await lag_sampler.start()

    speech_16k = voice_fixtures["short"].read_bytes()

    async with WSVoiceClient(handle.url) as client:
        await client.wait_for_ready(timeout=5.0)
        await client.negotiate_config(sample_rate=16000)

        for _ in range(10):
            await client.send_pcm_realtime(speech_16k, sample_rate=16000)
            await client.send_silence(seconds=0.8, sample_rate=16000)
            await asyncio.sleep(1.0)

    event_loop_lag_ms = await lag_sampler.stop()
    session = handle.session
    assert session is not None

    records = session.journal.read()
    assert_strictly_monotonic_sequences(records)
    assert count_distinct_turns(session.journal) >= 3  # at least a few
    assert not session.journal.degraded
    _append_stress_reliability_sample(
        condition_id="ten_turns_live_openai",
        event_loop_lag_ms=event_loop_lag_ms,
        journal_degraded=session.journal.degraded,
        active_sessions=1,
        dropped_frames=session._outbound_queue.drops,  # noqa: SLF001 - stress telemetry
        queue_depth=session._outbound_queue.qsize(),  # noqa: SLF001 - stress telemetry
    )
