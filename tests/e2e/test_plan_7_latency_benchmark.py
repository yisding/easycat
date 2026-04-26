"""Plan 7: End-to-end latency benchmark under different configurations.

Measures the single most-important UX metric for a voice bot:
**time from user stops speaking -> first audio byte the user can hear**.

The benchmark captures both client-side and server-side timestamps:

- ``client_speech_end_ts``       — when the client finishes pacing the
  final chunk of spoken audio. This approximates "the user stopped
  speaking."
- ``VADStopSpeaking.timestamp``  — when the server decides the turn is
  over and can start generating a response.
- ``AgentRequestStarted.timestamp`` — when EasyCat actually starts the
  agent/LLM request for the turn.
- ``TTSAudio.timestamp``         — the first synthesized audio chunk
  leaving the server toward the user.
- ``client_first_audio_ts``      — when the client receives the first
  audio frame of the reply.

All timestamps use ``time.monotonic()`` on one host, so simple
subtraction yields stage latencies in seconds.

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
<ms> ms``.

## SLO assertions

In addition to the outer sanity bound (< 8000 ms), the sweep enforces
per-stage SLOs so regressions fail the test instead of silently
widening the histogram:

- Baseline (``B``) p50 < ``EASYCAT_BENCHMARK_SLO_BASELINE_P50_MS``
  (default 5000 ms) and p90 < ``_P90_MS`` (default 6500 ms).  This
  includes no-smart-turn end-of-turn detection plus live-provider timing.
- Full stack (``X``) p50 < ``EASYCAT_BENCHMARK_SLO_FULL_STACK_P50_MS``
  (default 4500 ms).
- Per-feature median overhead versus baseline:
  - ``debug=light`` journal: < 400 ms
  - ``debug=full`` journal: < 800 ms
  - noise reduction: < 500 ms
  - echo cancellation: < 500 ms
- Smart turn is logged but only fails if it is *dramatically* slower
  than baseline (> 800 ms), since a given fixture may not trigger
  early termination.

All thresholds are overridable via env vars (see ``_SLO_*`` module
constants) so slow hardware or known regressions can loosen them
without editing the test.

Run with the usual ``OPENAI_API_KEY`` env var:

    OPENAI_API_KEY=... uv run pytest tests/e2e/test_plan_7_latency_benchmark.py -s -v
"""

from __future__ import annotations

import asyncio
import math
import os
import pathlib
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from easycat.events import AgentDelta, AgentRequestStarted, STTFinal, TTSAudio, VADStopSpeaking

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
    """Captures the server-side pipeline timestamps.

    Subscribes to:
      - ``VADStopSpeaking``  — server detected user stop
      - ``STTFinal``         — transcription finalized
      - ``AgentRequestStarted`` — EasyCat started the agent request
      - ``AgentDelta``       — first LLM token emerged
      - ``TTSAudio``         — first audio byte about to leave server
    """

    vad_stop_ts: float | None = None
    stt_final_ts: float | None = None
    agent_request_start_ts: float | None = None
    first_agent_delta_ts: float | None = None
    first_tts_ts: float | None = None

    def reset(self) -> None:
        self.vad_stop_ts = None
        self.stt_final_ts = None
        self.agent_request_start_ts = None
        self.first_agent_delta_ts = None
        self.first_tts_ts = None

    def on_vad_stop(self, event: VADStopSpeaking) -> None:
        if self.vad_stop_ts is None:
            self.vad_stop_ts = event.timestamp

    def on_stt_final(self, event: STTFinal) -> None:
        if self.stt_final_ts is None:
            self.stt_final_ts = event.timestamp

    def on_agent_request_started(self, event: AgentRequestStarted) -> None:
        if self.agent_request_start_ts is None:
            self.agent_request_start_ts = event.timestamp

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
    def stt_finalize_close_ms(self) -> float | None:
        """STT-final -> agent-request-start. Post-final STT close/cleanup time."""
        return self._diff_ms(self.stt_final_ts, self.agent_request_start_ts)

    @property
    def agent_request_start_ms(self) -> float | None:
        """VAD-stop -> agent-request-start. When the model request actually begins."""
        return self._diff_ms(self.vad_stop_ts, self.agent_request_start_ts)

    @property
    def llm_ttft_ms(self) -> float | None:
        """Agent-request-start -> first agent delta. Pure model TTFT."""
        return self._diff_ms(self.agent_request_start_ts, self.first_agent_delta_ts)

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
    detection: float | None = None
    stt: float | None = None
    stt_finalize_close: float | None = None
    agent_request_start: float | None = None
    llm_ttft: float | None = None
    tts_ttfb: float | None = None
    transport: float | None = None
    total: float | None = None


