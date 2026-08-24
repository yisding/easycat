# Chapter 8: Test the Experience

A voice app can be functionally correct and still feel wrong: it may choose the
wrong tool, ramble, fail to finish a turn, or become too slow. EasyCat's testing
surface lets one captured record vocabulary support fast unit tests, reviewed
bundle fixtures, quality judges, and live validation.

This chapter starts with the cheapest useful rung: deterministic text turns
through the real agent bridge and journal, with no audio or credentials.

## Prerequisites

- Complete [chapter 7](../07-observability/), or understand that assertion
  helpers read journal-backed records.
- Run `uv sync --group dev` from the repository root.
- No API keys, microphone, audio providers, or network are needed for the
  chapter checkpoint.
- Live latency validation and the default LLM judge are optional later rungs
  and need the corresponding provider credentials.

## Run the offline eval suite

```bash
uv run python docs/using-easycat/08-testing-evals/main.py
```

The script evaluates two cases and prints output like:

```text
PASS hours: Support is open from nine to five Pacific time. (... ms, relevance=5)
PASS refund: I can help with a refund. What is your order number? (... ms, relevance=5)
PASS latency: p95 <= 5000.0 ms across 2 turns
```

The exact timings vary. The behavior and pass/fail conditions do not.

Prove that the latency assertion is a gate rather than a log line:

```bash
uv run python docs/using-easycat/08-testing-evals/main.py --max-ms 0
```

That command exits nonzero with an `AssertionError` explaining the observed
P95 and the zero-millisecond budget.

## The testing ladder

Each rung catches a different failure class and costs more:

| Rung | Input | Runs | Best at |
|---|---|---|---|
| Bundle regression | Reviewed `RunBundle` fixture | No providers | Reproducing a captured failure |
| Text turn | Agent or text session | Agent bridge + journal, Noop audio | Logic, tools, turn completion, fast budgets |
| Metrics/judge | Many results or transcripts | Statistical metric or judge | Quality and distribution drift |
| Live validation | Real providers/audio path | Full integration | Credentials, provider behavior, end-to-end latency |

Do not replace the lower rungs with one live end-to-end test. Fast deterministic
checks should explain most regressions before a slower provider lane runs.

## `run_text_turn` uses the real turn machinery

`run_text_turn(agent, prompt)` builds a journaled text session, calls
`Session.send_text`, stops the session, and returns a `TurnResult` containing:

- `turn_id`, input, response, and measured `latency_ms`;
- the journal records created during that turn;
- the same `records()` protocol implemented by a loaded bundle.

STT, TTS, VAD, and the transport are Noop stages, but agent adaptation,
runner timeouts, tool-event translation, turn correlation, and latency metrics
are real. A passing text test does not validate speech providers; it isolates
the agent-facing contract from them.

For several stateful turns, create one `create_text_session(...)`, pass the
live session to `run_text_turn`, and own it with `async with`. Passing a bare
agent creates a fresh throwaway text session for each case, as this lesson does.

## Assertions work on turns and bundles

The core helpers raise plain `AssertionError`, so they work in pytest, another
test runner, or this standalone script:

- `assert_exact_match` pins deterministic response text.
- `assert_regex` allows intentional variation while requiring a contract.
- `assert_turn_completed` catches a started turn that never ended.
- `assert_no_error` rejects journaled failures, optionally per turn.
- `assert_tool_called` requires a named tool lifecycle event.
- `assert_latency` enforces a percentile budget.

The first case uses exact match; the second uses a regex. Both also verify turn
completion and absence of errors.

The same helpers accept a reviewed bundle fixture:

```python
from easycat.debug.testing import assert_no_error, load_bundle

bundle = load_bundle("tests/fixtures/refund.bundle")
assert_no_error(bundle)
```

Replace that placeholder path with a reviewed `.bundle` fixture from your own
project.

Promote a production failure into a fixture only after reviewing and reducing
its sensitive contents. Normal debug bundles may contain PII.

