# Chapter 10 — Cleaning the Signal

> Two different problems often confused as one. **Noise reduction**
> removes uncorrelated background sound (fan, keyboard, baby).
> **Acoustic echo cancellation** removes the bot's own voice
> coming back through the user's microphone. They live in the same
> pipeline slot but use fundamentally different techniques.

## Prerequisites

- Chapter 9 (Interruption / Barge-in)

## Learning objectives

1. Distinguish noise reduction (NR) from acoustic echo cancellation
   (AEC) in concept, in code path, and in failure mode.
2. Add NR to the pipeline using the Krisp → RNNoise → passthrough
   fallback chain.
3. Add AEC using the LiveKit APM wrapper (or the passthrough
   fallback) and explain the **reference signal** that AEC
   needs but NR doesn't.
4. Recognize **half-duplex vs full-duplex** as a real constraint
   imposed by hardware (laptop speakerphone) or transport
   (cellular networks) and the techniques used to fake full-duplex
   when you can't have it.

## What you build

`docs/teaching/10-cleaning-signal/main.py`:

- Starts from a copy of `docs/teaching/09-interruption/estimate.py`.
- Adds NR via `easycat.create_noise_reducer()`.
- Adds AEC via
  `easycat.create_echo_canceller(EchoCancellationConfig(enabled=True))`.
  The default `EchoCancellationConfig(enabled=False)` returns a
  `PassthroughAEC` — call this out in the chapter so the reader
  doesn't accidentally run a no-op AEC and wonder why nothing
  changes on `speakerphone_loop.wav`.
- **Install the extras.** Both factories silently return a
  passthrough when their optional deps are missing:
  `create_noise_reducer()` falls back Krisp → RNNoise → passthrough,
  and `create_echo_canceller()` falls back LiveKit APM → passthrough.
  On a plain `uv sync --group dev` both land on passthrough, so
  the before/after recordings come out identical and the
  chapter's main experiment fails silently. Install
  `easycat[rnnoise]` (open-source RNNoise, permissive license) or
  Krisp per its own SDK instructions for NR, and `easycat[aec]`
  (LiveKit APM) for AEC before running the demos. The chapter
  should verify the active backend by reading the journal's
  `audio` stage `noise_reducer` / `echo_canceller` fields (not by
  trusting config).
- A replay mode runs three recordings through the pipeline.
  Crucially, **AEC is dual-input**: the live pipeline feeds the
  clean outbound (TTS) audio into `echo_canceller.feed_reference()`
  from `Session._drain_outbound_audio()`. If replay only plays a
  single recorded mic track back in, the canceller never sees the
  reference it needs and AEC-on / AEC-off runs come out identical.
  So every AEC-exercising recording is stored as a **pair**: the
  mic capture *and* the far-end reference that was playing at the
  same time (synchronised sample counts), with the replay harness
  pushing the reference into `feed_reference()` in lockstep.
  - Noisy mic + no bot speech (`noisy_alone.wav`, single track)
    — exercises NR. No reference needed (no bot audio played).
  - Quiet mic + bot speech bleeding through the speaker
    (`speakerphone_loop.mic.wav` + `speakerphone_loop.ref.wav`)
    — exercises AEC.
  - Both at once (`hard_mode.mic.wav` + `hard_mode.ref.wav`)
    — exercises both.
- Auto-generated comparison table from the dumped bundles.

## Narrative arc

1. **Replay `noisy_alone.wav` through the chapter-4 VAD-only
   pipeline.** Count the VAD false triggers in the journal.
2. **Add NR. Re-run.** False triggers drop dramatically. The
   VAD didn't change — the signal it sees did. NR is *single-input*:
   it filters the mic stream against a model of stationary noise.
3. **Now replay `speakerphone_loop.wav` with NR enabled but no
   AEC.** The bot's own TTS, leaking from the laptop speaker into
   the laptop mic, fires the VAD as if the user were speaking.
   The "ignore" version of chapter 9's interruption code keeps
   talking — fine. The "cancel" version cuts itself off
   constantly. NR doesn't help: the bot's voice is *signal*, not
   noise, from NR's point of view.
4. **Add AEC. Re-run `speakerphone_loop.wav`.** AEC is
   *dual-input*: it takes the mic stream *and* the reference
   signal (what was sent to the speaker). It subtracts a filtered
   version of the reference from the mic. The bot stops hearing
   itself. Walk through `easycat.echo_cancellation` —
   `EchoCanceller` protocol, `LiveKitAEC`, `PassthroughAEC`.
