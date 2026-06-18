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
    bundle = easycat_bundle("tests/fixtures/refund_flow.zip")
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
the same thing as one enforced in a validation lane. Latency and cost
budgets share one value-object API in `easycat.budgets`: `LatencyBudget`
(re-exported from `easycat.validation.latency`, so the legacy import path
keeps working) and the net-new `CostBudget` (with the
`max_session_cost_usd` config alias). `easycat.budgets.build_budget_report`
evaluates a budget set against runtime journal records and the offline
validation percentile columns through one builder, so the eval runner,
debugger, CLI, and validation surfaces report budgets the same way. Every
scaffolded
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

## Scenario runner — `easycat.evals`

Multi-turn conversation scenarios live in `easycat.evals` (a submodule
export — `from easycat.evals import ...`). A scenario is a named list of
turns plus optional latency/cost budgets:

```python
from easycat.evals import EvalRunner, EvalScenario, EvalTurn
from easycat.budgets import CostBudget, LatencyBudget

scenario = EvalScenario(
    name="refund_flow",
    turns=[EvalTurn(user="I need a refund", expect_response_regex="refund|order")],
    budgets=[
        LatencyBudget(stage="total_ms", max_ms=1500),
        CostBudget(max_session_usd=0.05),
    ],
)
result = await EvalRunner(my_agent).run(scenario)
assert result.passed
```

`EvalRunner` replays each turn against a text-mode session (audio stages
are Noop stubs, no API keys needed) and returns a journal-backed
`ScenarioResult` whose `.records()` works with the same `assert_*`
helpers re-exported from `easycat.evals`. Budgets are evaluated through
the shared `build_budget_report` from `easycat.budgets`; use
`assert_budgets_pass(result.budget_report)` to assert them directly.

**Text-mode budget rule.** Text turns emit only `total_ms` latency
records, so text scenarios may assert turn-total (`total_ms`) latency
budgets plus cost budgets. A provider-stage latency budget
(`tts_ttfb_ms`, `llm_ttft_ms`) attached to a text scenario would
evaluate against zero samples; rather than passing vacuously the runner
raises a clear `no samples for stage X` error. Provider-stage latency
budgets become meaningful once the audio-simulation runner lands.

Scenario files are YAML or JSON:

```yaml
name: refund_flow
budgets:
  latency:
    - stage: total_ms
      max_ms: 1500
  cost:
    max_session_cost_usd: 0.05
turns:
  - user: "I need a refund"
    expect:
      response_regex: "refund|order"
      tools:
        - lookup_order
```

Run them from the CLI; `--json` emits the eval report envelope
(`schema_version=1`):

```bash
uv run easycat eval run tests/evals/refund_flow.yaml
uv run easycat eval run tests/evals --json
uv run easycat eval report .easycat/evals/latest.json
uv run easycat eval report .easycat/evals/latest.json --json
```

## Promote a recorded turn into a test

`easycat eval promote` turns one recorded turn into a committed rung-1
regression test. It is the hardened, FORKED replacement for the legacy
`journal promote` command and is **safe by default**:

```bash
uv run easycat eval promote PATH TURN_ID --out tests/test_regressions.py
```

It writes two artifacts next to `--out`: a redacted single-turn slice
(`<out>.bundle`) and the `.py` test that loads it via the `easycat_bundle`
fixture. The generated test imports its helpers from `easycat.evals`, so it
runs under `pytest` immediately.

Hardened defaults:

- **Redact by default.** Every promoted record is routed through
  `redact_value`, and the reply/transcript `text` fields are replaced with
  redaction placeholders, so the verbatim conversation is never committed.
- **`--no-audio` is the default.** Audio blobs (and their `input_ref` /
  `output_ref` pointers) are dropped unless you pass `--include-audio`.
- **PII tripwire.** Before writing, the serialized slice is scanned with
  `contains_unredacted_sensitive_text`; promotion RAISES unless `--allow-pii`
  is explicitly set.
- **Hash-by-default assertion.** `--assert-on hash` (default) pins a stable
  hash of the redacted reply. `--assert-on regex` is the redaction-safe
  alternative; `--assert-on exact` embeds the verbatim reply and is opt-in
  (it warns), so it should only be used with `--allow-pii` on already-safe
  text.

`--json` emits the promotion envelope (`schema_version=1`).

> **Legacy `journal promote`.** The older `journal promote PATH TURN_ID --out
> slice.zip` command copies the full raw NDJSON, every audio blob, and the
> verbatim reply into the slice with **zero redaction**. It is retained only
> for back-compat; prefer `eval promote` for anything committed to a repo.

## Rung 4 — live audio

Once text-level behavior is pinned down, validate the full audio
pipeline against real providers with the validation lanes:

```bash
uv run easycat validate latency --smoke
uv run easycat validate live --provider openai
```

Reports land in `.easycat/validation/latest.json`; see the
[validation workflow](validation.md) for lane
selection and report inspection.
