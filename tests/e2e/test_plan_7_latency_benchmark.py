"""Plan 7: End-to-end latency benchmark under different configurations.

Measures the single most-important UX metric for a voice bot:
**time from user stops speaking -> first audio byte the user can hear**.

The metric is captured server-side via two EventBus timestamps:

- ``VADStopSpeaking.timestamp``  — VAD detected end-of-speech (or Smart
  Turn decided the turn is over).  This is "the moment we started
  generating a response."
- ``TTSAudio.timestamp`` of the FIRST ``TTSAudio`` event — the first
  chunk of synthesized response audio leaving the pipeline toward the
  user.  The user starts hearing the response within roughly one
  network RTT + one transport-frame boundary of this moment.

Both events use ``time.monotonic()`` so a simple subtraction yields
the latency in seconds.

## Conditions tested

| ID | debug  | NR  | AEC | Smart Turn | Notes |
|----|--------|-----|-----|------------|-------|
| B  | off    | off | off | off        | Fastest possible (baseline) |
| L  | light  | off | off | off        | +in-memory journal overhead |
| F  | full   | off | off | off        | +SQLite journal (crash-durable) |
| N  | off    | on  | off | off        | +RNNoise |
| A  | off    | off | on  | off        | +LiveKit AEC |
| S  | off    | off | off | on         | +Smart Turn (should REDUCE latency) |
| X  | full   | on  | on  | off        | Full stack, everything observable |

## Industry context

Per public benchmarks (Hamming/Dograh/Twilio/Gladia reports across
Pipecat, LiveKit, Vapi, Retell, ElevenLabs, Deepgram, 2025-2026):

- Human-conversation target: <300 ms (natural turn-taking)
- "Feels responsive" target: <800 ms
- Typical traditional STT->LLM->TTS pipelines: 1000-2000 ms
- Best-in-class streaming stacks: 500-800 ms
- Speech-to-speech models (e.g. GPT-4o Realtime): 160-400 ms

The LLM TTFT + TTS TTFB dominate (~90% of the loop), with streaming
STT effectively negligible.

## Reading the output

Latency is logged per condition at the end as ``[latency] <id>:
<ms> ms``.  CI assertions enforce only the outermost sanity bound
(< 8000 ms) — the goal of the test is to track / compare /
instrument, not to gate on a hard SLO.

Run with the usual ``OPENAI_API_KEY`` env var:

    OPENAI_API_KEY=... uv run pytest tests/e2e/test_plan_7_latency_benchmark.py -s -v
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import statistics
from dataclasses import dataclass, field
from typing import Any

import pytest

from easycat.events import AgentDelta, STTFinal, TTSAudio, VADStopSpeaking

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration_socket,
    pytest.mark.integration_live,
    pytest.mark.slow,
]


# ---------------------------------------------------------------------------
# Latency measurement scaffold
# ---------------------------------------------------------------------------


@dataclass
class LatencyProbe:
    """Captures the four key pipeline timestamps.

    Subscribes to:
      - ``VADStopSpeaking``  — user stopped
      - ``STTFinal``         — transcription finalized
      - ``AgentDelta``       — first LLM token emerged
      - ``TTSAudio``         — first audio byte about to leave
    """

    vad_stop_ts: float | None = None
    stt_final_ts: float | None = None
    first_agent_delta_ts: float | None = None
    first_tts_ts: float | None = None

    def reset(self) -> None:
        self.vad_stop_ts = None
        self.stt_final_ts = None
        self.first_agent_delta_ts = None
        self.first_tts_ts = None

    def on_vad_stop(self, event: VADStopSpeaking) -> None:
        if self.vad_stop_ts is None:
            self.vad_stop_ts = event.timestamp

    def on_stt_final(self, event: STTFinal) -> None:
        if self.stt_final_ts is None:
            self.stt_final_ts = event.timestamp

    def on_agent_delta(self, event: AgentDelta) -> None:
        if self.first_agent_delta_ts is None:
            self.first_agent_delta_ts = event.timestamp

    def on_tts_audio(self, event: TTSAudio) -> None:
        if self.first_tts_ts is None:
            self.first_tts_ts = event.timestamp

    def _diff_ms(self, a: float | None, b: float | None) -> float | None:
        if a is None or b is None:
            return None
        return (b - a) * 1000.0

    @property
    def stt_ms(self) -> float | None:
        """VAD-stop -> STT-final. STT latency."""
        return self._diff_ms(self.vad_stop_ts, self.stt_final_ts)

    @property
    def agent_ttft_ms(self) -> float | None:
        """STT-final -> first agent delta. LLM time-to-first-token."""
        return self._diff_ms(self.stt_final_ts, self.first_agent_delta_ts)

    @property
    def tts_ttfb_ms(self) -> float | None:
        """First agent delta -> first TTS audio. TTS time-to-first-byte."""
        return self._diff_ms(self.first_agent_delta_ts, self.first_tts_ts)

    @property
    def latency_ms(self) -> float | None:
        """VAD-stop -> first TTS audio. End-to-end."""
        return self._diff_ms(self.vad_stop_ts, self.first_tts_ts)


@dataclass
class StageBreakdown:
    stt: float | None = None
    agent_ttft: float | None = None
    tts_ttfb: float | None = None
    total: float | None = None


@dataclass
class ConditionResult:
    name: str
    samples_ms: list[float] = field(default_factory=list)
    stt_ms: list[float] = field(default_factory=list)
    agent_ttft_ms: list[float] = field(default_factory=list)
    tts_ttfb_ms: list[float] = field(default_factory=list)

    def add(self, breakdown: StageBreakdown) -> None:
        if breakdown.total is not None:
            self.samples_ms.append(breakdown.total)
        if breakdown.stt is not None:
            self.stt_ms.append(breakdown.stt)
        if breakdown.agent_ttft is not None:
            self.agent_ttft_ms.append(breakdown.agent_ttft)
        if breakdown.tts_ttfb is not None:
            self.tts_ttfb_ms.append(breakdown.tts_ttfb)

    @staticmethod
    def _median(xs: list[float]) -> float:
        return statistics.median(xs) if xs else float("nan")

    @property
    def median_ms(self) -> float:
        return self._median(self.samples_ms)

    @property
    def stt_median_ms(self) -> float:
        return self._median(self.stt_ms)

    @property
    def agent_ttft_median_ms(self) -> float:
        return self._median(self.agent_ttft_ms)

    @property
    def tts_ttfb_median_ms(self) -> float:
        return self._median(self.tts_ttfb_ms)

    @property
    def p90_ms(self) -> float:
        if not self.samples_ms:
            return float("nan")
        srt = sorted(self.samples_ms)
        idx = max(0, min(len(srt) - 1, int(round(0.9 * (len(srt) - 1)))))
        return srt[idx]

    @property
    def min_ms(self) -> float:
        return min(self.samples_ms) if self.samples_ms else float("nan")


# ---------------------------------------------------------------------------
# Session builder that lets us toggle each pipeline stage independently
# ---------------------------------------------------------------------------


def _build_session_with_flags(
    *,
    transport: Any,
    debug: str,
    enable_noise_reduction: bool,
    enable_echo_cancellation: bool,
    enable_smart_turn: bool,
):
    """Build a live session with fine-grained control over each stage."""
    from agents import Agent  # type: ignore[import-untyped]

    from easycat import EasyCatConfig, create_session
    from easycat.smart_turn import SmartTurnConfig

    api_key = os.environ["OPENAI_API_KEY"]
    agent = Agent(
        name="latency_probe",
        instructions="Reply in one short sentence.",
        model="gpt-4o-mini",
    )
    return create_session(
        EasyCatConfig(
            openai_api_key=api_key,
            transport=transport,
            agent=agent,
            debug=debug,  # type: ignore[arg-type]
            enable_noise_reduction=enable_noise_reduction,
            enable_echo_cancellation=enable_echo_cancellation,
            smart_turn=SmartTurnConfig(enabled=enable_smart_turn),
        )
    )


# ---------------------------------------------------------------------------
# Single-shot latency measurement
# ---------------------------------------------------------------------------


async def _measure_one_turn(
    *,
    ws_server_factory,
    voice_fixture_path: pathlib.Path,
    debug: str,
    enable_noise_reduction: bool,
    enable_echo_cancellation: bool,
    enable_smart_turn: bool,
) -> StageBreakdown:
    """Drive one voice turn and return a per-stage latency breakdown.

    Captures four timestamps: ``VADStopSpeaking``, ``STTFinal``, first
    ``AgentDelta``, first ``TTSAudio`` — and returns the three stage
    gaps plus the end-to-end total.

    Raises RuntimeError if we never got the first TTSAudio in time.
    """
    from easycat.transports.websocket import (
        WebSocketConnectionTransport,
        WebSocketTransportConfig,
    )
    from tests.e2e._clients import WSVoiceClient

    probe = LatencyProbe()

    async def builder(ws):  # type: ignore[no-untyped-def]
        transport = WebSocketConnectionTransport(ws, WebSocketTransportConfig())
        session = _build_session_with_flags(
            transport=transport,
            debug=debug,
            enable_noise_reduction=enable_noise_reduction,
            enable_echo_cancellation=enable_echo_cancellation,
            enable_smart_turn=enable_smart_turn,
        )
        session.subscribe_event(VADStopSpeaking, probe.on_vad_stop)
        session.subscribe_event(STTFinal, probe.on_stt_final)
        session.subscribe_event(AgentDelta, probe.on_agent_delta)
        session.subscribe_event(TTSAudio, probe.on_tts_audio)
        return session

    handle = await ws_server_factory(builder)
    speech = voice_fixture_path.read_bytes()

    async with WSVoiceClient(handle.url) as client:
        await client.wait_for_ready(timeout=5.0)
        await client.negotiate_config(sample_rate=16000)
        await client.send_pcm_realtime(speech, sample_rate=16000)
        await client.send_silence(seconds=1.0, sample_rate=16000)
        # Wait for TTS to start (not drain — we only care about first-byte latency).
        deadline = asyncio.get_event_loop().time() + 20.0
        while probe.first_tts_ts is None:
            if asyncio.get_event_loop().time() >= deadline:
                raise RuntimeError(
                    "no TTS audio within 20 s; pipeline likely didn't complete a turn"
                )
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)

    if probe.latency_ms is None:
        raise RuntimeError(
            f"probe incomplete: vad_stop={probe.vad_stop_ts}, first_tts={probe.first_tts_ts}"
        )
    return StageBreakdown(
        stt=probe.stt_ms,
        agent_ttft=probe.agent_ttft_ms,
        tts_ttfb=probe.tts_ttfb_ms,
        total=probe.latency_ms,
    )


# ---------------------------------------------------------------------------
# The benchmark
# ---------------------------------------------------------------------------


CONDITIONS: list[dict[str, Any]] = [
    {
        "id": "B",
        "label": "baseline (no debug/NR/AEC/smart-turn)",
        "debug": "off",
        "nr": False,
        "aec": False,
        "smart": False,
    },
    {
        "id": "L",
        "label": "+debug=light (in-memory journal)",
        "debug": "light",
        "nr": False,
        "aec": False,
        "smart": False,
    },
    {
        "id": "F",
        "label": "+debug=full (SQLite journal)",
        "debug": "full",
        "nr": False,
        "aec": False,
        "smart": False,
    },
    {
        "id": "N",
        "label": "+noise reduction (RNNoise)",
        "debug": "off",
        "nr": True,
        "aec": False,
        "smart": False,
    },
    {
        "id": "A",
        "label": "+echo cancellation (LiveKit)",
        "debug": "off",
        "nr": False,
        "aec": True,
        "smart": False,
    },
    {
        "id": "S",
        "label": "+smart turn detection",
        "debug": "off",
        "nr": False,
        "aec": False,
        "smart": True,
    },
    {
        "id": "X",
        "label": "full stack (debug=full + NR + AEC)",
        "debug": "full",
        "nr": True,
        "aec": True,
        "smart": False,
    },
]


# Three samples per condition keeps total runtime near ~3-5 minutes.
# Bump locally for tighter error bars.
_SAMPLES_PER_CONDITION = 3


async def test_latency_benchmark_by_pipeline_flags(
    voice_fixtures: dict[str, pathlib.Path],
    ws_server_factory,
) -> None:
    """Sweep the condition matrix and report per-condition latency."""
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY required")
    pytest.importorskip("agents")

    fixture = voice_fixtures["question"]

    results: list[ConditionResult] = []
    for cond in CONDITIONS:
        # Skip conditions whose backends aren't available.
        if cond["nr"]:
            try:
                import pyrnnoise  # noqa: F401
            except ImportError:
                continue
        if cond["aec"]:
            try:
                import livekit  # noqa: F401
            except ImportError:
                continue
        if cond["smart"]:
            try:
                import onnxruntime  # noqa: F401
            except ImportError:
                continue

        result = ConditionResult(name=f"{cond['id']}: {cond['label']}")
        for _ in range(_SAMPLES_PER_CONDITION):
            try:
                breakdown = await _measure_one_turn(
                    ws_server_factory=ws_server_factory,
                    voice_fixture_path=fixture,
                    debug=cond["debug"],
                    enable_noise_reduction=cond["nr"],
                    enable_echo_cancellation=cond["aec"],
                    enable_smart_turn=cond["smart"],
                )
                result.add(breakdown)
            except Exception as exc:  # noqa: BLE001
                print(f"[latency] {cond['id']} sample failed: {exc}")
        results.append(result)

    # ── Per-stage breakdown ──────────────────────────────────────
    print()
    print("═" * 92)
    print("End-to-end voice-loop latency, broken down by stage (all numbers are medians, ms)")
    print("  STT       = VADStop -> STTFinal")
    print("  Agent     = STTFinal -> first AgentDelta  (LLM time-to-first-token)")
    print("  TTS       = first AgentDelta -> first TTSAudio  (time-to-first-byte)")
    print("  Total     = VADStop -> first TTSAudio  (what the user perceives)")
    print("═" * 92)
    header = f"{'id':<4}{'condition':<40}{'STT':>8}{'Agent':>8}{'TTS':>8}{'Total':>8}{'(p90)':>8}"
    print(header)
    print("─" * 92)
    for r in results:
        short_id = r.name.split(":", 1)[0]
        label = r.name.split(":", 1)[1].strip() if ":" in r.name else r.name
        if not r.samples_ms:
            print(f"{short_id:<4}{label:<40}{'(skipped)':>40}")
            continue
        print(
            f"{short_id:<4}{label:<40}"
            f"{r.stt_median_ms:>7.0f} "
            f"{r.agent_ttft_median_ms:>7.0f} "
            f"{r.tts_ttfb_median_ms:>7.0f} "
            f"{r.median_ms:>7.0f} "
            f"{r.p90_ms:>7.0f}"
        )
    print("═" * 92)
    print("Industry context (public benchmarks, 2025-2026):")
    print("  human-conversation target:   <300 ms")
    print("  'feels responsive' target:   <800 ms")
    print("  Pipecat/LiveKit best-case:  500-800 ms")
    print("  Retell typical overhead:    50-100 ms added")
    print("  speech-to-speech models:    160-400 ms")
    print("Typical per-stage budgets (Gladia/Speechmatics/Deepgram guides):")
    print("  STT (streaming):  100-300 ms")
    print("  LLM TTFT:         100-500 ms")
    print("  TTS TTFB:         75-300 ms (ElevenLabs Flash 75-135 ms)")
    print("═" * 92)

    # ── Sanity assertions ─────────────────────────────────────────
    any_ran = [r for r in results if r.samples_ms]
    assert any_ran, "no latency samples collected"
    for r in any_ran:
        # Outer bound only; don't gate on specific SLOs.
        assert r.p90_ms < 8000.0, f"{r.name} p90={r.p90_ms:.0f} ms exceeds 8000 ms sanity bound"

    # ── Relative comparisons ──────────────────────────────────────
    by_id = {r.name.split(":", 1)[0]: r for r in results if r.samples_ms}
    if "B" in by_id and "F" in by_id:
        full_overhead = by_id["F"].median_ms - by_id["B"].median_ms
        print(
            f"[latency] debug=full journal overhead vs baseline: {full_overhead:+.0f} ms (median)"
        )
    if "B" in by_id and "L" in by_id:
        light_overhead = by_id["L"].median_ms - by_id["B"].median_ms
        print(
            f"[latency] debug=light journal overhead vs baseline: "
            f"{light_overhead:+.0f} ms (median)"
        )
    if "B" in by_id and "N" in by_id:
        nr_overhead = by_id["N"].median_ms - by_id["B"].median_ms
        print(f"[latency] noise-reduction overhead vs baseline: {nr_overhead:+.0f} ms (median)")
    if "B" in by_id and "A" in by_id:
        aec_overhead = by_id["A"].median_ms - by_id["B"].median_ms
        print(f"[latency] echo-cancellation overhead vs baseline: {aec_overhead:+.0f} ms (median)")
    if "B" in by_id and "S" in by_id:
        smart_delta = by_id["S"].median_ms - by_id["B"].median_ms
        # Smart turn SHOULD reduce latency (earlier end-of-speech detection).
        print(
            f"[latency] smart-turn delta vs baseline: {smart_delta:+.0f} ms (median) "
            f"{'(faster)' if smart_delta < 0 else '(slower — unexpected)'}"
        )
