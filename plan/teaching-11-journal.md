# Chapter 11 — The Journal as Mental Model

> You will not write new pipeline code. You will read three
> `RunBundle`s with planted bugs and find them.

## Prerequisites

- Chapters 0-10. This chapter consolidates rather than introduces.

## Learning objectives

1. Navigate a `RunBundle` with `JournalView`.
2. Correlate events across stages by turn ID and timestamps
   (STT final → agent request → TTS first audio).
3. Diagnose three concrete bug classes by journal evidence alone —
   without running the pipeline.

## What you build

Not new pipeline code — **investigations**. The chapter ships
three pre-recorded bundles with planted bugs in
`docs/teaching/11-journal/bundles/` (these are the one exception
to the gitignored-`runs/` convention — they are intentionally
checked in because the chapter cannot work without them):

- `bug_01_missing_final.bundle` — turn starts, STT never emits a
  final.
- `bug_02_tts_stutter.bundle` — TTS output has audible gaps
  between sentences.
- `bug_03_ghost_interruption.bundle` — bot believes it was
  interrupted but the user never spoke.

Plus a helper: `docs/teaching/11-journal/investigate.py` that
loads and queries bundles.

## Narrative arc

1. **The journal is not logs.** Walk through
   `src/easycat/runtime/` and explain the distinction:
   - Logs are unstructured prose. You grep them.
   - The journal is structured events with causal ordering and
     stable schemas. You query it.
2. **A guided investigation: bug 1.** The journal shows the turn
   entered `PROCESSING` (via a `turn_state_changed` record) but
   no `stage_start` with `stage="agent"` followed. Trace back: STT
   emitted a final but `text=""`. Why? Pre-roll off-by-one caused
   the first frame of speech to be dropped and the STT committed
   before real speech arrived. Reader is walked through the
   evidence step by step — including the query shape
   (`journal.filter_by_stage("agent")` returns the
   `stage_start`/`stage_complete` pairs stages currently emit; the
   debugger derives span durations from those pairs).
3. **Bug 2, semi-guided.** Gaps in TTS output. Hypothesis
   checklist:
   - Sentence splitter? Check — spans look reasonable.
   - Agent stream stalls? Check — tokens arriving steadily.
   - TTS network retries? **Yes** — pair up
     `filter_by_stage("tts")` records
     (`stage_start`/`stage_complete`) and compute their durations;
     some are 3-5× normal. The `ws_reconnect_attempt` /
     `ws_reconnect_success` / `ws_reconnect_failure` records
     confirm.
   Reader follows with lighter prompting.
4. **Bug 3, unguided.** Reader finds it alone using
   `investigate.py`, writes up their evidence trail, and only
   *then* reads the solution.

## Key concepts

- `src/easycat/runtime/ExecutionJournal` structure and ordering
  guarantees
- `src/easycat/runtime/JournalView` query API
- `src/easycat/debug/RunBundle` serialization — what's included
  inline, what's indirected through `ArtifactStore`
- Cross-stage correlation via turn ID and monotonic sequence
  numbers

## Exercises

1. Write a `JournalView` query that finds every turn whose agent
   stage took longer than 1500ms. Use
   `view.filter_by_stage("agent")`, pair up each `stage_start`
   with its matching `stage_complete` on the same `turn_id`, and
   compute the duration from their timestamps (stages currently
   journal `stage_start` / `stage_complete`, not a single
   `stage.agent.execute` record — the debugger derives spans from
   those pairs).
2. Plant your own bug in chapter 9c's code. Dump a bundle. Send it
   to a classmate. Can they find it from the bundle alone?
3. Pick any bundle from chapters 2-10. Propose three hypotheses for
   how a flaky test using that bundle would fail. For each
   hypothesis, name the journal record you'd check.

## Key files for the reader to study

- `src/easycat/runtime/` — journal + view + artifact store
- `src/easycat/debug/` — `RunBundle` serialization and load
- `plan/essential-debug-first-runtime.md` — design rationale
- `plan/workstream-1-journal-foundation.md` — what actually got
  built

Reading plans, not just code, is part of the chapter — plans reveal
intent that code can't.

## Files created

- `docs/teaching/11-journal/investigate.py`
- `docs/teaching/11-journal/README.md`
- Three planted-bug bundles in `docs/teaching/11-journal/bundles/`
- `docs/teaching/11-journal/solutions.md` (spoilers, separated
  by deliberate scroll distance)

## Success criteria

- The reader solves bug 3 unassisted.
- The reader can explain, in a sentence, *why* observability is a
  first-class feature of EasyCat and not an afterthought, citing
  the design rationale in
  `plan/essential-debug-first-runtime.md`.

## Links forward

Chapter 12 is the *operate-it* chapter: now that you can read the
journal, you can measure. Latency budgets, P95 vs P50, WER, MOS,
barge-in F1, and the difference between "my bot is fast" and "my
bot is fast 95% of the time."