## Eval cases separate input from oracle

`EvalCase` keeps the prompt and expected behavior outside `SupportAgent`. The
agent cannot inspect its own test oracle, and adding a case does not add a
branch whose only purpose is to make that case pass.

In a project test suite, turn the same data into parametrized pytest cases:

```python
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_support_case(case):
    result = asyncio.run(run_text_turn(SupportAgent(), case.prompt))
    assert_no_error(result, turn_id=result.turn_id)
    # Apply the case's exact/regex/tool oracle here.
```

An eval suite is a collection of cases, record-backed assertions, and metrics.
There is no separate `easycat eval` CLI command; keep deterministic cases in
normal tests so they use the project's ordinary collection and reporting.

## Judges are one oracle, not ground truth

`assert_llm_judge` renders a user/bot transcript, applies a rubric for
relevance, spoken fluency, and appropriate length, and requires every numeric
score to meet `min_score`.

By default it calls an OpenAI judge. Passing `judge=` injects any async
`(transcript, rubric) -> mapping` function. The lesson's
`deterministic_judge` only verifies that injection, transcript, rubric, and
threshold plumbing work offline. It always returns known scores and therefore
does not measure semantic quality.

Use a real judge to triage a larger dataset, then calibrate it against human
review. Pin the judge model/rubric, retain reasoning for failures, and never
treat one model's score as unquestionable truth.

## Report latency or gate latency deliberately

EasyCat records latency; it does not fail a running user session because a
turn was slow. Choose a testing/validation layer to enforce a budget:

- `TurnResult.latency_ms` is one observed text-turn value.
- `assert_latency(results, max_ms=..., percentile="p95")` fails a local test.
- `easycat latency BUNDLE --json` summarizes captured milestone percentiles but
  does not enforce a budget.
- `easycat validate latency --smoke --json` is a low-sample live integration
  probe. It intentionally omits tail-budget evaluation.
- `easycat validate latency --sweep --require-samples --json` collects the
  broader live sample set and enforces configured/default budgets.
- Add `--baseline PATH` to compare eligible current samples with a stored
  latency artifact and detect regression/drift.

`assert_latency` and validation use the same percentile vocabulary and
implementation (`p50`, `p90`, `p95`, `p99`). This lesson uses a generous
5-second P95 budget for a deterministic stub: it catches a pipeline hang
without turning scheduler noise on a shared CI worker into a flaky benchmark.

For a chapter-7 bundle, report its captured percentiles with:

```bash
uv run easycat latency .easycat/tutorial/ch07/baseline.bundle --json
```

That text-only bundle has no audio milestones, so many speech-specific cells
are null. Use live validation when the question is STT endpointing, LLM TTFT,
TTS first byte, or full speech-to-speech delay.

## Graduate to live validation last

Preflight the environment before provider lanes:

```bash
uv run easycat doctor
uv run easycat doctor --json
uv run easycat validate latency --smoke --json
```

If keys live in `.env`:

```bash
uv run easycat doctor --env-file .env
uv run easycat doctor --env-file .env --json
uv run --env-file .env easycat validate latency --smoke --json
```

Use the sweep in a scheduled/release lane where credentials, sample cost, and
runtime variance are expected. Save its structured artifact and compare only
compatible conditions; a different provider, region, or pipeline signature is
not a valid like-for-like baseline.

Continue with [the exercises](./EXERCISES.md) to add a case, make an assertion
fail usefully, and promote a bundle into a regression fixture.

## What you should be able to answer now

> Does `run_text_turn` test STT and TTS?

No. It tests the real agent-turn path with Noop audio stages.

> Why does latency smoke not fail the default tail budget?

Its sample count is intentionally too small for meaningful tail enforcement;
the sweep is the budgeted live lane.

> Is an injected always-five judge a quality eval?

No. It is a deterministic contract test for judge plumbing.

## What's next

Chapter 9 moves from one caller to many: per-connection factories,
authentication, limits, and supervised shutdown.
