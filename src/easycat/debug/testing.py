"""Text-first eval helpers and bundle-driven pytest assertions.

These helpers live in the library (LiveKit 1.0 pattern — ship the
testing surface in core, not a sidecar package) so authors can promote
a production failure into a regression test in the same PR that fixes
it.  The ladder of evals, smallest to largest:

1. **Bundle fixtures** — :func:`load_bundle` a checked-in ``RunBundle``
   and assert on its records (``assert_exact_match`` and friends).
2. **Text turns** — :func:`run_text_turn` drives one real agent-bridge
   turn through ``Session.send_text`` with Noop audio stages and
   returns a journal-backed :class:`TurnResult` that the same
   ``assert_*`` helpers accept.  :func:`run_text_turns` runs several
   inputs against one session, and :func:`run_scripted_audio_turn`
   drives one turn through the real *audio* pipeline with scripted
   stub I/O — it checks pipeline wiring, not speech quality.
3. **Latency + judge** — :func:`assert_latency` budgets turn latency
   using the validation percentile vocabulary; :func:`assert_llm_judge`
   scores conversational quality with an LLM rubric (teaching chapter
   12 promotes the same rubric).
4. **Live audio** — ``easycat validate latency`` / ``validate live``
   cover full-pipeline runs against real providers.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from easycat.debug.bundle import RunBundle

__all__ = [
    "JUDGE_RUBRIC",
    "RecordSource",
    "TurnResult",
    "assert_exact_match",
    "assert_latency",
    "assert_llm_judge",
    "assert_no_error",
    "assert_regex",
    "assert_tool_called",
    "assert_turn_completed",
    "extract_transcript",
    "find_record",
    "iter_records",
    "load_bundle",
    "run_scripted_audio_turn",
    "run_text_turn",
    "run_text_turns",
    "turn_records",
]


# ── Record sources ───────────────────────────────────────────────


@runtime_checkable
class RecordSource(Protocol):
    """Anything that yields journal records as dicts.

    Both :class:`~easycat.debug.bundle.RunBundle` (loaded from a
    ``.zip``) and :class:`TurnResult` (captured live by
    :func:`run_text_turn`) satisfy this, so every ``assert_*`` helper
    below works on either without conversion.
    """

    def records(self) -> Iterable[dict[str, Any]]: ...


@dataclass(frozen=True)
class TurnResult:
    """Journal-backed result of one text turn run by :func:`run_text_turn`.

    Shares vocabulary with the bundle helpers: ``records()`` yields the
    same dict shape as ``RunBundle.records()``, so ``assert_no_error``,
    ``assert_turn_completed``, ``assert_regex`` etc. accept a
    ``TurnResult`` directly.
    """

    turn_id: str
    user_input: str
    response: str
    latency_ms: float
    journal_records: tuple[dict[str, Any], ...] = field(default=())

    def records(self) -> Iterable[dict[str, Any]]:
        """Journal records captured during this turn (bundle dict shape)."""
        return iter(self.journal_records)


# ── Loading ──────────────────────────────────────────────────────


def load_bundle(path: str | Path) -> RunBundle:
    """Load a :class:`RunBundle` from a ``.zip`` path.

    Works equally well with bundles captured via
    ``session.export_debug_bundle(...)`` and with the fixture bundles
    checked into ``tests/fixtures/``.
    """
    return RunBundle.load(Path(path))


# ── Live text turns ──────────────────────────────────────────────


async def run_text_turn(config_or_session: Any, user_input: str) -> TurnResult:
    """Run one real agent-bridge turn through ``Session.send_text``.

    Accepts, in order of preference:

    - a live text-mode :class:`~easycat.session.Session` (created with
      ``create_text_session(..., debug="light")``) — the caller owns
      its lifecycle and can run many turns against it;
    - a :class:`~easycat.config.TextSessionConfig` or
      :class:`~easycat.config.EasyConfig` — a throwaway text session is
      built around its agent settings (audio stages are Noop stubs);
    - any agent object ``create_text_session`` accepts (a framework
      agent, an ``ExternalAgentBridge``, or a plain object with
      ``async run(text) -> str``).

    The turn's journal records are captured into the returned
    :class:`TurnResult`, so the ``assert_*`` helpers in this module can
    be applied to it exactly like a loaded bundle.

    Use :func:`run_text_turns` when a scenario needs several turns on
    one session.
    """
    return (await run_text_turns(config_or_session, [user_input]))[0]


async def run_text_turns(
    config_or_session: Any,
    user_inputs: Sequence[str],
) -> list[TurnResult]:
    """Run several text turns against ONE session, in order.

    Same dispatch as :func:`run_text_turn`: a live
    :class:`~easycat.session.Session` is used as given and left running
    (the caller owns it); a config or bare agent gets one throwaway
    journaled session that serves every turn and is force-stopped when
    the sequence ends or raises.  Returns one :class:`TurnResult` per
    input, in order; each result's records are scoped to its own turn.

    A config is resolved once, at session construction — the same
    snapshot semantics ``create_text_session`` already has, so mutating
    the config between turns has no effect on the running session.
    """
    from easycat.session import Session

    if isinstance(user_inputs, str):
        raise TypeError("run_text_turns() takes a sequence of inputs; pass [text] for one turn.")
    inputs = list(user_inputs)
    if not inputs:
        raise ValueError("run_text_turns() needs at least one user input")

    if isinstance(config_or_session, Session):
        return [await _run_turn_on_session(config_or_session, text) for text in inputs]

    session = _build_text_session(config_or_session)
    try:
        return [await _run_turn_on_session(session, text) for text in inputs]
    finally:
        await session.stop(force=True)


async def run_scripted_audio_turn(
    agent: Any,
    *,
    transcript: str = "hello",
    timeout_s: float = 10.0,
    drain_s: float = 0.25,
) -> TurnResult:
    """Drive one turn through the *audio* pipeline with scripted stub I/O.

    Uses :func:`easycat.stubs.scripted_turn_config` — a scripted
    transport, VAD, STT and TTS around the caller's *agent* — so
    transport → VAD → STT → agent → TTS really runs with no microphone,
    no API key, no provider extra and no network.  The audio is
    synthetic: this checks pipeline wiring, not speech quality.

    The returned ``latency_ms`` is the voice pipeline's
    turn-ended-to-first-TTS-audio interval (``turn_total_latency_ms``),
    not :func:`run_text_turn`'s send-to-reply interval.
    """
    import asyncio

    from easycat.config import create_session
    from easycat.debug._serialize import record_to_dict
    from easycat.events import AgentFinal
    from easycat.stubs import scripted_turn_config

    config = scripted_turn_config(agent=agent, transcript=transcript)
    session = create_session(config)
    reply: dict[str, str] = {}
    done = asyncio.Event()

    def _on_reply(event: AgentFinal) -> None:
        reply.setdefault("text", event.text)
        done.set()

    session.subscribe_event(AgentFinal, _on_reply)
    timed_out = False
    wall_ms = 0.0
    async with session:
        # Bracket the turn alone: session start-up, the drain sleep and
        # teardown must not inflate the fallback latency.
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout_s)
        except TimeoutError:
            timed_out = True
        else:
            wall_ms = (time.monotonic() - t0) * 1000
            # Let TTS synthesis drain into the journal before teardown.
            await asyncio.sleep(drain_s)
    if timed_out:
        raise AssertionError(
            f"scripted audio turn produced no agent reply within {timeout_s:.1f}s"
        )

    view = session.journal
    raw = view.read() if view is not None else []
    records = tuple(record_to_dict(record) for record in raw)
    turn_id = next((r["turn_id"] for r in records if r.get("turn_id")), "")
    return TurnResult(
        turn_id=turn_id,
        user_input=transcript,
        response=reply.get("text", ""),
        latency_ms=_latency_ms(records, fallback=wall_ms),
        journal_records=records,
    )


def _build_text_session(config_or_agent: Any) -> Any:
    """Build a journaled text session from a config or bare agent."""
    import copy

    from easycat.config import EasyConfig, TextSessionConfig, create_text_session

    if isinstance(config_or_agent, TextSessionConfig):
        cfg = config_or_agent
        if cfg.debug == "off":
            # A journal is what makes the TurnResult assertable; "light"
            # is an in-memory ring buffer, so this costs nothing.
            cfg = copy.deepcopy(cfg)
            cfg.debug = "light"
        return create_text_session(cfg)

    if isinstance(config_or_agent, EasyConfig):
        source = config_or_agent
        cfg = TextSessionConfig(
            agent=source.agent,
            agent_model=source.agent_model,
            remote_agent_api_key=source.remote_agent_api_key,
            agent_runner=source.agent_runner,
            wrap_agent=source.wrap_agent,
            mcp_servers=list(source.mcp_servers) if source.mcp_servers else None,
            debug=source.debug if source.debug != "off" else "light",
        )
        return create_text_session(cfg)

    return create_text_session(agent=config_or_agent, debug="light")


async def _run_turn_on_session(session: Any, user_input: str) -> TurnResult:
    from easycat.debug._serialize import record_to_dict

    view = session.journal
    if view is None:
        raise RuntimeError(
            "run_text_turn() needs a journal to build a TurnResult; create the "
            'session with create_text_session(..., debug="light") (or "full").'
        )

    seen = len(view.read())
    t0 = time.monotonic()
    response = await session.send_text(user_input)
    wall_ms = (time.monotonic() - t0) * 1000

    records = tuple(record_to_dict(record) for record in view.read()[seen:])
    turn_id = next((r["turn_id"] for r in records if r.get("turn_id")), "")
    return TurnResult(
        turn_id=turn_id,
        user_input=user_input,
        response=response,
        latency_ms=_latency_ms(records, fallback=wall_ms),
        journal_records=records,
    )


# Name priority, NOT record order: scan for the first name, and only if no
# record carries it fall through to the next.  A text turn's latency must keep
# coming from text_turn_latency_ms even if a future release also emits
# turn_total_latency_ms on the text path — the two measure different intervals.
_LATENCY_METRIC_NAMES = ("text_turn_latency_ms", "turn_total_latency_ms")


def _latency_ms(records: Sequence[dict[str, Any]], *, fallback: float) -> float:
    """Return the first journal latency metric by name priority."""
    for name in _LATENCY_METRIC_NAMES:  # outer loop = the priority
        for record in records:  # inner loop = record order
            if record.get("name") != name:
                continue
            data = record.get("data")
            if isinstance(data, dict) and isinstance(data.get("value"), (int, float)):
                return float(data["value"])
    return fallback


# ── Iteration helpers ────────────────────────────────────────────


def iter_records(source: RecordSource, *, name: str | None = None) -> Iterable[dict[str, Any]]:
    """Iterate journal records, optionally filtering by event name.

    Names match the :data:`JournalRecord.name` field emitted at record
    creation.  The session's journal sink writes snake_case names
    (``"turn_started"``, ``"stt_final"``, ``"agent_final"``,
    ``"tool_call_started"``) — pass those here.
    """
    for record in source.records():
        if name is None or record.get("name") == name:
            yield record


def turn_records(source: RecordSource, turn_id: str) -> list[dict[str, Any]]:
    """Return every record that carries the given ``turn_id``."""
    return [r for r in source.records() if r.get("turn_id") == turn_id]


def find_record(source: RecordSource, *, name: str) -> dict[str, Any] | None:
    """Return the first record whose ``name`` matches, or ``None``."""
    for record in iter_records(source, name=name):
        return record
    return None


# ── Assertion helpers ────────────────────────────────────────────
#
# Each helper raises ``AssertionError`` with a pytest-friendly message
# so failing tests surface the offending record payload, not just a
# boolean.  They stay deliberately independent of pytest so callers
# outside a test context (e.g. a CLI `replay --fail-on-regression`
# integration) can use them too.


def _assistant_text(record: dict[str, Any]) -> str:
    """Best-effort extraction of the assistant-reply text from a record.

    Prefers ``data.text`` (what the session journal sink writes for
    ``agent_final`` / ``stt_final``) and falls back to a top-level
    ``text`` key for bundle variants that flatten it.
    """
    data = record.get("data") or {}
    if isinstance(data, dict) and "text" in data:
        return str(data["text"])
    if "text" in record:
        return str(record["text"])
    return ""


def assert_exact_match(
    source: RecordSource,
    *,
    expected: str,
    event: str = "agent_final",
) -> None:
    """Assert an event's text field equals ``expected`` exactly.

    Matches Vapi Evals' "exact match" method: deterministic content
    checks are the baseline for non-semantic regressions.  Defaults to
    ``agent_final`` — the name the session journal sink emits for
    :class:`~easycat.events.AgentFinal`.
    """
    record = find_record(source, name=event)
    if record is None:
        raise AssertionError(f"no {event!r} record in bundle")
    actual = _assistant_text(record)
    if actual != expected:
        raise AssertionError(
            f"{event} text mismatch\n  expected: {expected!r}\n  actual:   {actual!r}"
        )


def assert_regex(
    source: RecordSource,
    *,
    pattern: str,
    event: str = "agent_final",
    flags: int = 0,
) -> None:
    """Assert an event's text field matches a regex pattern.

    Complement to :func:`assert_exact_match` for flexible checks like
    "mentions the user's name" or "ends with a question mark".
    """
    compiled = re.compile(pattern, flags)
    record = find_record(source, name=event)
    if record is None:
        raise AssertionError(f"no {event!r} record in bundle")
    actual = _assistant_text(record)
    if not compiled.search(actual):
        raise AssertionError(f"{event} text did not match /{pattern}/\n  actual: {actual!r}")


def assert_turn_completed(source: RecordSource, turn_id: str) -> None:
    """Assert the given turn emitted both ``turn_started`` and ``turn_ended``.

    Catches pipeline hangs where a turn starts but never resolves —
    the single most common "sessions feel broken" symptom.
    """
    records = turn_records(source, turn_id)
    names = {r.get("name") for r in records}
    if "turn_started" not in names:
        raise AssertionError(f"turn {turn_id!r} has no turn_started record")
    if "turn_ended" not in names:
        raise AssertionError(f"turn {turn_id!r} never completed (no turn_ended record)")


def assert_no_error(source: RecordSource, *, turn_id: str | None = None) -> None:
    """Assert no journal record carries an ``error`` payload.

    Scopes to a single turn when ``turn_id`` is provided so fixture
    bundles that deliberately include a neighbouring failed turn still
    exercise the happy-path assertion.
    """
    iterator = turn_records(source, turn_id) if turn_id else source.records()
    for record in iterator:
        err = record.get("error")
        if err:
            scope = f"turn {turn_id!r}" if turn_id else "bundle"
            raise AssertionError(
                f"{scope} contains an error record: "
                f"{err.get('type', '?')}: {err.get('message', '')} "
                f"(record={record.get('name')!r} seq={record.get('sequence')})"
            )


def assert_tool_called(
    source: RecordSource,
    *,
    tool_name: str,
    event: str = "tool_call_started",
) -> None:
    """Assert the agent invoked a specific tool at least once.

    Reads the ``tool_name`` field that the session journal sink writes
    from :class:`~easycat.events.ToolCallStarted` (see
    ``_JOURNAL_ATTRS`` in ``session/_session.py``).
    """
    for record in iter_records(source, name=event):
        data = record.get("data") or {}
        if isinstance(data, dict) and data.get("tool_name") == tool_name:
            return
    raise AssertionError(
        f"tool {tool_name!r} was never invoked (no {event} record with tool_name={tool_name!r})"
    )


# ── Latency assertion ────────────────────────────────────────────


_LATENCY_PERCENTILES = ("p50", "p90", "p95", "p99")


def assert_latency(
    results: TurnResult | float | Iterable[TurnResult | float],
    *,
    max_ms: float,
    percentile: str = "p95",
) -> None:
    """Assert turn latency stays under ``max_ms`` at the given percentile.

    Accepts one :class:`TurnResult`, one raw millisecond value, or any
    iterable mixing the two.  Percentiles are computed with the same
    :class:`~easycat.validation.latency.LatencyPercentileStats`
    nearest-rank code that backs ``easycat validate latency``, so a
    budget asserted here means the same thing in CI and release lanes.
    """
    from easycat.validation.latency import LatencyPercentileStats

    if percentile not in _LATENCY_PERCENTILES:
        raise ValueError(
            f"percentile must be one of {', '.join(_LATENCY_PERCENTILES)}; got {percentile!r}"
        )

    if isinstance(results, (TurnResult, int, float)):
        results = [results]
    values = [r.latency_ms if isinstance(r, TurnResult) else float(r) for r in results]
    if not values:
        raise AssertionError("assert_latency() received no latency samples")

    stats = LatencyPercentileStats.from_values(values)
    observed = getattr(stats, percentile)
    if observed is None or observed > max_ms:
        raise AssertionError(
            f"latency {percentile} {observed:.1f} ms exceeds budget {max_ms:.1f} ms "
            f"(samples={stats.count}, p50={stats.p50}, p95={stats.p95}, p99={stats.p99})"
        )


# ── LLM-as-judge ─────────────────────────────────────────────────

_DEFAULT_LLM_JUDGE_MODEL = "gpt-5.6-luna"


JUDGE_RUBRIC = """You are evaluating a single voice-bot turn.

