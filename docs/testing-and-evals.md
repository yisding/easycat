# Testing and Evals

How to test an EasyCat agent, from fully offline regression tests to
live-audio validation. Work up the ladder; each rung costs more (keys,
time, flakiness) and catches a different class of failure. Every rung
shares one vocabulary: journal records, read through the helpers in
`easycat.debug.testing`.

## The ladder

| Rung | What runs | Needs keys? | Catches |
| --- | --- | --- | --- |
| 1. Bundle fixtures | Assertions over a checked-in `RunBundle` | No | Regressions in recorded behavior |
| 2. Text turns | One real agent-bridge turn via `send_text` (Noop audio) | No (stub agent) / yes (real agent) | Turn pipeline, tool calls, latency budgets |
| 3. Metrics and judges | Percentiles, WER, barge-in F1, LLM-as-judge | Judge needs `OPENAI_API_KEY` | Conversational quality drift |
| 4. Live audio | Full pipeline against real providers | Yes | Provider and audio-path integration |

## Rung 1 — bundle fixtures

Export a bundle from a failing session (`session.export_debug_bundle(...)`),
check it into your test fixtures, and assert on its records. The
`easycat_bundle` pytest fixture ships with the library:

```python
from easycat.debug.testing import assert_no_error, assert_tool_called


def test_refund_flow_regression(easycat_bundle):
    # Point this at a bundle exported from your own regression session.
    bundle = easycat_bundle("artifacts/regression/refund_flow.zip")
    assert_no_error(bundle)
    assert_tool_called(bundle, tool_name="lookup_order")
```

The helper suite — `assert_exact_match`, `assert_regex`,
`assert_turn_completed`, `assert_no_error`, `assert_tool_called` —
raises plain `AssertionError`s with the offending record payload, so
it also works outside pytest.

## Rung 2 — text turns

`run_text_turn()` drives one real agent-bridge turn through the same
`send_text` path the text-chat scaffold uses, with Noop audio stages.
It accepts a live text session, a `TextSessionConfig` / `EasyConfig`,
or any agent object, and returns a journal-backed `TurnResult` that
the rung-1 helpers accept directly:

```python
from easycat.debug.testing import (
    assert_latency,
    assert_no_error,
    assert_turn_completed,
    run_text_turn,
)


async def test_agent_turn():
    result = await run_text_turn(my_agent, "What are your hours?")
    assert_turn_completed(result, result.turn_id)
    assert_no_error(result, turn_id=result.turn_id)
    assert "open" in result.response.lower()
    assert_latency(result, max_ms=5000.0, percentile="p95")
```

`assert_latency` reuses the nearest-rank percentile code behind
`easycat validate latency`, so a budget asserted in a unit test means
the same thing as one enforced in a validation lane. Every scaffolded
project ships this pattern as `tests/test_agent.py` — offline and
key-free via a stub agent — plus an `AGENTS.md` documenting it; run it
with `uv run pytest` inside the project. The repo's own coverage for
these helpers lives in the debug test suite:

```bash
uv run pytest tests/debug/test_testing_helpers.py
```

## Rung 3 — metrics and judges

Teaching chapter 12 ([`docs/teaching/12-evals-and-latency`](teaching/12-evals-and-latency/))
walks through P50/P95 latency, WER, and barge-in F1 over checked-in
bundles. Its LLM-as-judge rubric is promoted into the library as
`assert_llm_judge`:

```python
from easycat.debug.testing import assert_llm_judge


async def test_turn_quality():
    result = await run_text_turn(my_agent, "I want a refund")
    verdict = await assert_llm_judge(result, min_score=4)
```

The default judge calls OpenAI (set `OPENAI_API_KEY`); pass `judge=`
— any `async (transcript, rubric) -> mapping` — to keep CI offline or
to use another provider. The standalone chapter script still works on
any exported bundle:

```bash
uv run python docs/teaching/12-evals-and-latency/llm_judge.py docs/teaching/12-evals-and-latency/bundles/turn_01_fast.bundle
```

## Rung 4 — live audio

Once text-level behavior is pinned down, validate the full audio
pipeline against real providers with the validation lanes:

```bash
uv run easycat validate latency --smoke
uv run easycat validate live --provider openai
```

If credentials live in `.env`, preflight and run through that file:

```bash
uv run easycat doctor --env-file .env
uv run easycat doctor --env-file .env --json
uv run --env-file .env easycat validate latency --smoke
uv run --env-file .env easycat validate live --provider openai --strict
```

Use `--strict` when an explicitly requested provider should fail fast if its
secrets are missing; omit it when you want a capability report that skips
unavailable live providers. Reports land in `.easycat/validation/latest.json`:

```bash
uv run easycat validate report .easycat/validation/latest.json
```

See the [validation workflow](validation.md) for lane selection and report
inspection.
