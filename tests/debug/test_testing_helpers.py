"""Tests for the bundle-driven pytest helpers in easycat.debug.testing."""

from __future__ import annotations

import asyncio
import json
import sys
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from easycat.config import TextSessionConfig, create_text_session
from easycat.debug.bundle import FORMAT_VERSION, RunBundle
from easycat.debug.testing import (
    JUDGE_RUBRIC,
    TurnResult,
    _latency_ms,
    _openai_judge,
    assert_exact_match,
    assert_latency,
    assert_llm_judge,
    assert_no_error,
    assert_regex,
    assert_tool_called,
    assert_turn_completed,
    extract_transcript,
    find_record,
    iter_records,
    load_bundle,
    run_scripted_audio_turn,
    run_text_turn,
    run_text_turns,
    turn_records,
)


def _make_bundle(tmp_path: Path, records: list[dict]) -> Path:
    """Roll a minimal bundle zip around *records*."""
    path = tmp_path / "test.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps({"format_version": FORMAT_VERSION}))
        zf.writestr(
            "journal.ndjson",
            "\n".join(json.dumps(r) for r in records),
        )
    return path


def test_load_bundle_returns_runbundle(tmp_path: Path):
    bundle_path = _make_bundle(
        tmp_path,
        [{"sequence": 1, "name": "turn_started", "turn_id": "t1"}],
    )
    bundle = load_bundle(bundle_path)
    assert isinstance(bundle, RunBundle)


