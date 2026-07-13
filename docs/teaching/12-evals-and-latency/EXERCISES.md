# Chapter 12 — Exercises

<!-- BEGIN auto:navigation -->
[← Chapter narrative](./README.md) · [Teaching ladder](../) · [Chapter 13 — Swap Providers AND Transports →](../13-swap-providers-and-transports/)
<!-- END auto:navigation -->

<!-- BEGIN auto:exercise-protocol -->
> **Completion evidence for every task**
>
> 1. **Before hints:** keep your initial prediction or plan.
> 2. **After the attempt:** keep the exact command or change and one observed field,
>    measurement, or behavior.
> 3. **Before moving on:** explain in one sentence why the evidence supports or changes
>    your model.
>
> A task is complete when all three are present. Keep a wrong first answer visible;
> it is evidence to explain after revealing hints, not an answer to rewrite.
<!-- END auto:exercise-protocol -->

## 1. Find the budget-blower, propose a fix without coding it

**Task.** Run `latency_budget.py` over each of the six primary
bundles. Identify the slowest. Which stage blew its budget?
Propose a fix — model swap, prompt cache, warmer pool, smarter
turn detection — *without implementing it*. The point is
diagnosis.

<!-- BEGIN auto:exercise-hints -->
<details markdown="1">
<summary>Reveal hints after your first attempt</summary>

**Hints**

1. `turn_02_slow_agent.bundle` is the obvious one — `agent` is at
   2100 ms vs 600 ms budget. Possible fixes: smaller model
   (gpt-4o-mini → gpt-4o-nano), prompt caching for the system
   prompt, agent warm-pool (keep one open connection per session
   to avoid TLS handshake).
2. Don't conflate "stage blew its budget" with "stage is the
   biggest absolute cost." A stage at 400 ms vs a 200 ms budget
   is more interesting than a stage at 600 ms vs a 1000 ms
   budget — the first is *drift*, the second is *normal*.
3. The point of a budget isn't to be a timeout. It's a drift
   detector — you set a threshold based on your historical P50
   and alarm when the live number consistently breaks past it.

</details>
<!-- END auto:exercise-hints -->

## 2. Add a `filler_appropriate` rubric dimension

**Task.** Add a `filler_appropriate` dimension to the LLM-judge
rubric. Re-run on the chapter-7 tool-bearing bundles. Does the
judge agree with your ears?

<!-- BEGIN auto:exercise-hints -->
<details markdown="1">
<summary>Reveal hints after your first attempt</summary>

**Hints**

1. The rubric lives in `llm_judge.py`. Extend the prompt and
   `SCORE_KEYS` with a `filler_appropriate: 1-5` field. The
   judge's evaluation prompt should ask whether filler utterances
   landed at appropriate moments and matched the tool that was
   running.
2. The interesting failure mode is when the bundle is *technically
   correct* (filler played at the right time) but the rubric
   marks it 3/5 because the *text* of the filler is wrong for the
   tool ("Let me check the weather for you" before a *timer* tool
   call).
3. LLM-as-judge is most useful when the rubric dimension is
   text-only — anything that requires audio judgement (prosody,
   pacing, naturalness) is invisible to it.
4. Copy a `tools_*.bundle` from chapter 7's runs/ into chapter
   12's bundles/ for the judge to consume.

</details>
<!-- END auto:exercise-hints -->

## 3. Wire a latency regression test

**Task.** Write a pytest test that fails if the fixture set's P95 exceeds
1200 ms. That's the seed of a latency regression suite.

<!-- BEGIN auto:exercise-hints -->
<details markdown="1">
<summary>Reveal hints after your first attempt</summary>

**Hints**

1. Start in `tests/teaching/` with a focused test file, for example
   `test_latency_budget.py`. Use `easycat.debug.testing.load_bundle` to load
   each fixture, the same `turn.gap` extraction as `evals.py`, and
   `easycat.validation.latency.LatencyPercentileStats.from_values` for the
   percentile. Reusing the maintained helper keeps the test aligned with the
   debug CLI.
2. Hard-coded thresholds are fine for the teaching version. For
   production, compare representative baseline and candidate turns
   under matched conditions, predeclare an absolute/relative tolerance,
   and quantify uncertainty before failing.
