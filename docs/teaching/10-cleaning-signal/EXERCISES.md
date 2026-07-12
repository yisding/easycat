# Chapter 10 — Exercises

## 1. Type while you talk

**Task.** Type loudly on your keyboard while saying "hello." Run
each of the four `--nr/--aec` combinations. Where does VAD fire
in each?

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

## 2. Run the chapter-9-style barge-in problem with AEC

**Task.** Run `--aec off` on speakerphone (no headphones). The bot
interrupts itself on chapter 9's `cancel.py` style coordinator
(this chapter's `main.py` is built on that shape). Then enable
AEC. Compare bundles.

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

## 3. NR on but AEC off — what changes?

**Task.** Run the two single-component configurations separately:

```bash
uv run python docs/teaching/10-cleaning-signal/main.py --nr on --aec off
uv run python docs/teaching/10-cleaning-signal/main.py --nr off --aec on
```

Compare audio quality and each bundle's `audio.config` record. In the
first run, which noises remain? In the second, which noises remain?

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

## 4. Run `wrong_order.py` and confirm the journal

**Task.** Run `wrong_order.py --mode nr-after-vad` and read the
journal. Confirm that NR ran *after* VAD had already made its
decision (so NR's output never affected what VAD saw).

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

## Self-check

You should be able to draw the NR → AEC → VAD → STT pipeline from
memory, explain why each stage sits where it does, and predict
which `--nr/--aec` combination is best for each of (a) quiet
office with bluetooth headset, (b) noisy retail kiosk with
speakerphone, (c) phone call (Twilio).
