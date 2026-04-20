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
- Adds AEC via `easycat.create_echo_canceller()`.
- A replay mode runs three recordings through the pipeline:
  - Noisy mic + no bot speech (`noisy_alone.wav`) — exercises NR.
  - Quiet mic + bot speech bleeding through speaker
    (`speakerphone_loop.wav`) — exercises AEC.
  - Both at once (`hard_mode.wav`) — exercises both.
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
   `transport → AEC → NR → VAD → STT`. If NR runs first, AEC's
   subtraction math is off (NR may have already attenuated parts
   of the reference signal). If VAD runs before either, both miss
   their job. Demonstrate by reordering and showing it breaks.
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
  `transport → AEC → NR → VAD → STT → agent → TTS → transport`
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

- `stage.noise_reducer.execute` records, one per frame
- `stage.echo_canceller.execute` records, one per frame
- VAD activation counts under each combination, presented as a
  before/after table
- Per-backend latency in each cleaning span

## Files created

- `docs/teaching/10-cleaning-signal/main.py`
- `docs/teaching/10-cleaning-signal/README.md`
- `docs/teaching/10-cleaning-signal/recordings/noisy_alone.wav`
- `docs/teaching/10-cleaning-signal/recordings/speakerphone_loop.wav`
- `docs/teaching/10-cleaning-signal/recordings/hard_mode.wav`

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