3. The six chapter-12 fixtures include `turn_02_slow_agent`, which is
   *deliberately* 2420 ms to first audio. With this small sample, the maintained
   clamped-exclusive P95 is the observed maximum, so your test should flag it.
   That's the right behavior: the test catches the slowdown the fixture was
   built to represent.
4. Bonus: also test that the golden WER bundles produce the
   numbers their filenames advertise. That's a regression test
   for the WER pipeline itself.

</details>
<!-- END auto:exercise-hints -->

## 4. Break coverage before calculating a score

**Task.** Run the provider-free coverage probe, then remove one row
from a copy of `ground_truth.csv` and run `evals.py` against it:

```bash
uv run python docs/teaching/12-evals-and-latency/coverage_probe.py
```

Why is a hard failure better than computing latency from the remaining
bundles?

<!-- BEGIN auto:exercise-hints -->
<details markdown="1">
<summary>Reveal hints after your first attempt</summary>

**Hints**

1. Missing labels change WER and F1 denominators. Missing `turn.gap`
   records selectively remove failed/no-audio turns from latency.
2. A warning is easy to miss in CI; a non-zero exit prevents an
   incomplete manifest from becoming the new baseline.
3. Put two `stt.final` records in one fixture. The evaluator asks you
   to split and label the turns instead of guessing which transcript
   belongs to the bundle-level CSV row.
4. Coverage is not a fifth quality metric. It is the precondition that
   makes the other four interpretable.

</details>
<!-- END auto:exercise-hints -->

## 5. Find the turn that controls P95

**Task.** Run the provider-free sensitivity probe:

```bash
uv run python docs/teaching/12-evals-and-latency/p95_sensitivity_probe.py
```

Before reading the output, predict the P95 after omitting each fixture.
Then explain what the 1,160–2,420 ms range proves—and what it cannot
prove.

<!-- BEGIN auto:exercise-hints -->
<details markdown="1">
<summary>Reveal hints after your first attempt</summary>

**Hints**

1. With this maintained small-sample percentile rule, the slowest
   observed turn owns P95. Removing any non-maximum turn leaves 2,420 ms.
2. Removing `turn_02_slow_agent.bundle` exposes the next-slowest tool
   turn at 1,160 ms, a -1,260 ms change.
3. Leave-one-out sensitivity identifies influential observed samples.
   It is not a confidence interval and says nothing about unobserved
   production turns or whether the fixture mix is representative.
4. A candidate comparison needs repeated, representative measurements.
   Pair baseline/candidate observations by prompt, environment, and
   provider conditions when possible so traffic-mix changes do not
   masquerade as a latency win.

</details>
<!-- END auto:exercise-hints -->

## 6. (Bonus) Build a real eval set

**Task.** Record 20 of your own chapter-6 or chapter-10 turns,
hand-type the reference transcripts into a CSV, and run
`evals.py` against the directory.

<!-- BEGIN auto:exercise-hints -->
<details markdown="1">
<summary>Reveal hints after your first attempt</summary>

**Hints**

1. Real numbers feel different. Twenty turns are better than six, but
   no fixed sample count guarantees precision. Report the number of
   turns and reference words, then estimate uncertainty—for example by
   bootstrapping turns—before claiming a small regression.
2. Use a mix of clean and adversarial inputs (whisper, fast
   speech, accented speech, background TV) to stress different
   stages.
3. This is the unglamorous part of voice eval: building a
   ground-truth set. Production teams maintain hundreds to
   thousands of these. The six fixtures here are training
   wheels.

</details>
<!-- END auto:exercise-hints -->

## Self-check

You should be able to: (a) read a bundle and within 30 seconds
say "this turn's bottleneck was X", (b) explain why F1 over
TP/FP/FN/TN is the right shape for barge-in (rather than raw
accuracy), and (c) describe one regression each of the four
metrics catches that the others miss, and (d) explain why coverage
must be validated before any point estimate is reported, and (e)
distinguish an influential-sample diagnostic from uncertainty about a
production percentile.

<!-- BEGIN auto:exercise-completion -->
---
Self-check complete? Prepare the cumulative spine, then replay it through this chapter:

```bash
uv sync --extra quickstart --group dev
uv run python docs/teaching/offline_spine.py --run --through 12 --jobs 4 --show-evidence
```

- [Review the chapter narrative](./README.md)
- [Continue to Chapter 13 — Swap Providers AND Transports →](../13-swap-providers-and-transports/)
<!-- END auto:exercise-completion -->