def test_iter_records_filters_by_name(tmp_path: Path):
    records = [
        {"sequence": 1, "name": "turn_started"},
        {"sequence": 2, "name": "stt_final", "data": {"text": "hi"}},
        {"sequence": 3, "name": "stt_final", "data": {"text": "there"}},
        {"sequence": 4, "name": "turn_ended"},
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    stt = list(iter_records(bundle, name="stt_final"))
    assert len(stt) == 2


def test_assert_exact_match_passes(tmp_path: Path):
    records = [
        {"sequence": 1, "name": "agent_final", "data": {"text": "Hello!"}},
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    assert_exact_match(bundle, expected="Hello!")


def test_assert_exact_match_fails(tmp_path: Path):
    records = [
        {"sequence": 1, "name": "agent_final", "data": {"text": "Hello!"}},
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    with pytest.raises(AssertionError, match="text mismatch"):
        assert_exact_match(bundle, expected="Goodbye")


def test_assert_exact_match_reads_top_level_text(tmp_path: Path):
    """Some bundle variants stash the reply text on the record root rather than under ``data``."""
    records = [
        {"sequence": 1, "name": "agent_final", "text": "Hello from root!"},
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    assert_exact_match(bundle, expected="Hello from root!")


def test_assert_regex_matches(tmp_path: Path):
    records = [
        {"sequence": 1, "name": "agent_final", "data": {"text": "The weather is 72F."}},
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    assert_regex(bundle, pattern=r"\d+F")


def test_assert_regex_fails_on_no_match(tmp_path: Path):
    records = [
        {"sequence": 1, "name": "agent_final", "data": {"text": "all clear"}},
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    with pytest.raises(AssertionError, match="did not match"):
        assert_regex(bundle, pattern=r"\d+F")


def test_assert_turn_completed_requires_both_boundaries(tmp_path: Path):
    # turn_started without turn_ended = hang.
    records = [
        {"sequence": 1, "name": "turn_started", "turn_id": "t1"},
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    with pytest.raises(AssertionError, match="never completed"):
        assert_turn_completed(bundle, "t1")


def test_assert_turn_completed_passes(tmp_path: Path):
    records = [
        {"sequence": 1, "name": "turn_started", "turn_id": "t1"},
        {"sequence": 2, "name": "turn_ended", "turn_id": "t1"},
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    assert_turn_completed(bundle, "t1")


def test_assert_no_error_passes_on_clean_bundle(tmp_path: Path):
    records = [{"sequence": 1, "name": "turn_started", "turn_id": "t1"}]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    assert_no_error(bundle)


def test_assert_no_error_flags_error_record(tmp_path: Path):
    records = [
        {
            "sequence": 1,
            "name": "error",
            "turn_id": "t1",
            "error": {"type": "STTTimeout", "message": "no partials"},
        }
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    with pytest.raises(AssertionError, match="STTTimeout"):
        assert_no_error(bundle)


def test_assert_tool_called(tmp_path: Path):
    records = [
        {
            "sequence": 1,
            "name": "tool_call_started",
            "data": {"tool_name": "calculator"},
        }
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    assert_tool_called(bundle, tool_name="calculator")


def test_turn_records_and_find_record(tmp_path: Path):
    records = [
        {"sequence": 1, "name": "turn_started", "turn_id": "t1"},
        {"sequence": 2, "name": "stt_final", "turn_id": "t1", "data": {"text": "hi"}},
        {"sequence": 3, "name": "turn_started", "turn_id": "t2"},
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    assert len(turn_records(bundle, "t1")) == 2
    first = find_record(bundle, name="turn_started")
    assert first is not None
    assert first["turn_id"] == "t1"
    assert find_record(bundle, name="does_not_exist") is None


# ── run_text_turn / TurnResult ───────────────────────────────────


class _EchoAgent:
    """Deterministic stand-in for an LLM-backed agent."""

    async def run(self, text: str) -> str:
        return f"echo: {text}"


async def test_run_text_turn_with_bare_agent_returns_journal_backed_result():
    result = await run_text_turn(_EchoAgent(), "hello")

    assert isinstance(result, TurnResult)
    assert result.user_input == "hello"
    assert result.response == "echo: hello"
    assert result.turn_id.startswith("turn-")
    assert result.latency_ms > 0
    names = {r.get("name") for r in result.records()}
    assert "turn_started" in names
    assert "agent_final" in names
    assert "turn_ended" in names


async def test_run_text_turn_result_works_with_bundle_assert_helpers():
    result = await run_text_turn(_EchoAgent(), "ping")

    assert_turn_completed(result, result.turn_id)
    assert_no_error(result, turn_id=result.turn_id)
    assert_exact_match(result, expected="echo: ping")
    assert_regex(result, pattern=r"^echo: ")


async def test_run_text_turn_with_text_session_config():
    cfg = TextSessionConfig(agent=_EchoAgent())  # debug defaults to "light"

    result = await run_text_turn(cfg, "config path")

    assert result.response == "echo: config path"
    assert_turn_completed(result, result.turn_id)
    # The caller's config must not be mutated to get a journal; the default
    # debug="light" already journals, so run_text_turn uses it as-is.
    assert cfg.debug == "light"


async def test_run_text_turn_with_debug_off_config_upgrades_without_mutation():
    # When the caller explicitly opted out (debug="off"), run_text_turn must
    # upgrade a *copy* to "light" so the TurnResult is assertable, leaving the
    # caller's config untouched.
    cfg = TextSessionConfig(agent=_EchoAgent(), debug="off")

    result = await run_text_turn(cfg, "off path")

    assert result.response == "echo: off path"
    assert_turn_completed(result, result.turn_id)
    assert cfg.debug == "off"


async def test_run_text_turn_with_caller_owned_session_runs_many_turns():
    session = create_text_session(agent=_EchoAgent(), debug="light")
    async with session:
        first = await run_text_turn(session, "one")
        second = await run_text_turn(session, "two")

    assert first.response == "echo: one"
    assert second.response == "echo: two"
    assert first.turn_id != second.turn_id
    assert_turn_completed(second, second.turn_id)
    # The first result's window never sees the later turn.
    assert not turn_records(first, second.turn_id)


async def test_run_text_turn_rejects_session_without_journal():
    session = create_text_session(agent=_EchoAgent(), debug="off")
    async with session:
        with pytest.raises(RuntimeError, match='debug="light"'):
            await run_text_turn(session, "hi")


# ── run_text_turns / run_scripted_audio_turn ─────────────────────


class _FailingAgent:
    """Agent whose tool dependency is down."""

    async def run(self, text: str) -> str:
        raise RuntimeError("tool 'current_time' is unavailable")


class _NeverRepliesAgent:
    """Agent that never produces a reply, so the audio helper must time out."""

    async def run(self, text: str) -> str:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")  # pragma: no cover - defensive


def _session_ids(result: TurnResult) -> set[str]:
    return {r["session_id"] for r in result.records() if r.get("session_id")}


async def test_run_text_turns_runs_every_turn_on_one_session():
    first, second = await run_text_turns(_EchoAgent(), ["one", "two"])

    assert first.response == "echo: one"
    assert second.response == "echo: two"
    assert first.turn_id != second.turn_id
    assert _session_ids(first) == _session_ids(second) != set()
    assert not turn_records(first, second.turn_id)
    assert_turn_completed(first, first.turn_id)
    assert_turn_completed(second, second.turn_id)


async def test_run_text_turns_with_caller_owned_session_keeps_it_open():
    session = create_text_session(agent=_EchoAgent(), debug="light")
    async with session:
        await run_text_turns(session, ["a", "b"])
        # The helper never stops a session it does not own.
        third = await run_text_turn(session, "c")

    assert third.response == "echo: c"


async def test_run_text_turns_rejects_a_bare_string():
    with pytest.raises(TypeError, match="sequence of inputs"):
        await run_text_turns(_EchoAgent(), "hi")


async def test_run_text_turns_rejects_empty_inputs(monkeypatch: pytest.MonkeyPatch):
    def _boom(config_or_agent):  # pragma: no cover - must never be reached
        raise AssertionError("run_text_turns() built a session for zero work")

    monkeypatch.setattr("easycat.debug.testing._build_text_session", _boom)

    with pytest.raises(ValueError, match="at least one"):
        await run_text_turns(_EchoAgent(), [])


async def test_run_text_turns_propagates_agent_failure_and_still_stops_the_session():
    with pytest.raises(RuntimeError, match="unavailable"):
        await run_text_turns(_FailingAgent(), ["what time is it?"])

    # The failed run left nothing behind that blocks a fresh one.
    recovered = await run_text_turns(_EchoAgent(), ["again"])
    assert recovered[0].response == "echo: again"


async def test_run_text_turns_leaves_no_owned_tasks_behind():
    before = asyncio.all_tasks()

    await run_text_turns(_EchoAgent(), ["one", "two"])
    assert asyncio.all_tasks() - before == set()

    with pytest.raises(RuntimeError):
        await run_text_turns(_FailingAgent(), ["boom"])
    assert asyncio.all_tasks() - before == set()


async def test_run_text_turns_results_accept_assert_latency():
    results = await run_text_turns(_EchoAgent(), ["one", "two"])

    assert_latency(results, max_ms=5000.0, percentile="p95")


async def test_run_scripted_audio_turn_drives_the_full_pipeline():
    result = await run_scripted_audio_turn(_EchoAgent(), transcript="hello")

    names = {r.get("name") for r in result.records()}
    assert {"vad_start_speaking", "stt_final", "agent_final", "tts_audio"} <= names
    assert_exact_match(result, expected="echo: hello")
    metric = find_record(result, name="turn_total_latency_ms")
    assert metric is not None
    assert result.latency_ms == float(metric["data"]["value"])


async def test_run_scripted_audio_turn_leaves_no_owned_tasks_behind():
    before = asyncio.all_tasks()

    await run_scripted_audio_turn(_EchoAgent(), transcript="hello")

    assert asyncio.all_tasks() - before == set()


async def test_run_scripted_audio_turn_fallback_latency_times_the_turn_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wall-clock fallback must not bill session setup, drain or teardown.

    A run whose journal carries no latency metric (an agent that replies but
    whose TTS emits nothing) would otherwise report the whole session lifetime
    as the turn latency, which then feeds a user's ``assert_latency``.
    """
    from easycat.debug import testing as testing_module

    seen: list[float] = []

    def _capture(records: object, *, fallback: float) -> float:
        seen.append(fallback)
        return fallback

    monkeypatch.setattr(testing_module, "_latency_ms", _capture)

    result = await run_scripted_audio_turn(_EchoAgent(), transcript="hello", drain_s=1.0)

    assert seen, "the fallback path never ran"
    assert seen[0] < 900.0, f"the 1 s drain leaked into the fallback latency: {seen[0]}"
    assert result.latency_ms == seen[0]


async def test_run_scripted_audio_turn_times_out_with_a_named_failure():
    with pytest.raises(AssertionError, match="no agent reply"):
        await run_scripted_audio_turn(_NeverRepliesAgent(), timeout_s=0.2)


def test_latency_ms_prefers_the_text_metric_over_the_voice_metric():
    # Name priority, not record order: the voice metric comes first in the
    # record list and must still lose to the text metric.
    records = [
        {"name": "turn_total_latency_ms", "data": {"value": 99.0}},
        {"name": "text_turn_latency_ms", "data": {"value": 7.0}},
    ]

    assert _latency_ms(records, fallback=123.0) == 7.0


def test_latency_ms_falls_back_when_no_metric_record_is_present():
    assert _latency_ms([], fallback=123.0) == 123.0


# ── assert_latency ───────────────────────────────────────────────


def test_assert_latency_passes_within_budget():
    results = [
        TurnResult(turn_id=f"t{i}", user_input="", response="", latency_ms=float(i))
        for i in range(1, 11)
    ]
    assert_latency(results, max_ms=50.0)
    assert_latency(results[0], max_ms=1.0, percentile="p50")
    assert_latency([12.0, 14.0], max_ms=20.0, percentile="p99")


def test_assert_latency_fails_over_budget():
    with pytest.raises(AssertionError, match="exceeds budget"):
        assert_latency([100.0, 200.0, 9000.0], max_ms=1000.0, percentile="p99")


def test_assert_latency_rejects_unknown_percentile():
    with pytest.raises(ValueError, match="percentile"):
        assert_latency([1.0], max_ms=10.0, percentile="p42")


def test_assert_latency_rejects_empty_samples():
    with pytest.raises(AssertionError, match="no latency samples"):
        assert_latency([], max_ms=10.0)


# ── extract_transcript / assert_llm_judge ────────────────────────


def test_extract_transcript_from_turn_result():
    result = TurnResult(turn_id="t1", user_input="hi", response="hello!", latency_ms=1.0)
    assert extract_transcript(result) == "User: hi\nBot: hello!"


def test_extract_transcript_reads_session_sink_names(tmp_path: Path):
    records = [
        {"sequence": 1, "name": "stt_final", "data": {"text": "what time is it"}},
        {"sequence": 2, "name": "agent_final", "data": {"text": "It is noon."}},
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    assert extract_transcript(bundle) == "User: what time is it\nBot: It is noon."


def test_extract_transcript_falls_back_to_stage_names(tmp_path: Path):
    """Teaching chapter 12 bundles use stage-level names (stt.final / stage.tts.execute)."""
    records = [
        {"sequence": 1, "name": "stt.final", "data": {"text": "hi"}},
        {"sequence": 2, "name": "stage.tts.execute", "data": {"text": "Hi there."}},
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    assert extract_transcript(bundle) == "User: hi\nBot: Hi there."


async def test_assert_llm_judge_passes_with_injected_judge():
    result = TurnResult(turn_id="t1", user_input="hi", response="hello!", latency_ms=1.0)
    seen: dict = {}

    async def fake_judge(transcript: str, rubric: str) -> dict:
        seen["transcript"] = transcript
        seen["rubric"] = rubric
        return {"relevance": 5, "fluency": 4, "appropriate_length": 5, "reasoning": "fine"}

    verdict = await assert_llm_judge(result, judge=fake_judge)

    assert verdict["reasoning"] == "fine"
    assert seen["transcript"] == "User: hi\nBot: hello!"
    assert seen["rubric"] == JUDGE_RUBRIC


async def test_assert_llm_judge_fails_below_min_score():
    result = TurnResult(turn_id="t1", user_input="hi", response="??", latency_ms=1.0)

    async def harsh_judge(transcript: str, rubric: str) -> dict:
        return {"relevance": 1, "fluency": 5, "reasoning": "did not answer"}

    with pytest.raises(AssertionError, match="relevance"):
        await assert_llm_judge(result, judge=harsh_judge)


async def test_assert_llm_judge_requires_numeric_scores():
    result = TurnResult(turn_id="t1", user_input="hi", response="x", latency_ms=1.0)

    async def vague_judge(transcript: str, rubric: str) -> dict:
        return {"reasoning": "no scores"}

    with pytest.raises(AssertionError, match="no numeric scores"):
        await assert_llm_judge(result, judge=vague_judge)


async def test_assert_llm_judge_default_judge_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = TurnResult(turn_id="t1", user_input="hi", response="x", latency_ms=1.0)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        await assert_llm_judge(result)


@pytest.mark.parametrize(
    ("model", "expected_reasoning_effort"),
    [
        ("gpt-5.6-luna", "none"),
        ("gpt-4.1-mini", None),
    ],
)
async def test_openai_judge_only_sets_reasoning_for_its_default_model(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    expected_reasoning_effort: str | None,
) -> None:
    calls: list[dict[str, object]] = []

    class _Completions:
        async def create(self, **kwargs: object) -> object:
            calls.append(kwargs)
            message = SimpleNamespace(
                content='{"relevance": 5, "fluency": 5, "appropriate_length": 5}'
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class _AsyncOpenAI:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=_Completions())

    openai_stub = ModuleType("openai")
    openai_stub.AsyncOpenAI = _AsyncOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", openai_stub)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    judge = _openai_judge(model)
    verdict = await judge("User: hi\nBot: hello", JUDGE_RUBRIC)

    assert verdict["relevance"] == 5
    if expected_reasoning_effort is None:
        assert "reasoning_effort" not in calls[0]
    else:
        assert calls[0]["reasoning_effort"] == expected_reasoning_effort
