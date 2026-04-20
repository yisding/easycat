# Chapter 9 — Noise Reduction

> Not the showiest chapter. But it explains why NR lives *before*
> VAD in the pipeline and not after.

## Prerequisites

- Chapter 8

## Learning objectives

1. Add a `NoiseReducer` to the pipeline using the Krisp → RNNoise
   → passthrough fallback chain.
2. Explain why NR improves VAD accuracy, not just "how it sounds."
3. Recognize NR tradeoffs: added latency and the risk of eating
   quiet speech.

## What you build

`docs/teaching/09-noise-reduction/main.py`:

- Starts from a copy of `docs/teaching/08-interruption/estimate.py`.
- Adds NR via `create_noise_reducer()`.
- Includes a replay mode that runs noisy recordings (keyboard
  clicks, fan hum, crying baby, TV in background) through the
  pipeline both with and without NR.
- Auto-generated comparison table printed from the dumped bundle.

## Narrative arc

1. **Replay a noisy recording through chapter 4's VAD-only
   pipeline.** Count the VAD false triggers by reading the bundle.
2. **Add NR.** Same recording. Re-run.
3. **Count again.** False triggers drop dramatically. The VAD
   didn't change — NR did.
4. **Pipeline order matters.** If NR lived *after* VAD, the VAD
   would still false-trigger on keyboard clicks; the bot would
   still process silence-as-speech. Demonstrate this by briefly
   reordering in the teaching script; show it breaks.
5. **Tradeoffs.** NR adds 10-50ms of latency per frame. Aggressive
   settings can clip quiet speech entirely (the reader should hear
   this too, not just be warned).

## Key concepts

- `NoiseReducer` protocol in `src/easycat/providers.py`
- `src/easycat/noise_reduction.py::create_noise_reducer()`
  fallback chain: Krisp → RNNoise → passthrough
- Why the pipeline order is `transport → NR → VAD → STT`, and not
  any other order (VAD accuracy depends on cleaned audio; STT
  accuracy benefits too but gains less)

## Exercises

1. Record yourself typing loudly while saying "hello." Run with
   and without NR. Where does the VAD fire in each case? Which NR
   backend is most effective?
2. Swap backends via config. Is Krisp audibly better than RNNoise
   on a quiet (clean) recording? Often the answer is "not
   obviously" — which is a useful data point.
3. Set NR to passthrough. Confirm the pipeline still works — all
   fallbacks are real and load-bearing, not placeholders.

## Journal highlights

- `stage.noise_reducer.execute` records, one per frame
- VAD activation counts with and without NR, presented as a
  before/after table
- Per-backend latency in the NR span

## Files created

- `docs/teaching/09-noise-reduction/main.py`
- `docs/teaching/09-noise-reduction/README.md`
- `docs/teaching/09-noise-reduction/noisy_samples/` — a small set
  of canonical noisy recordings (keyboard, fan, TV, crowd);
  checked in so every reader sees the same inputs

## Success criteria

- The reader has measurably reduced VAD false triggers on a noisy
  sample (usually 5-10× reduction).
- The reader can state, from memory, why NR goes before VAD and
  not after.

## Links forward

Chapter 10 steps off the ladder of *building* and onto the ladder
of *debugging*: mastering the journal you've been dumping all
along.
