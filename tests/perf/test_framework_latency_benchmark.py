from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from perf.bench_framework_latency import (
    LOCK_EXCLUDE_NEWER_BY_FRAMEWORK,
    PINS,
    Worker,
    WorkerSpec,
    _lock_exclude_newer,
    _lock_metadata,
    _validate_sample,
    percentile,
    rank_by_latency,
    require_lock_exclude_newer,
    run_benchmark,
    worker_specs,
)
from perf.framework_latency_worker import _shutdown_pipecat_runner, _timed_critical_path

# The external-framework smoke tests launch `uv run --locked --project
# perf/framework_environments/<framework>`, which resolves and installs the
# competitor's whole dependency tree before the worker can even handshake.
# `Worker` already bounds every message it waits on (``worker_timeout_s``,
# 120s by default) and raises a TimeoutError carrying the worker's stderr --
# but the global 60s ``timeout`` in pyproject fired first, force-exiting the
# xdist worker process so that diagnostic never reached the report. Budget
# above the worker's own ceiling (handshake + samples + shutdown) so the
# benchmark's bounded, message-carrying error paths always win the race.
_EXTERNAL_WORKER_TIMEOUT_S = 600


def test_timed_critical_path_restores_gc_state_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        "perf.framework_latency_worker._begin_critical_path",
        lambda: (12.5, True),
    )
    monkeypatch.setattr(
        "perf.framework_latency_worker._end_critical_path",
        lambda was_enabled: calls.append(was_enabled),
    )

    with pytest.raises(RuntimeError, match="sample failed"), _timed_critical_path() as started:
        assert started == 12.5
        raise RuntimeError("sample failed")

    assert calls == [True]


async def test_pipecat_runner_shutdown_is_bounded() -> None:
    class Task:
        def __init__(self) -> None:
            self.frames: list[object] = []

        async def queue_frame(self, frame: object) -> None:
            self.frames.append(frame)

    task = Task()
    end_frame = object()
    runner_task = asyncio.create_task(asyncio.Event().wait())

    with pytest.raises(TimeoutError, match="Pipecat runner did not stop"):
        await _shutdown_pipecat_runner(task, runner_task, end_frame, timeout_s=0.01)

    assert task.frames == [end_frame]
    assert runner_task.cancelled()


def test_ranking_uses_raw_latency_not_overlap_adjusted_diagnostic() -> None:
    results = {
        "easycat": {
            "latency_p50_ms": 21.0,
            "latency_p95_ms": 23.0,
            "framework_overhead_p50_ms": 1.0,
            "framework_overhead_p95_ms": 1.5,
        },
        "livekit": {
            "latency_p50_ms": 20.0,
            "latency_p95_ms": 22.0,
            "framework_overhead_p50_ms": 2.0,
            "framework_overhead_p95_ms": 2.5,
        },
    }

    ranking, fastest = rank_by_latency(results, ("easycat", "livekit"))

    assert ranking == ["livekit", "easycat"]
    assert fastest == {"p50": False, "p95": False, "all": False}


def test_worker_specs_pin_competitors_in_isolated_environments(tmp_path: Path) -> None:
    worker = tmp_path / "worker.py"
    specs = worker_specs(worker_path=worker)

    easycat, livekit, pipecat = specs
    assert easycat.command == (sys.executable, str(worker), "--framework", "easycat")
    assert "--no-config" in livekit.command
    assert "--no-config" in pipecat.command
    for spec in (livekit, pipecat):
        cutoff = spec.command[spec.command.index("--exclude-newer") + 1]
        assert cutoff == LOCK_EXCLUDE_NEWER_BY_FRAMEWORK[spec.framework]
    # Pin the cutoffs as literals so an unintended lock regeneration (which
    # shifts the whole transitive snapshot) fails loudly instead of moving the
    # dict and the command in lockstep.
    assert LOCK_EXCLUDE_NEWER_BY_FRAMEWORK == {
        "livekit": "2026-08-05T00:00:00Z",
        "pipecat": "2026-08-05T00:00:00Z",
    }
    assert "--isolated" in livekit.command
    assert "--locked" in livekit.command
    assert "--locked" in pipecat.command
    assert livekit.command[livekit.command.index("--python") + 1] == sys.executable
    assert pipecat.command[pipecat.command.index("--python") + 1] == sys.executable
    assert PINS == {
        "livekit": ("livekit-agents==1.6.8",),
        "pipecat": ("pipecat-ai==1.7.0", "websockets==17.0.1"),
    }


def test_worker_startup_exit_fails_without_waiting_for_response_timeout(tmp_path: Path) -> None:
    worker = tmp_path / "exits_immediately.py"
    worker.write_text("raise SystemExit(2)\n")

    with pytest.raises(RuntimeError, match="worker exited before sending a response"):
        Worker(
            WorkerSpec(framework="easycat", command=(sys.executable, str(worker))),
            timeout_s=0.5,
        )


def test_regenerated_lock_without_cutoff_reads_as_missing_instead_of_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A Dependabot bump to perf/framework_environments/<framework> regenerates
    # the lock without `--exclude-newer`, which drops the whole [options]
    # table. Reading that at import time raised KeyError('options') and failed
    # collection for this entire module, so every perf test reported an opaque
    # missing key instead of the one real problem.
    project = tmp_path / "pipecat"
    project.mkdir()
    (project / "uv.lock").write_text('version = 1\n\n[[package]]\nname = "aiohttp"\n')
    monkeypatch.setattr("perf.bench_framework_latency.ENVIRONMENT_ROOT", tmp_path)

    assert _lock_exclude_newer("pipecat") is None