Score each dimension 1 (awful) to 5 (excellent):

- relevance: did the bot answer what was actually asked?
- fluency: was the reply well-phrased for speech?
- appropriate_length: was the reply the right length for a voice turn?

Return JSON with keys {relevance, fluency, appropriate_length, reasoning}.
"""


def extract_transcript(source: RecordSource | TurnResult) -> str:
    """Render a ``User: ... / Bot: ...`` transcript from a record source.

    For a :class:`TurnResult` the captured input/response pair is used
    directly.  For bundles, user lines come from ``stt_final`` (session
    sink) or ``stt.final`` (stage-level) records and bot lines from
    ``agent_final`` with a ``stage.tts.execute`` fallback — the same
    vocabulary the teaching chapter 12 bundles use.
    """
    if isinstance(source, TurnResult):
        return f"User: {source.user_input}\nBot: {source.response}"

    user_lines: list[str] = []
    bot_lines: list[str] = []
    tts_lines: list[str] = []
    for record in source.records():
        name = record.get("name")
        data = record.get("data") or {}
        text = str(data.get("text", "")) if isinstance(data, dict) else ""
        if name in ("stt_final", "stt.final"):
            user_lines.append(text)
        elif name == "agent_final":
            bot_lines.append(text)
        elif name == "stage.tts.execute":
            tts_lines.append(text)
    return "User: " + " ".join(user_lines) + "\nBot: " + " ".join(bot_lines or tts_lines)


async def assert_llm_judge(
    source: RecordSource | TurnResult,
    *,
    min_score: int = 4,
    rubric: str = JUDGE_RUBRIC,
    model: str = _DEFAULT_LLM_JUDGE_MODEL,
    judge: Callable[[str, str], Awaitable[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Judge a turn's transcript with an LLM rubric; assert every score.

    Promoted from teaching chapter 12 (``llm_judge.py``).  The default
    judge calls OpenAI (requires ``OPENAI_API_KEY`` and the ``openai``
    package); pass ``judge=`` — any ``async (transcript, rubric) ->
    mapping`` — to substitute a stub in offline tests or another
    provider in CI.

    Every numeric dimension in the verdict must be ``>= min_score``.
    Returns the full verdict dict (including ``reasoning``) so tests
    can log or further inspect it.  Not a replacement for human evals:
    LLM-as-judge is a fast triage layer (~95% human agreement on most
    rubrics), nothing more.
    """
    transcript = extract_transcript(source)
    if judge is None:
        judge = _openai_judge(model)
    verdict = dict(await judge(transcript, rubric))

    scores = {k: v for k, v in verdict.items() if isinstance(v, (int, float))}
    if not scores:
        raise AssertionError(f"LLM judge returned no numeric scores: {verdict!r}")
    failing = {k: v for k, v in scores.items() if v < min_score}
    if failing:
        raise AssertionError(
            f"LLM judge scored below min_score={min_score}: {failing!r}\n"
            f"  reasoning: {verdict.get('reasoning', '')!r}\n"
            f"  transcript: {transcript!r}"
        )
    return verdict


def _openai_judge(model: str) -> Callable[[str, str], Awaitable[Mapping[str, Any]]]:
    """Build the default OpenAI-backed judge callable."""
    import json
    import os

    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "assert_llm_judge() needs OPENAI_API_KEY for the default judge; "
            "set the key or pass judge= explicitly."
        )
    from openai import AsyncOpenAI

    client = AsyncOpenAI()

    async def _judge(transcript: str, rubric: str) -> Mapping[str, Any]:
        request: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": rubric},
                {"role": "user", "content": transcript},
            ],
            "response_format": {"type": "json_object"},
        }
        if model == _DEFAULT_LLM_JUDGE_MODEL:
            request["reasoning_effort"] = "none"
        resp = await client.chat.completions.create(**request)
        raw = resp.choices[0].message.content or "{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"error": "judge returned non-JSON", "raw": raw}

    return _judge
