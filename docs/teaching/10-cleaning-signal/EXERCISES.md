# Chapter 10 — Exercises

<!-- BEGIN auto:navigation -->
[← Chapter narrative](./README.md) · [Teaching ladder](../) · [Chapter 11 — The Journal as Mental Model →](../11-journal/)
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

## 1. Type while you talk

**Task.** Type loudly on your keyboard while saying "hello." Run
each of the four `--nr/--aec` combinations. Where does VAD fire
in each?

<!-- BEGIN auto:exercise-hints -->
<details markdown="1">
<summary>Reveal hints after your first attempt</summary>

**Hints**

1. Keyboard clicks are short, energetic, broadband — they look
   like consonants to a VAD that just measures energy.
2. With NR off, the journal shows VAD-on events that line up with
   click timestamps, not with your voice.
3. With NR on (RNNoise or Krisp), keystrokes should drop below
   the speech threshold. NR is good at "stationary or
   short-burst non-speech."
4. AEC doesn't help here — it cancels *the bot's voice*, not
   keystrokes. If your bundle shows AEC on + NR off with VAD
   still firing on clicks, that's the experiment landing.
5. The point: NR and AEC attack different problems. The chapter
   names them clearly because production teams routinely treat
   them as one thing.

</details>
<!-- END auto:exercise-hints -->

## 2. Run the chapter-9-style barge-in problem with AEC

**Task.** Run `--aec off` on speakerphone (no headphones). The bot
interrupts itself on chapter 9's `cancel.py` style coordinator
(this chapter's `main.py` is built on that shape). Then enable
AEC. Compare bundles.

<!-- BEGIN auto:exercise-hints -->
<details markdown="1">
<summary>Reveal hints after your first attempt</summary>

**Hints**

1. With AEC off: every TTS sentence triggers a VAD-on, then an
   interruption event. The bundle shows `interruption.start`
   records timed with `stage.tts.execute` records — perfect
   correlation = bot hearing itself.
2. With AEC on (LiveKit APM): the reference path subtracts the
   echo, VAD sees clean mic, no false interrupts.
3. If you have an aggressive filter setting, you may *also* clip
   the user's actual barge-in (the "double-talk" failure mode
   described in the README). Tune carefully.

</details>
<!-- END auto:exercise-hints -->

## 3. NR on but AEC off — what changes?

**Task.** Run the two single-component configurations separately:

```bash
uv run python docs/teaching/10-cleaning-signal/main.py --nr on --aec off
uv run python docs/teaching/10-cleaning-signal/main.py --nr off --aec on
```

Compare audio quality and each bundle's `audio.config` record. In the
first run, which noises remain? In the second, which noises remain?

<!-- BEGIN auto:exercise-hints -->
<details markdown="1">
<summary>Reveal hints after your first attempt</summary>

**Hints**

1. NR cleans the *mic side* — fan, keyboard, fridge hum drop out.
   But the bot's own voice is still in the mic, looped through
   the speaker.
2. AEC alone (without NR): subtracts the bot's voice but leaves
   the fan in. Useful if your environment is quiet and the
   speaker is the only problem.
3. `--aec off` installs `_Passthrough`; no AEC filter runs, even
   though the TTS drain safely calls its no-op `feed_reference()`.
   To study a real AEC filter with a dead reference path, use
   `wrong_order.py --mode aec-no-reference` in exercise 4.
4. The order also matters: NR-first lets NR see raw noise and
   model it cleanly; AEC then handles whatever NR couldn't
   classify as noise (the bot's voice has speech *structure*, so
   NR leaves it alone). This is why the pipeline is NR → AEC, not
   the other way.

</details>
<!-- END auto:exercise-hints -->

## 4. Run `wrong_order.py` and confirm the journal

**Task.** Run `wrong_order.py --mode nr-after-vad` and read the
journal. Confirm that NR ran *after* VAD had already made its
decision (so NR's output never affected what VAD saw).

<!-- BEGIN auto:exercise-hints -->
<details markdown="1">
<summary>Reveal hints after your first attempt</summary>

**Hints**

1. The journal records `vad.processed_raw` followed by
   `nr.applied_after_vad` with the same `frame_index`. NR is still
   running, but only after VAD consumed the raw frame, so its output
   cannot influence that verdict.
2. The `audio.config` record names the live backend, so you can
   confirm RNNoise is loaded. `vad.processed_raw.data.events` names
   any VAD transitions produced from each raw frame. The paired record
   order proves NR cannot affect those transitions; a matched baseline
   run is still required before claiming the false-fire rate is
   unchanged.
3. Try `--mode aec-no-reference`: AEC's `feed_reference()` counter
   stays at zero, and the bundle records `aec.no_reference`. The
   subtraction has nothing to subtract from, so it's a pure
   passthrough.
4. This is the "wrong-version-first" for pipeline ordering —
   right components, wrong wiring, indistinguishable from "no
   feature" except in the journal.

</details>
<!-- END auto:exercise-hints -->

## 5. Make replay evidence fail closed

**Task.** Run the provider-free replay metrics probe:

```bash
uv run python docs/teaching/10-cleaning-signal/replay_metrics_probe.py
```

Explain why both rejected cases must fail before constructing the AEC
backend. Then change one scripted scale filter from `0.5` to `1.0` and
predict the per-frame and aggregate RMS values.

<!-- BEGIN auto:exercise-hints -->
<details markdown="1">
<summary>Reveal hints after your first attempt</summary>

**Hints**

1. `--aec on` without `--ref` is not a weaker AEC experiment; it is a
   dead reference path and belongs in the explicit wrong version.
2. A short reference silently stops feeding partway through the mic
   stream unless frame counts are checked up front. The corrected replay
   rejects that alignment error.
3. Two `0.5` scale stages turn RMS 1000 into 250, a change of about
   -12.041 dB. Replacing one with `1.0` should leave RMS 500 and about
   -6.021 dB.
4. Verify the per-frame `replay.frame` records and aggregate
   `reference_frames_fed`; a loaded backend name alone does not prove
   correct dataflow.
5. RMS is not a quality score. A filter that deletes the user can show a
   dramatic energy reduction while making the voice path unusable.

</details>
<!-- END auto:exercise-hints -->

## Self-check

<!-- BEGIN auto:self-check-protocol -->
> **Closed-book retrieval gate**
>
> 1. Close the chapter narrative and every hint disclosure.
> 2. Answer each outcome below from memory, aloud or in writing.
> 3. Support the answer with at least one observed field, measurement, or behavior
>    from your attempt record.
>
> If an answer needs notes, reopen only the section that owns the weak concept,
> correct your explanation, close it, and retry. Continue only when you can answer
> without looking.
<!-- END auto:self-check-protocol -->

You should be able to draw the NR → AEC → VAD → STT pipeline from
memory, explain why each stage sits where it does, and predict
which `--nr/--aec` combination is best for each of (a) quiet
office with bluetooth headset, (b) noisy retail kiosk with
speakerphone, (c) phone call (Twilio). You should also be able to
distinguish backend availability, reference alignment, signal-energy
change, and actual speech quality.

<!-- BEGIN auto:exercise-completion -->
---
Self-check complete? Prepare the cumulative spine, then replay it through this chapter:

```bash
uv sync --extra quickstart --group dev
uv run python docs/teaching/offline_spine.py --run --through 10 --jobs 4 --show-evidence
```

- [Review the chapter narrative](./README.md)
- [Continue to Chapter 11 — The Journal as Mental Model →](../11-journal/)
<!-- END auto:exercise-completion -->