def test_missing_lock_cutoff_names_the_lock_and_how_to_re_pin_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(LOCK_EXCLUDE_NEWER_BY_FRAMEWORK, "pipecat", None)

    with pytest.raises(RuntimeError, match="exclude-newer") as excinfo:
        require_lock_exclude_newer("pipecat")

    message = str(excinfo.value)
    assert "perf/framework_environments/pipecat/uv.lock" in message
    assert "uv lock --project perf/framework_environments/pipecat" in message

    # The isolated-environment command must never be built from a lock that no
    # longer pins the snapshot it claims to reproduce.
    with pytest.raises(RuntimeError, match="exclude-newer"):
        worker_specs(("pipecat",))

    # Nor may a benchmark report record a cutoff the lock no longer carries.
    with pytest.raises(RuntimeError, match="exclude-newer"):
        _lock_metadata()


def test_competitor_lock_metadata_is_content_addressed() -> None:
    metadata = _lock_metadata()

    assert set(metadata) == {"livekit", "pipecat"}
    for framework, lock in metadata.items():
        assert lock["path"].endswith("uv.lock")
        assert len(lock["sha256"]) == 64
        assert lock["exclude_newer"] == LOCK_EXCLUDE_NEWER_BY_FRAMEWORK[framework]


def test_percentile_interpolates_and_validates_inputs() -> None:
    assert percentile([1.0, 2.0, 3.0], 0.5) == 2.0
    assert percentile([1.0, 3.0], 0.5) == 2.0
    with pytest.raises(ValueError, match="samples"):
        percentile([], 0.5)
    with pytest.raises(ValueError, match="quantile"):
        percentile([1.0], 1.1)


@pytest.mark.parametrize(
    "sample",
    [
        {
            "latency_ms": -1.0,
            "provider_elapsed_ms": 0.0,
            "framework": "easycat",
            "text": "Hello there.",
            "audio_bytes": 1,
        },
        {
            "latency_ms": 1.0,
            "provider_elapsed_ms": 0.5,
            "framework": "easycat",
            "text": "wrong",
            "audio_bytes": 1,
        },
        {
            "latency_ms": 1.0,
            "provider_elapsed_ms": 0.5,
            "framework": "easycat",
            "text": "Hello there.",
            "audio_bytes": 0,
        },
        {
            "latency_ms": 1.0,
            "provider_elapsed_ms": 2.0,
            "framework": "easycat",
            "text": "Hello there.",
            "audio_bytes": 1,
        },
    ],
)
def test_correctness_gate_rejects_invalid_samples(sample: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _validate_sample(sample)


def test_correctness_gate_requires_easycat_voice_transition() -> None:
    sample = {
        "latency_ms": 1.0,
        "provider_elapsed_ms": 0.5,
        "framework": "easycat",
        "text": "Hello there.",
        "audio_bytes": 320,
        "agent_request_started_in_timed_path": False,
    }

    with pytest.raises(ValueError, match="voice-turn agent request transition"):
        _validate_sample(sample)

    sample["agent_request_started_in_timed_path"] = True
    _validate_sample(sample)


async def test_easycat_timed_span_covers_end_of_speech_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The measured EasyCat sample must run the voice end-of-speech transition.

    Guards the boundary the cross-framework comparison depends on: the span
    timed by ``_sample_easycat`` enters through ``end_turn()`` into
    ``TurnRunner.handle_end_of_speech`` (not a direct ``run_streaming_agent``
    call), and the AgentRequestStarted event-bus dispatch lands inside the
    timed span, before first audio.
    """
    from easycat.session._turn_runner import TurnRunner
    from perf.framework_latency_worker import _sample_easycat

    transcripts: list[str] = []
    original = TurnRunner.handle_end_of_speech

    async def _spy(  # type: ignore[no-untyped-def]
        self: TurnRunner,
        turn=None,
        *,
        identity=None,
        activity=None,
    ) -> None:
        transcripts.append(turn.transcript_text if turn is not None else "")
        await original(self, turn=turn, identity=identity, activity=activity)

    monkeypatch.setattr(TurnRunner, "handle_end_of_speech", _spy)

    sample = await _sample_easycat(0.0, 0.0)

    assert transcripts == ["Hello"]
    assert sample["agent_request_started_in_timed_path"] is True
    _validate_sample({"kind": "sample", "framework": "easycat", **sample})


def test_easycat_worker_smoke_includes_public_voice_transition() -> None:
    result = run_benchmark(
        iterations=2,
        warmups=1,
        llm_delay_ms=1.0,
        tts_delay_ms=1.0,
        frameworks=("easycat",),
    )

    assert result["metric"] == "accepted_transcript_to_first_audio_ms"
    easycat = result["results"]["easycat"]
    assert len(easycat["latency_samples_ms"]) == 2
    assert len(easycat["framework_overhead_samples_ms"]) == 2
    assert easycat["latency_p50_ms"] > 2.0
    assert (
        result["methodology"]["correctness_gate"]
        == "exact_framework_tts_text_nonempty_audio_and_easycat_voice_transition"
    )
    assert result["comparison_metric"] == "accepted_transcript_to_first_audio_ms"
    assert result["ranking_by_latency_p50"] == ["easycat"]
    assert "not used for ranking" in result["methodology"]["framework_overhead"]


@pytest.mark.integration_external
@pytest.mark.timeout(_EXTERNAL_WORKER_TIMEOUT_S)
@pytest.mark.parametrize("framework", ["livekit", "pipecat"])
def test_external_framework_worker_smoke(framework: str) -> None:
    result = run_benchmark(
        iterations=1,
        warmups=0,
        llm_delay_ms=0.0,
        tts_delay_ms=0.0,
        frameworks=(framework,),  # type: ignore[arg-type]
    )

    measured = result["results"][framework]
    assert measured["latency_samples_ms"]
    assert measured["latency_p50_ms"] >= 0
    assert measured["version"]
