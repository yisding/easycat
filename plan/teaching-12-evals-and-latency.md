# Chapter 12 — Evals and the Latency Budget

> Now that you can read the journal, you can measure. The
> difference between *building* a voice bot and *operating* one is
> measurement: P95 latency, WER, MOS, barge-in F1, task completion.

## Prerequisites

- Chapter 11 (you must be comfortable with `JournalView` queries)

## Learning objectives

1. Read **percentiles** (P50/P90/P95/P99) and explain why P95
   matters more than P50 for voice UX.
2. Construct a **latency budget**: a per-stage millisecond
   allocation that sums to a target turn time, and detect which
   stage blew through its budget on a given recording.
3. Apply at least three voice-specific evaluation metrics on a
   bundle: WER (STT), barge-in F1 (interruption correctness),
   end-to-end response latency.
4. Use an **LLM-as-judge** check for conversational quality on a
   small set of recorded turns.

## What you build

`docs/teaching/12-evals-and-latency/`:

- `latency_budget.py` — reads any bundle from chapters 5-11,
  decomposes the STT-final-to-first-TTS-audio gap into per-stage
  spans, and prints a budget-vs-actual table.
- `evals.py` — given a directory of bundles + a small "ground
  truth" CSV (recorded transcripts and expected agent behaviors),
  computes WER, barge-in F1, and E2E latency P50/P95.
- `llm_judge.py` — sends a bundle's transcript to an LLM with a
  rubric and parses the structured score.
- `bundles/` — five pre-recorded turn bundles checked in (mix of
  clean and edge-case turns) so the chapter is runnable without
  the reader having to record their own data.

## Narrative arc

1. **Why P95 not P50.** Plot a histogram of E2E latencies from a
   real session. P50 looks great; P95 has a long tail. Voice users
   *remember the bad turns*, not the good ones — one stumble
   poisons the perception of an otherwise-fast bot. Industry
   convention: report P50 *and* P95, target P95.
2. **Build the budget sheet.** Concrete numbers from production
   voice bots:
   - VAD silence wait: 200-500ms
   - STT final commit: 100-300ms
   - Agent LLM (streaming, first sentence): 300-1000ms
   - TTS first byte: 100-400ms
   - Total target: <1000ms human-perceived
   Walk through the journal of one of the bundled turns; show that
   the spans almost match. Find the spike.
3. **WER for STT.** Word Error Rate = (substitutions + deletions +
   insertions) / reference words. Run it on a few bundles. Note
   that a 10% WER for a casual conversation is good; 5% is great;
   anything below 5% is mostly the ceiling of the model.
4. **Barge-in F1.** Turn-taking quality as a classification
   problem: each user speech onset either was or wasn't
   intentional; the bot either did or didn't interrupt itself.
   F1 over precision (false interrupts) and recall (missed
   interrupts).
5. **LLM-as-judge.** Send a transcript with a rubric ("Did the
   agent answer the user's actual question? 1-5") and a small
   number of examples. Studies (Hamming, Braintrust) show
   ~95% agreement with human raters on most rubrics. *Not* a
   replacement for human eval — a fast triage layer.

## Key concepts

- **Percentiles, not averages.** Means hide tail behavior.
- **Budgets, not timeouts.** Timeouts catch failure; budgets catch
  *drift*.
- **Multiple metrics, not a single score.** No one number captures
  voice quality. WER + latency P95 + LLM-judge + manual spot
  checks.
- **`JournalView.filter_by_stage`** (introduced in chapter 11;
  lives in `src/easycat/runtime/journal.py`) — the query
  primitive for budget reads.
- The peripheral plans `peripheral-eval-and-debugger-ui.md` and
  `peripheral-observability-and-cost.md` codify what this chapter
  hand-rolls.

## Exercises

1. Pick the slowest bundle (highest E2E P95 turn). Identify which
   stage blew its budget. Propose a fix (model swap? prompt cache?
   warmer pool?) — but don't implement it. The point is the
   diagnosis.
2. Modify the LLM judge rubric to also score "filler appropriate"
   (was the filler used when needed, omitted when not?). Run on
   the chapter-7 tool-bearing bundles. How well does the judge
   distinguish?
3. Write a `pytest` test that fails if any bundle in the test set
   has P95 E2E latency > 1.2s. This is the seed of a regression
   suite.

## Journal highlights

- The chapter doesn't introduce new journal record types; it
  *consumes* them across many bundles.
- Per-stage span timings (`stage.stt.execute`, `stage.agent.execute`,
  `stage.tts.execute`) are the primary reads.
- Tags applied during the run (e.g., `was_interrupted`,
  `tool_used`) become filterable axes in the eval scripts.

## Files created

- `docs/teaching/12-evals-and-latency/latency_budget.py`
- `docs/teaching/12-evals-and-latency/evals.py`
- `docs/teaching/12-evals-and-latency/llm_judge.py`
- `docs/teaching/12-evals-and-latency/bundles/` (five checked-in
  bundles)
- `docs/teaching/12-evals-and-latency/ground_truth.csv`
- `docs/teaching/12-evals-and-latency/README.md`

## Success criteria

- The reader has produced a P50/P95 latency table from the bundled
  data and identified the worst-stage offender.
- The reader has computed WER on at least one bundle with a
  matching ground-truth transcript.
- The reader can articulate why a single "quality score" is the
  wrong abstraction for voice eval.

## Links forward

Chapter 13 is the finale: with eval in hand, you can make
informed swap decisions. Same `Session`, four provider/transport
combinations, real numbers behind the choice.
