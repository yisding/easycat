# Chapter 12 — Evals + the Latency Budget

> The difference between *building* a voice bot and *operating* one
> is measurement. This chapter produces four concrete numbers:
> P50/P95 latency, WER, barge-in F1, and an LLM-as-judge score.

## Prerequisites

- [Chapter 11](../11-journal/). You need to be comfortable
  opening a bundle and querying records.
- `uv sync --group dev`. The LLM-judge script additionally
  wants `OPENAI_API_KEY`.

## The five pre-recorded bundles

```bash
uv run python docs/teaching/12-evals-and-latency/generate_bundles.py
```

- `turn_01_fast.bundle` — clean, fast turn.
- `turn_02_slow_agent.bundle` — agent is slow; P95 spike.
- `turn_03_ghost_interrupt.bundle` — the bot cancelled itself
  when the user never spoke (ch 11 bug 3 flavour).
- `turn_04_real_interrupt.bundle` — the user legitimately
  interrupted.
- `turn_05_medium.bundle` — middle of the pack.

`ground_truth.csv` ties each bundle to its reference transcript
and whether the interruption (if any) was real.

## 1 — Latency budget, per bundle

Read one bundle's gap against a per-stage budget:

```bash
uv run python docs/teaching/12-evals-and-latency/latency_budget.py \
    docs/teaching/12-evals-and-latency/bundles/turn_02_slow_agent.bundle
```

Expected output:

```
=== turn_02_slow_agent.bundle ===
  agent first token                2100 ms     budget   600 ms    OVER
  tts synth (2 sent.)               610 ms     budget   400 ms    OVER
  total (stt final → done)         2900 ms     budget  1000 ms    OVER
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
| TTS → first audio | 100-400 ms |
| **Total (stt-final → bot-done)** | **<1000 ms** |

The budgets here are defensible starting points; real numbers are
per-deployment.

## 2 — Aggregate evals, across bundles

```bash
uv run python docs/teaching/12-evals-and-latency/evals.py \
    docs/teaching/12-evals-and-latency/bundles/ \
    docs/teaching/12-evals-and-latency/ground_truth.csv
```

Three blocks of output:

### Latency percentiles

Sort every bundle's `turn.gap`, then report P50 and P95. **P95 is
the number you report.** Voice users remember the bad turns, not
the good ones; a single stumble poisons an otherwise-fast bot's
reputation. Track P50 so you know the median, but *target* P95.

> With only five bundles, P95 is approximated by the fourth-
> slowest — noisy. Real eval sets need dozens of turns for P95 to
> be stable; re-run this against a directory full of your own
> chapter-6 or chapter-10 runs for a number you can trend.

### WER — word error rate for STT

WER = (substitutions + deletions + insertions) / reference words.
For casual conversation: **10% is OK, 5% is great, below 5% is
usually the model's ceiling.** Report aggregate across bundles,
not per-bundle, to even out small-sample noise.

> The bundled fixtures are synthetic — the STT "hypothesis" is
> identical to the ground-truth transcript, so WER is trivially
> 0% on all five. The script is wired and ready; point it at a
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

F1 = 2·P·R / (P+R). Target >0.9 on realistic eval sets. Tune NR
/ AEC / VAD threshold from there.

## 3 — LLM as judge

```bash
uv run python docs/teaching/12-evals-and-latency/llm_judge.py \
    docs/teaching/12-evals-and-latency/bundles/turn_01_fast.bundle
```

The judge reads the bundle's transcript (STT + TTS text) and
scores 1-5 on relevance, fluency, and appropriate-length.

**Not a replacement for human eval.** Research (Hamming AI,
Braintrust) places LLM-as-judge agreement with humans around
95% on many rubrics — a fast triage layer, nothing more. A score
of 5 means "the judge could not find something to complain about
from the transcript alone" — it does not mean the turn sounded
good. Audio quality, prosody, and awkward pacing don't show up in
text.

Use the judge to:

- Triage a large eval set cheaply; hand-review the low-scoring
  tail.
- Regression-gate a PR: require no bundle's score to drop more
  than N points vs a baseline.

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
3. Write a pytest test that fails if any bundle's P95 exceeds
   1200 ms. That is the seed of a latency regression suite.

## What's next

[Chapter 13 — Swap providers AND transports](../13-swap-providers-and-transports/).
With eval in hand, you can make informed swap decisions. Same
`Session`, four provider × transport combinations, real numbers
behind each choice.