@dataclass
class ConditionResult:
    name: str
    samples_ms: list[float] = field(default_factory=list)
    detection_ms: list[float] = field(default_factory=list)
    stt_ms: list[float] = field(default_factory=list)
    stt_finalize_close_ms: list[float] = field(default_factory=list)
    agent_request_start_ms: list[float] = field(default_factory=list)
    llm_ttft_ms: list[float] = field(default_factory=list)
    tts_ttfb_ms: list[float] = field(default_factory=list)
    transport_ms: list[float] = field(default_factory=list)

    def add(self, breakdown: StageBreakdown) -> None:
        if breakdown.total is not None:
            self.samples_ms.append(breakdown.total)
        if breakdown.detection is not None:
            self.detection_ms.append(breakdown.detection)
        if breakdown.stt is not None:
            self.stt_ms.append(breakdown.stt)
        if breakdown.stt_finalize_close is not None:
            self.stt_finalize_close_ms.append(breakdown.stt_finalize_close)
        if breakdown.agent_request_start is not None:
            self.agent_request_start_ms.append(breakdown.agent_request_start)
        if breakdown.llm_ttft is not None:
            self.llm_ttft_ms.append(breakdown.llm_ttft)
        if breakdown.tts_ttfb is not None:
            self.tts_ttfb_ms.append(breakdown.tts_ttfb)
        if breakdown.transport is not None:
            self.transport_ms.append(breakdown.transport)

    @staticmethod
    def _median(xs: list[float]) -> float:
        return statistics.median(xs) if xs else float("nan")

    @property
    def median_ms(self) -> float:
        return self._median(self.samples_ms)

    @property
    def detection_median_ms(self) -> float:
        return self._median(self.detection_ms)

    @property
    def stt_median_ms(self) -> float:
        return self._median(self.stt_ms)

    @property
    def stt_finalize_close_median_ms(self) -> float:
        return self._median(self.stt_finalize_close_ms)

    @property
    def agent_request_start_median_ms(self) -> float:
        return self._median(self.agent_request_start_ms)

    @property
    def llm_ttft_median_ms(self) -> float:
        return self._median(self.llm_ttft_ms)

    @property
    def tts_ttfb_median_ms(self) -> float:
        return self._median(self.tts_ttfb_ms)

    @property
    def transport_median_ms(self) -> float:
        return self._median(self.transport_ms)

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
    stt_provider: str = "openai-realtime",
):
    """Build a live session with fine-grained control over each stage."""
    from agents import (
        Agent,  # type: ignore[import-untyped]
        ModelSettings,  # type: ignore[import-untyped]
    )
    from openai.types.shared import Reasoning

    import easycat.config as config_module
    from easycat import (
        EasyConfig,
        ElevenLabsSTTConfig,
        ElevenLabsTTSConfig,
        OpenAIRealtimeSTTConfig,
        OpenAITTSConfig,
    )
    from easycat.smart_turn import SmartTurnConfig
    from easycat.tts.elevenlabs_tts import ElevenLabsStreamMode

    api_key = os.environ["OPENAI_API_KEY"]
    llm_model = os.environ.get("EASYCAT_BENCHMARK_LLM_MODEL", "gpt-5.4")
    reasoning_effort = os.environ.get("EASYCAT_BENCHMARK_LLM_REASONING_EFFORT", "none").strip()
    provider_name = stt_provider.strip().lower()
    tts_provider = os.environ.get("EASYCAT_BENCHMARK_TTS_PROVIDER", "openai").strip().lower()
    tts_voice_id = os.environ.get("EASYCAT_BENCHMARK_TTS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL").strip()
    openai_tts_voice = os.environ.get("EASYCAT_BENCHMARK_TTS_VOICE", "alloy").strip()
    stt_config: OpenAIRealtimeSTTConfig | ElevenLabsSTTConfig | None
    if provider_name in {"openai", "openai-realtime"}:
        stt_config = OpenAIRealtimeSTTConfig(api_key=api_key)
    elif provider_name == "elevenlabs":
        elevenlabs_api_key = os.environ.get("ELEVENLABS_API_KEY")
        if not elevenlabs_api_key:
            pytest.skip("ELEVENLABS_API_KEY required for ElevenLabs latency benchmark")
        stt_config = ElevenLabsSTTConfig(api_key=elevenlabs_api_key, mode="realtime")
    else:
        raise ValueError(f"Unsupported STT provider for benchmark: {stt_provider!r}")

    transport_fmt = getattr(transport, "audio_format", None)
    if tts_provider == "openai":
        tts_model = os.environ.get("EASYCAT_BENCHMARK_TTS_MODEL", "gpt-4o-mini-tts").strip()
        tts_kwargs: dict[str, Any] = {
            "api_key": api_key,
            "model": tts_model,
            "voice": openai_tts_voice,
        }
        if transport_fmt is not None:
            tts_kwargs["output_format"] = transport_fmt
        tts_config = OpenAITTSConfig(**tts_kwargs)
    elif tts_provider == "elevenlabs":
        elevenlabs_api_key = os.environ.get("ELEVENLABS_API_KEY")
        if not elevenlabs_api_key:
            pytest.skip("ELEVENLABS_API_KEY required for ElevenLabs latency benchmark")
        elevenlabs_model = os.environ.get("EASYCAT_BENCHMARK_TTS_MODEL", "eleven_v3").strip()
        tts_kwargs = {
            "api_key": elevenlabs_api_key,
            "voice_id": tts_voice_id,
            "model_id": elevenlabs_model,
            "stream_mode": ElevenLabsStreamMode.HTTP,
            "output_format": "pcm_24000",
        }
        if transport_fmt is not None:
            tts_kwargs["output_format"] = f"pcm_{transport_fmt.sample_rate}"
            tts_kwargs["audio_format"] = transport_fmt
        tts_config = ElevenLabsTTSConfig(**tts_kwargs)
    else:
        raise ValueError(f"Unsupported TTS provider for benchmark: {tts_provider!r}")

    model_settings = ModelSettings()
    if reasoning_effort:
        model_settings = ModelSettings(reasoning=Reasoning(effort=reasoning_effort))
    agent = Agent(
        name="latency_probe",
        instructions="Reply in one short sentence.",
        model=llm_model,
        model_settings=model_settings,
    )
    config = EasyConfig(
        openai_api_key=api_key,
        transport=transport,
        agent=agent,
        stt=stt_config,
        tts=tts_config,
        debug=debug,  # type: ignore[arg-type]
        enable_noise_reduction=enable_noise_reduction,
        enable_echo_cancellation=enable_echo_cancellation,
        smart_turn=SmartTurnConfig(enabled=enable_smart_turn),
    )
    return config_module.create_session(config)


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
    stt_provider: str = "openai-realtime",
) -> StageBreakdown:
    """Drive one voice turn and return a per-stage latency breakdown.

    Captures client speech-end, ``VADStopSpeaking``, ``STTFinal``,
    ``AgentRequestStarted``, first ``AgentDelta``, first ``TTSAudio``,
    and first audio received by the client. Returns the stage gaps plus
    the user-perceived total.

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
            stt_provider=stt_provider,
        )
        session.subscribe_event(VADStopSpeaking, probe.on_vad_stop)
        session.subscribe_event(STTFinal, probe.on_stt_final)
        session.subscribe_event(AgentRequestStarted, probe.on_agent_request_started)
        session.subscribe_event(AgentDelta, probe.on_agent_delta)
        session.subscribe_event(TTSAudio, probe.on_tts_audio)
        return session

    handle = await ws_server_factory(builder)
    speech = voice_fixture_path.read_bytes()

    def _diff_ms(a: float | None, b: float | None) -> float | None:
        if a is None or b is None:
            return None
        return (b - a) * 1000.0

    async def _send_silence_until_turn_end(
        *,
        client: WSVoiceClient,
        sample_rate: int,
        max_silence_s: float = 1.0,
        chunk_ms: int = 20,
    ) -> None:
        bytes_per_ms = int((sample_rate * 2) / 1000)
        chunk_size = max(bytes_per_ms * chunk_ms, 320)
        silence_chunk = bytes(chunk_size)
        chunk_count = max(1, int(math.ceil((max_silence_s * 1000.0) / chunk_ms)))
        for _ in range(chunk_count):
            if probe.vad_stop_ts is not None:
                return
            if handle.exception is not None:
                raise RuntimeError(f"server failed during silence tail: {_sample_debug()}")
            await client.send_binary(silence_chunk)
            await asyncio.sleep(chunk_ms / 1000.0)

    def _sample_debug() -> str:
        session = getattr(handle, "session", None)
        session_running = getattr(session, "_is_running", None) if session is not None else None
        pipeline_task = getattr(session, "_pipeline_task", None) if session is not None else None
        pipeline_done = pipeline_task.done() if pipeline_task is not None else None
        return (
            f"server_exception={handle.exception!r}, "
            f"session_running={session_running}, "
            f"pipeline_done={pipeline_done}, "
            f"vad_stop={probe.vad_stop_ts}, "
            f"stt_final={probe.stt_final_ts}, "
            f"agent_request_start={probe.agent_request_start_ts}, "
            f"agent_delta={probe.first_agent_delta_ts}, "
            f"tts_audio={probe.first_tts_ts}"
        )

    try:
        async with WSVoiceClient(handle.url) as client:
            await client.wait_for_ready(timeout=5.0)
            await client.negotiate_config(sample_rate=16000)
            await client.send_pcm_realtime(speech, sample_rate=16000)
            client_speech_end_ts = time.monotonic()
            await _send_silence_until_turn_end(client=client, sample_rate=16000)
            # Wait for the first outbound audio frame to reach the client.
            deadline = asyncio.get_event_loop().time() + 20.0
            while probe.first_tts_ts is None or client.first_binary_ts is None:
                if handle.exception is not None:
                    raise RuntimeError(f"server failed during sample: {_sample_debug()}")
                if asyncio.get_event_loop().time() >= deadline:
                    raise RuntimeError(
                        "no reply audio within 20 s; "
                        f"pipeline likely didn't complete a turn ({_sample_debug()})"
                    )
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"benchmark sample setup/turn failed: server_exception={handle.exception!r}, "
            f"detail={exc}"
        ) from exc

    if probe.vad_stop_ts is None or probe.first_tts_ts is None or client.first_binary_ts is None:
        raise RuntimeError(
            "probe incomplete: "
            f"client_speech_end={client_speech_end_ts}, "
            f"vad_stop={probe.vad_stop_ts}, "
            f"stt_final={probe.stt_final_ts}, "
            f"agent_request_start={probe.agent_request_start_ts}, "
            f"first_agent_delta={probe.first_agent_delta_ts}, "
            f"first_tts={probe.first_tts_ts}, "
            f"client_first_audio={client.first_binary_ts}, "
            f"{_sample_debug()}"
        )
    detection_ms = _diff_ms(client_speech_end_ts, probe.vad_stop_ts)
    if detection_ms is not None and detection_ms < 0:
        raise RuntimeError(
            "probe invalid: server detected stop before client finished the utterance; "
            f"use a less pausey fixture or inspect turn splitting ({_sample_debug()})"
        )
    return StageBreakdown(
        detection=detection_ms,
        stt=probe.stt_ms,
        stt_finalize_close=probe.stt_finalize_close_ms,
        agent_request_start=probe.agent_request_start_ms,
        llm_ttft=probe.llm_ttft_ms,
        tts_ttfb=probe.tts_ttfb_ms,
        transport=_diff_ms(probe.first_tts_ts, client.first_binary_ts),
        total=_diff_ms(client_speech_end_ts, client.first_binary_ts),
    )


async def test_single_full_stack_latency_probe(
    voice_fixtures: dict[str, pathlib.Path],
    ws_server_factory,
) -> None:
    """One live end-to-end turn with the full stack enabled.

    Intended as a quick smoke probe before the full sweep:
    VAD, Smart Turn, noise reduction, echo cancellation, OpenAI STT,
    OpenAI Agents, and OpenAI TTS all participate in the loop.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY required")
    pytest.importorskip("agents")
    stt_provider = os.environ.get("EASYCAT_BENCHMARK_STT_PROVIDER", "openai-realtime")

    breakdown = await _measure_one_turn(
        ws_server_factory=ws_server_factory,
        voice_fixture_path=voice_fixtures["question"],
        debug="full",
        enable_noise_reduction=True,
        enable_echo_cancellation=True,
        enable_smart_turn=True,
        stt_provider=stt_provider,
    )

    print()
    print(f"[single-probe] full stack ({stt_provider}, debug=full + NR + AEC + Smart Turn)")
    print(f"[single-probe] detect: {_format_ms(breakdown.detection)}")
    print(f"[single-probe] stt: {_format_ms(breakdown.stt)}")
    print(f"[single-probe] stt-close: {_format_ms(breakdown.stt_finalize_close)}")
    print(f"[single-probe] agent-start: {_format_ms(breakdown.agent_request_start)}")
    print(f"[single-probe] llm: {_format_ms(breakdown.llm_ttft)}")
    print(f"[single-probe] tts: {_format_ms(breakdown.tts_ttfb)}")
    print(f"[single-probe] wire: {_format_ms(breakdown.transport)}")
    print(f"[single-probe] total: {_format_ms(breakdown.total)}")

    assert breakdown.total is not None, "single full-stack probe did not produce a latency sample"
    assert breakdown.total < 8000.0, f"single full-stack probe too slow: {breakdown.total:.0f} ms"


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
_WARMUP_SAMPLES = 1
_SAMPLE_ATTEMPTS = 2


