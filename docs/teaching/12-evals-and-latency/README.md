# Chapter 12 — Evals + the Latency Budget

<!-- BEGIN auto:navigation -->
**Progress: 13 of 16** · [← Chapter 11 — The Journal as Mental Model](../11-journal/) · [Ladder index](../) · [Progress worksheet](../PROGRESS.md) · [Exercises](./EXERCISES.md) · [Chapter 13 — Swap Providers AND Transports →](../13-swap-providers-and-transports/)
<!-- END auto:navigation -->

> The difference between *building* a voice bot and *operating* one
> is measurement. This chapter produces four concrete numbers:
> P50/P95 latency, WER, barge-in F1, and an LLM-as-judge score.

<!-- BEGIN auto:spaced-retrieval -->
## Recall before reading

> **Following the ladder? Spaced retrieval — Chapter 10 — Cleaning the Signal**
>
> Close earlier chapters and answer from memory before reading further. If this
> chapter is your starting point, skip this block.
>
> **Answer from memory:**
>
> What changes with aligned AEC reference audio, and what should fail when reference audio is
> missing or short?
>
> After recording your answer, explain one way `NR/AEC replay metrics` changes how you reason
> about `small-sample P95 sensitivity`. Keep the first answer visible.
>
> **Check only after answering:**
>
> ```bash
> uv run python docs/teaching/10-cleaning-signal/replay_metrics_probe.py
> ```
>
> Cite one observed field, measurement, or behavior; repair only the part your
> evidence disproved.
<!-- END auto:spaced-retrieval -->