5. **Pipeline order matters.**
   Production order is `transport → NR → AEC → VAD → STT`. Two
   things to notice:
   - NR runs before AEC. AEC's adaptive filter still converges
     because it sees the *unprocessed* reference signal on one
     side and the NR-processed mic on the other — it learns the
     combined (echo-path ∘ NR) mapping and subtracts accordingly.
     Swapping to AEC → NR is also a defensible design; the
     framework picks NR-first so NR sees the rawest possible
     noise spectrum.
   - VAD must run *after* both. If VAD ran before NR it would
     false-trigger on stationary noise; if it ran before AEC it
     would false-trigger on the bot's own voice. Demonstrate each
     reorder and show what breaks.
6. **Half-duplex vs full-duplex.** Phones (especially cellular)
   and speakerphones often operate half-duplex by hardware: only
   one direction transmits audio at a time. AEC is what makes a
   speakerphone *feel* full-duplex even when it isn't. Headsets
   sidestep the problem entirely (no speaker → mic path). Phone
   networks: the carrier may apply its own AEC; cooperate or
   conflict.
7. **Tradeoffs.** NR adds 10-50ms of latency; AEC adds 5-20ms.
   Aggressive NR clips quiet speech. Aggressive AEC clips
   double-talk (when both bot and user speak at once — which is
   exactly the barge-in case from chapter 9). Tuning is a
   per-deployment decision.

## Key concepts

- `easycat.providers.NoiseReducer` and
  `easycat.providers.EchoCanceller` protocols
- `src/easycat/noise_reduction.py::create_noise_reducer()` —
  fallback Krisp → RNNoise → passthrough
- `src/easycat/echo_cancellation.py::create_echo_canceller()` —
  fallback LiveKit APM → passthrough
- The **reference signal**: what AEC needs that NR doesn't, and
  why session wires it from the TTS output side
- Pipeline order:
  `transport → NR → AEC → VAD → STT → agent → TTS → transport`
  (matches the production flow documented in
  `src/easycat/session/_session.py::_run_pipeline` and the
  `AudioStage` chain in `src/easycat/stages/audio.py`)
- Half-duplex vs full-duplex; speakerphone vs headset
- **Double-talk** as the AEC failure mode that maps onto chapter
  9's barge-in scenario — these are the same physical problem
  from two angles

## Exercises

1. Record yourself typing loudly while saying "hello." Run the
   three modes (no NR, NR only, NR + AEC). Where does the VAD
   fire in each?
2. Disconnect AEC's reference signal (pass it the wrong audio,
   like silence). What changes? Why is the wrong-reference case
   *worse* than no AEC at all?
3. Set NR to passthrough. Confirm the pipeline still works — all
   fallbacks are real and load-bearing, not placeholders. Repeat
   for AEC.
4. Run `hard_mode.wav` (both noise and bot bleed) with various
   combinations. Which combination is best? Is "both on, both
   aggressive" ever the right answer?

## Journal highlights

- `audio` stage records, one `stage_start`/`stage_complete` pair
  per frame (NR + AEC run inside a single `AudioStage`; the
  snapshot's `noise_reducer` and `echo_canceller` fields on each
  record name the live backends)
- VAD activation counts under each combination, presented as a
  before/after table
- Per-backend latency derived from the `audio` stage spans

## Files created

- `docs/teaching/10-cleaning-signal/main.py`
- `docs/teaching/10-cleaning-signal/README.md`
- `docs/teaching/10-cleaning-signal/recordings/noisy_alone.wav`
- `docs/teaching/10-cleaning-signal/recordings/speakerphone_loop.mic.wav`
- `docs/teaching/10-cleaning-signal/recordings/speakerphone_loop.ref.wav`
- `docs/teaching/10-cleaning-signal/recordings/hard_mode.mic.wav`
- `docs/teaching/10-cleaning-signal/recordings/hard_mode.ref.wav`

## Success criteria

- The reader can articulate, in their own words, why NR doesn't
  fix bot self-hearing and AEC doesn't fix fan noise.
- The reader has measurably reduced VAD false triggers on the
  noisy sample (NR) and stopped self-interruption on the
  speakerphone sample (AEC).
- The reader can name the pipeline order and defend why each
  reordering breaks something.

## Links forward

Chapter 11 steps off the building ladder and onto the debugging
ladder: mastering the journal you've been dumping all along.