def _slo(env_var: str, default: float) -> float:
    raw = os.environ.get(env_var)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# SLO thresholds (ms). Overridable via env var so slow hardware / known
# regressions can loosen without editing the test.
_SLO_BASELINE_P50_MS = _slo("EASYCAT_BENCHMARK_SLO_BASELINE_P50_MS", 5000.0)
_SLO_BASELINE_P90_MS = _slo("EASYCAT_BENCHMARK_SLO_BASELINE_P90_MS", 6500.0)
_SLO_FULL_STACK_P50_MS = _slo("EASYCAT_BENCHMARK_SLO_FULL_STACK_P50_MS", 4500.0)
_SLO_LIGHT_JOURNAL_OVERHEAD_MS = _slo("EASYCAT_BENCHMARK_SLO_LIGHT_OVERHEAD_MS", 400.0)
_SLO_FULL_JOURNAL_OVERHEAD_MS = _slo("EASYCAT_BENCHMARK_SLO_FULL_OVERHEAD_MS", 800.0)
_SLO_NR_OVERHEAD_MS = _slo("EASYCAT_BENCHMARK_SLO_NR_OVERHEAD_MS", 500.0)
_SLO_AEC_OVERHEAD_MS = _slo("EASYCAT_BENCHMARK_SLO_AEC_OVERHEAD_MS", 500.0)
_SLO_SMART_TURN_SLOWDOWN_MS = _slo("EASYCAT_BENCHMARK_SLO_SMART_TURN_SLOWDOWN_MS", 800.0)