<!-- BEGIN auto:offline-checkpoint -->
> **Hardware-free checkpoint:** prove `small-sample P95 sensitivity` without a microphone,
> speakers, or provider credentials:
>
> **Predict first:** Which bundle controls P95, and how far should P95 move when that bundle is
> removed?
>
> ```bash
> uv run python docs/teaching/12-evals-and-latency/p95_sensitivity_probe.py
> ```
>
> **Evidence to find:** removing `turn_02_slow_agent.bundle` alone drops P95 by 1,260 ms.
>
> **Explain the result:** Explain why one slow bundle controls a small-sample percentile and what
> not to generalize.
>
> [See all 16 checkpoints](../#hardware-free-checkpoint-spine).
<!-- END auto:offline-checkpoint -->

## Prerequisites

- [Chapter 11](../11-journal/). You need to be comfortable
  opening a bundle and querying records.
- `uv sync --group dev`. The LLM-judge script additionally
  wants `OPENAI_API_KEY`; before running it, run
  `uv run easycat doctor` from the repo root. If the key lives in `.env`, run
  `uv run easycat doctor --env-file .env`. Use `uv run easycat doctor --env-file .env --json` for parseable checks.
- The optional LLM judge makes a live provider call that may incur charges.
  Review your provider billing and usage limits before running it.
- The optional LLM judge sends eval content to the configured provider. Use
  non-sensitive test data and review provider data-handling policies first.
- If the key lives in `.env`, also add `--env-file .env` after `uv run`
  in the chapter command you run.

> **Minimum to skip the ladder:** chapter 11 (journal queries).
> The aggregate evaluator works on single-turn bundles that follow the
> teaching shape and have matching CSV labels—you do not have to have
> built the pipeline.

## Diff from chapter 11

- **Added:** `latency_budget.py` (per-bundle latency vs budget),
  `evals.py` (aggregate P50/P95, WER, barge-in F1),
  `llm_judge.py` (LLM-as-judge triage); `generate_bundles.py`
  builds the eval set; `ground_truth.csv` ties each bundle to its
  reference transcript and labels; `coverage_probe.py` makes
  incomplete-manifest failures visible; `bundles/golden/` (added by
  this chapter's enhancement) — three bundles with controlled,
  reproducible WER values (5%, ~10%, ~25%) so the WER pipeline
  can be exercised without recording your own speech.
- **Removed:** still no live pipeline. This chapter is pure
  measurement.

## The six pre-recorded bundles

```bash
uv run python docs/teaching/12-evals-and-latency/generate_bundles.py \
    --output-root .easycat/teaching/12-evals-and-latency
```

The fixtures used below are already checked in. The command above is a
safe way to experiment: it writes a chapter-shaped copy under the
gitignored `.easycat/` directory instead of rewriting tracked bundles.
Maintainers intentionally refreshing the checked-in fixtures omit
`--output-root` and review the resulting bundle and CSV diffs.

- `turn_01_fast.bundle` — clean, fast turn.
- `turn_02_slow_agent.bundle` — agent is slow; P95 spike.
- `turn_03_ghost_interrupt.bundle` — the bot cancelled itself
  when the user never spoke (ch 11 bug 3 flavour).
- `turn_04_real_interrupt.bundle` — the user legitimately
  interrupted.
- `turn_05_medium.bundle` — middle of the pack.
- `tools_01_weather.bundle` — two chapter-7-style tool calls
  (`get_weather`, `set_timer`) plus three TTS sentences. Use this
  one for exercise 2.

`ground_truth.csv` ties each bundle to its reference transcript
and whether the interruption (if any) was real.

> **Bundle safety.** These scripts load ZIP/JSON bundles via
> `load_bundle`. Bundles are not signed or tamper-evident, so only
> feed them bundles you generated or trust. See ch 11's README for
> the full note.

## Coverage before scores

A point estimate is only honest when you know which examples were
eligible to contribute. The chapter evaluator therefore uses a strict
fixture contract:

- every `*.bundle` has exactly one CSV row, and every CSV row names an
  existing bundle;
- `had_real_barge_in` is exactly `0` or `1`—a typo such as `yes` is not
  silently interpreted as negative;
- every bundle contains exactly one `stt.final` and one `turn.gap`;
- missing first-audio turns fail the run instead of disappearing from
  the latency percentile.

One fixture represents one labeled turn. If you start from a multi-turn
production bundle, split its turns into separately named fixtures and
label each one; taking the last transcript and last gap would mix the
wrong evidence.

Run the provider-free failure probe:

```bash
uv run python docs/teaching/12-evals-and-latency/coverage_probe.py
```

A valid run of `evals.py` begins with matching coverage counts before it
prints any metric:

```text
=== Coverage ===
  bundles=6  labels=6  latency=6  WER=6  barge-in=6
```

## The golden WER fixtures (`bundles/golden/`)

The six fixtures above are useful for **latency** and **barge-in
F1** but they all happen to have STT hypotheses identical to their
reference transcripts — so the WER pipeline reports 0.0% across
the board. That tells you the pipeline runs without errors; it
does not tell you it produces the right numbers.

The golden set adds three bundles with hand-tuned, reproducible
WER values:

| Bundle                          | Edits         | Ref words | WER  |
|---------------------------------|---------------|----------:|-----:|
| `golden_01_wer_5pct.bundle`     | 1 substitute  |        20 | 5.0% |
| `golden_02_wer_10pct.bundle`    | 1 substitute  |        10 | 10.0%|
| `golden_03_wer_25pct.bundle`    | 1 sub + 1 del |         8 | 25.0%|

Aggregate (across the three): **10.5%**.

Run `evals.py` against the golden directory:

```bash
uv run python docs/teaching/12-evals-and-latency/evals.py \
    docs/teaching/12-evals-and-latency/bundles/golden \
    docs/teaching/12-evals-and-latency/bundles/golden/ground_truth.csv
```

You should see exactly those four numbers (5.0%, 10.0%, 25.0%,
aggregate 10.5%). If you don't, the WER implementation has
drifted — fix it before trusting the numbers on real recordings.

The same goldens are also the seed for a regression test
(exercise 3): a pytest case that asserts `wer == 5.0%` on
`golden_01_*` is your tripwire if anyone modifies the edit-distance
code by accident.

## 1 — Latency budget, per bundle

Read one bundle's gap against a per-stage budget:

```bash
uv run python docs/teaching/12-evals-and-latency/latency_budget.py \
    docs/teaching/12-evals-and-latency/bundles/turn_02_slow_agent.bundle
```

Expected output:

```
=== turn_02_slow_agent.bundle ===
  stt final → first token          2100 ms     budget   600 ms    OVER
  first token → first audio         320 ms     budget   400 ms    OK
  stt final → first audio          2420 ms     budget  1000 ms    OVER
```

The budget isn't a timeout. It is a **drift detector**. When a
stage consistently runs hot against its budget, something has
shifted — model choice, cold cache, backlog. Budgets let you
*see* drift.

Industry-rough defaults for voice bots (per production voice
teams):

| Stage | Budget |
|---|---|
| VAD silence wait | 200-500 ms |
| STT final commit | 100-300 ms |
| Agent → first token | 300-1000 ms |
| Agent first token → first audio | 100-400 ms |
| **Total (STT final → first audio)** | **<1000 ms** |

The budgets here are defensible starting points; real numbers are
per-deployment.

The check follows one responsiveness milestone: the first audio
chunk the transport accepted. Summing every `stage.tts.execute`
span measures response-length-dependent synthesis throughput, not
how long the user waited to hear the bot start. Instead, pull
`stt.final`, `agent.first_token`, `tts.first_audio`, and `turn.gap`
from the journal, compare the critical-path spans against their
budgets, and label each `OK` or `OVER`:

<!-- BEGIN auto:snippet src=latency_budget.py symbol=measure -->
```python
def measure(path: Path) -> dict[str, float | None]:
    """Return the three first-audio critical-path measurements."""
    bundle = load_bundle(path)
    stt_final_t = None
    first_token_t = None
    first_audio_t = None
    total_gap = None

    for r in bundle.records():
        if r["name"] == "stt.final" and stt_final_t is None:
            stt_final_t = r["data"].get("t_ms")
        elif r["name"] == "agent.first_token" and first_token_t is None:
            first_token_t = r["data"].get("t_ms")
        elif r["name"] == "tts.first_audio" and first_audio_t is None:
            first_audio_t = r["data"].get("t_ms")
        elif r["name"] == "turn.gap" and total_gap is None:
            total_gap = r["data"].get("total_gap_ms")

    agent_dispatch_ms = (
        first_token_t - stt_final_t
        if stt_final_t is not None and first_token_t is not None
        else None
    )
    first_token_to_audio_ms = (
        first_audio_t - first_token_t
        if first_audio_t is not None and first_token_t is not None
        else None
    )
    return {
        "stt_final_to_first_token_ms": agent_dispatch_ms,
        "first_token_to_audio_ms": first_token_to_audio_ms,
        "first_audio_gap_ms": total_gap,
    }
```
<!-- END auto:snippet -->

## 2 — Aggregate evals, across bundles

```bash
uv run python docs/teaching/12-evals-and-latency/evals.py \
    docs/teaching/12-evals-and-latency/bundles/ \
    docs/teaching/12-evals-and-latency/ground_truth.csv
```

Three blocks of output:

### Latency percentiles

Sort every bundle's first-audio `turn.gap`, then report P50 and P95. The script
uses the same `LatencyPercentileStats.from_values` helper as EasyCat's latency
validation and debug CLI, so the lesson and the operator tooling agree.
**P95 is the number you report.** Voice users remember the bad turns, not the
good ones; a single stumble poisons an otherwise-fast bot's reputation. Track
P50 so you know the median, but *target* P95.

> With only six bundles, the maintained clamped-exclusive calculation puts P95
> at the slowest observed turn: 2420 ms. That makes the deliberate slow-agent
> fixture visible, but it is still a noisy estimate of real tail latency. Real
> eval sets need dozens of turns for P95 to stabilize; re-run this against a
> directory full of your own chapter-6 or chapter-10 runs for a number you can
> trend.

#### One turn controls this P95

The evaluator also recomputes P95 after omitting each bundle once. Run
the same diagnostic directly:

```bash
uv run python docs/teaching/12-evals-and-latency/p95_sensitivity_probe.py
```

For the six primary fixtures, the full P95 is 2,420 ms. Omitting
`turn_02_slow_agent.bundle` lowers it to 1,160 ms; omitting any other
bundle leaves it at 2,420 ms. The printed leave-one-out range is
therefore 1,160–2,420 ms.

A one-bundle eval set can still report its P50/P95, WER, and barge-in
metrics, but it cannot omit a sample and retain a distribution. In that
case the two leave-one-out rows print `n/a` instead of aborting the rest
of the report.

This is an **influence diagnostic, not a confidence interval**. It
answers “which observed turn controls this statistic?” It does not
estimate unseen traffic, sampling bias, or the probability that a
candidate is faster. Use repeated representative turns, a paired
baseline/candidate design where possible, and an uncertainty method
suited to the decision before gating a small regression.

### WER — word error rate for STT

WER = (substitutions + deletions + insertions) / reference words.
There is **no universal good WER**: acceptable error depends on
language, domain vocabulary, acoustics, and the cost of a wrong word.
Report aggregate edits / reference words plus stratified results for
the conditions you care about; compare against a labeled baseline
rather than borrowing a generic threshold.

> The bundled fixtures are synthetic — the STT "hypothesis" is
> identical to the ground-truth transcript, so WER is trivially
> 0% on all six. The script is wired and ready; point it at a
> bundle you recorded through a real STT in chapter 6 (plus a
> hand-typed reference) and it will produce real edits.

### Barge-in F1

Treat "did the bot correctly interrupt itself?" as a classification
problem per turn:

|                              | real barge-in | no barge-in |
|------------------------------|:-------------:|:-----------:|
| **bot interrupted itself**   | TP            | FP          |
| **bot didn't interrupt**     | FN            | TN          |

- **False positives** (FP) → the bot stops talking when nobody
  asked. Our `turn_03_ghost_interrupt` is the canonical FP.
- **False negatives** (FN) → the user tries to barge in, bot
  plows through.

F1 = 2·P·R / (P+R). Choose a target from the relative cost of false
stops and missed interruptions, then validate it on a representative
labeled set. A single universal threshold hides that product tradeoff.
If the set contains no positive labels or no predicted interruptions,
the corresponding denominator is empty; the evaluator prints `n/a`
instead of inventing a zero-valued precision, recall, or F1 score.

## 3 — LLM as judge

```bash
uv run python docs/teaching/12-evals-and-latency/llm_judge.py \
    docs/teaching/12-evals-and-latency/bundles/turn_01_fast.bundle
```

The judge reads the bundle's transcript (STT + TTS text) and
scores 1-5 on relevance, fluency, and appropriate-length.

**Not a replacement for human eval.** Agreement varies with the judge
model, rubric, prompt, and dataset. Calibrate the exact setup against a
human-labeled sample and re-check that agreement when any of those
inputs change. Until then, use it as triage, not ground truth. A score
of 5 means "the judge could not find something to complain about from
the transcript alone"—it does not mean the turn sounded good. Audio
quality, prosody, and awkward pacing do not appear in text.

JSON mode guarantees an object-shaped response, not valid rubric
scores. The script checks that every score is an integer from 1 through
5 and that `reasoning` is text before reporting the result. Its
caller-owned `AsyncOpenAI` client is also scoped and closed after the
request.

Use the judge to:

- Triage a large eval set cheaply; hand-review the low-scoring
  tail.
- Flag candidate regressions against a pinned judge/rubric baseline,
  then review them. Before turning scores into a hard gate, measure
  repeat variance and agreement with human labels.

## Why one number is never enough

No single score captures voice quality.

- P95 latency catches sluggishness.
- WER catches STT regressions.
- Barge-in F1 catches turn-taking bugs.
- LLM-judge catches response-content regressions.
- Manual spot-checks catch everything the above misses (prosody,
  emotion, audible clipping).

A dashboard shows all five. A "quality score" that rolls them up
hides exactly the regressions you care about.

## Try breaking it

1. Identify the slowest bundle by `turn.gap`. Which stage blew
   its budget? Propose a fix — model swap, prompt cache, warmer
   pool — without implementing it. The point is diagnosis.
2. Add a `filler_appropriate` dimension to the LLM-judge rubric.
   Re-run on the chapter-7 tool-bearing bundles (you'll need to
   copy one over). Does the judge agree with your ears?
3. Write a pytest test that fails if the fixture set's P95 exceeds
   1200 ms. That is the seed of a latency regression suite.

<!-- BEGIN auto:practice-handoff -->
## Practice and self-check

Work through [the chapter exercises](./EXERCISES.md), then try their closing
self-check from memory. If an answer is weak, rerun the hardware-free
checkpoint or revisit the section that owns the gap.
<!-- END auto:practice-handoff -->

## What's next

[Chapter 13 — Swap providers AND transports](../13-swap-providers-and-transports/).
With eval in hand, you can make informed swap decisions. Same
`Session`, four provider × transport combinations, real numbers
behind each choice.