def _format_ms(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.0f} ms"


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
        total_runs = _WARMUP_SAMPLES + _SAMPLES_PER_CONDITION
        for run_idx in range(total_runs):
            is_warmup = run_idx < _WARMUP_SAMPLES
            label = "warmup" if is_warmup else f"sample {run_idx - _WARMUP_SAMPLES + 1}"
            breakdown: StageBreakdown | None = None
            last_error: Exception | None = None
            for attempt in range(1, _SAMPLE_ATTEMPTS + 1):
                try:
                    breakdown = await _measure_one_turn(
                        ws_server_factory=ws_server_factory,
                        voice_fixture_path=fixture,
                        debug=cond["debug"],
                        enable_noise_reduction=cond["nr"],
                        enable_echo_cancellation=cond["aec"],
                        enable_smart_turn=cond["smart"],
                    )
                    last_error = None
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if attempt < _SAMPLE_ATTEMPTS:
                        print(
                            "[latency] "
                            f"{cond['id']} {label} attempt {attempt} failed; retrying: {exc}"
                        )
                        await asyncio.sleep(0.25)
            if breakdown is None:
                assert last_error is not None
                print(f"[latency] {cond['id']} {label} failed: {last_error}")
                continue
            if is_warmup:
                print(f"[latency] {cond['id']} warmup complete: {_format_ms(breakdown.total)}")
                continue
            result.add(breakdown)
        results.append(result)

    # ── Per-stage breakdown ──────────────────────────────────────
    print()
    print("═" * 92)
    print("Perceived voice-loop latency, broken down by stage (all numbers are medians, ms)")
    print("  Detect    = client speech-end -> VADStopSpeaking")
    print("  STT       = VADStopSpeaking -> STTFinal")
    print("  STTClose  = STTFinal -> AgentRequestStarted")
    print("  AStart    = VADStopSpeaking -> AgentRequestStarted")
    print("  LLM       = AgentRequestStarted -> first AgentDelta")
    print("  TTS       = first AgentDelta -> first TTSAudio  (server TTS time-to-first-byte)")
    print("  Wire      = first TTSAudio -> first audio frame received by client")
    print("  Total     = client speech-end -> first reply audio at client")
    print("═" * 132)
    header = (
        f"{'id':<4}{'condition':<28}"
        f"{'Detect':>8}{'STT':>8}{'STTClose':>10}{'AStart':>9}{'LLM':>8}"
        f"{'TTS':>8}{'Wire':>8}{'Total':>8}{'(p90)':>8}"
    )
    print(header)
    print("─" * 132)
    for r in results:
        short_id = r.name.split(":", 1)[0]
        label = r.name.split(":", 1)[1].strip() if ":" in r.name else r.name
        if not r.samples_ms:
            print(f"{short_id:<4}{label:<34}{'(skipped)':>56}")
            continue
        print(
            f"{short_id:<4}{label:<28}"
            f"{r.detection_median_ms:>7.0f} "
            f"{r.stt_median_ms:>7.0f} "
            f"{r.stt_finalize_close_median_ms:>9.0f} "
            f"{r.agent_request_start_median_ms:>8.0f} "
            f"{r.llm_ttft_median_ms:>7.0f} "
            f"{r.tts_ttfb_median_ms:>7.0f} "
            f"{r.transport_median_ms:>7.0f} "
            f"{r.median_ms:>7.0f} "
            f"{r.p90_ms:>7.0f}"
        )
    print("═" * 132)
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
        # Outer sanity bound: anything over this is a broken pipeline.
        assert r.p90_ms < 8000.0, f"{r.name} p90={r.p90_ms:.0f} ms exceeds 8000 ms sanity bound"

    # ── Relative comparisons + SLO assertions ─────────────────────
    # Each `_SLO_*` threshold is overridable via env var — see module
    # docstring. We collect violations first and raise one combined
    # AssertionError so an engineer triaging a regression sees every
    # stage that got worse, not just the first.
    by_id = {r.name.split(":", 1)[0]: r for r in results if r.samples_ms}
    slo_violations: list[str] = []

    if "B" in by_id:
        baseline = by_id["B"]
        if baseline.median_ms > _SLO_BASELINE_P50_MS:
            slo_violations.append(
                f"baseline p50 {baseline.median_ms:.0f} ms > SLO {_SLO_BASELINE_P50_MS:.0f} ms"
            )
        if baseline.p90_ms > _SLO_BASELINE_P90_MS:
            slo_violations.append(
                f"baseline p90 {baseline.p90_ms:.0f} ms > SLO {_SLO_BASELINE_P90_MS:.0f} ms"
            )

    if "X" in by_id:
        full_stack = by_id["X"]
        if full_stack.median_ms > _SLO_FULL_STACK_P50_MS:
            slo_violations.append(
                f"full-stack (X) p50 {full_stack.median_ms:.0f} ms > "
                f"SLO {_SLO_FULL_STACK_P50_MS:.0f} ms"
            )

    if "B" in by_id and "F" in by_id:
        full_overhead = by_id["F"].median_ms - by_id["B"].median_ms
        print(
            f"[latency] debug=full journal overhead vs baseline: {full_overhead:+.0f} ms (median)"
        )
        if full_overhead > _SLO_FULL_JOURNAL_OVERHEAD_MS:
            slo_violations.append(
                f"debug=full journal overhead {full_overhead:+.0f} ms > "
                f"SLO {_SLO_FULL_JOURNAL_OVERHEAD_MS:.0f} ms"
            )
    if "B" in by_id and "L" in by_id:
        light_overhead = by_id["L"].median_ms - by_id["B"].median_ms
        print(
            f"[latency] debug=light journal overhead vs baseline: "
            f"{light_overhead:+.0f} ms (median)"
        )
        if light_overhead > _SLO_LIGHT_JOURNAL_OVERHEAD_MS:
            slo_violations.append(
                f"debug=light journal overhead {light_overhead:+.0f} ms > "
                f"SLO {_SLO_LIGHT_JOURNAL_OVERHEAD_MS:.0f} ms"
            )
    if "B" in by_id and "N" in by_id:
        nr_overhead = by_id["N"].median_ms - by_id["B"].median_ms
        print(f"[latency] noise-reduction overhead vs baseline: {nr_overhead:+.0f} ms (median)")
        if nr_overhead > _SLO_NR_OVERHEAD_MS:
            slo_violations.append(
                f"noise-reduction overhead {nr_overhead:+.0f} ms > "
                f"SLO {_SLO_NR_OVERHEAD_MS:.0f} ms"
            )
    if "B" in by_id and "A" in by_id:
        aec_overhead = by_id["A"].median_ms - by_id["B"].median_ms
        print(f"[latency] echo-cancellation overhead vs baseline: {aec_overhead:+.0f} ms (median)")
        if aec_overhead > _SLO_AEC_OVERHEAD_MS:
            slo_violations.append(
                f"echo-cancellation overhead {aec_overhead:+.0f} ms > "
                f"SLO {_SLO_AEC_OVERHEAD_MS:.0f} ms"
            )
    if "B" in by_id and "S" in by_id:
        smart_delta = by_id["S"].median_ms - by_id["B"].median_ms
        # Smart turn SHOULD reduce latency; tolerate moderate slowdown
        # because the fixture may not hit early termination, but a
        # large slowdown indicates a real regression.
        print(
            f"[latency] smart-turn delta vs baseline: {smart_delta:+.0f} ms (median) "
            f"{'(faster)' if smart_delta < 0 else '(slower — unexpected)'}"
        )
        if smart_delta > _SLO_SMART_TURN_SLOWDOWN_MS:
            slo_violations.append(
                f"smart-turn slowdown {smart_delta:+.0f} ms > "
                f"SLO {_SLO_SMART_TURN_SLOWDOWN_MS:.0f} ms"
            )

    if slo_violations:
        raise AssertionError("latency SLO violations:\n  - " + "\n  - ".join(slo_violations))
