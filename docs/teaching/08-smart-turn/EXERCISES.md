# Chapter 8 — Exercises

[← Back to chapter](./README.md) · [Ladder index](../)

## 1. Separate threshold changes from classification errors

**Task.** Record several ambiguous pauses once with `--backend smart`,
then re-score the same classifier outputs without another provider run:

```bash
uv run python docs/teaching/08-smart-turn/threshold_sweep.py PATH \
  --baseline 0.5 --candidate 0.3
```

Replace `PATH` with the emitted bundle. The report identifies
`newly_accepted` records. Create a JSON label file mapping each
`smart_turn.classify` sequence to `true` when you were actually done at
that pause and `false` when you intended to continue, then rerun with
`--labels labels.json`. How do the baseline and candidate confusion
counts differ? The file must label every classification sequence exactly;
the sweep rejects missing and unknown keys instead of emitting partial metrics.

**Hints**

1. The classifier outputs `P(end-of-turn)`. Scores from 0.3 through
   just below 0.5 are newly accepted by the candidate threshold, but
   that fact alone says nothing about whether each decision is correct.
2. A newly accepted decision is a false positive only when its
   `user_was_done` label is `false`. Without labels the report leaves
   `metrics` as `null` rather than inventing an error rate.
3. Re-scoring one bundle holds the audio and model probabilities fixed,
   isolating the threshold policy. Re-recording after editing
   `SMART_THRESHOLD` is still useful for experiencing the UX, but it is
   not a controlled metric comparison.
4. Lower thresholds usually trade fewer false negatives and earlier
   commits for more false positives and user interruption. Tune on a
   representative labeled set, not on probability counts alone.

## 2. Find a real misfire and keep it

**Task.** Record an utterance where the `vad` backend gets it
right and `smart` gets it wrong. Save both bundles. (You will
need this in chapter 12 when you build an eval set.)

**Hints**

1. The easiest misfire to provoke: a list with level intonation
   ("apples, bananas, pears"). Smart-turn may say "done" after
   "bananas" because pitch was flat at that word.
2. Another one: trailing "and?" with rising intonation. Smart-turn
   should *not* fire (pitch up = continuation), but may
   misclassify on noisy mics.
3. Save the bundle by copying it out of `runs/` before you re-run
   (the `runs/` directory is gitignored but the file persists
   until you re-run with the same session id).
4. A single real misfire is a tiny eval set of 1. Chapter 12
   teaches you to grow this into dozens.

## 3. Predict the cost of the "I was thinking..." case

**Task.** Before running, predict whether smart-turn will hit or
miss on the utterance *"I was thinking… we should order pizza."*
Then run `--backend smart` and check.

**Hints**

1. The "…" pause is ~500 ms of soft silence. VAD will fire
   `VADStopSpeaking` during it.
2. Smart-turn then sees the audio up to "thinking" and is asked
   "is this end-of-turn?" Pitch at "thinking" is mid-falling but
   not definitively final. Probability is likely in the 0.4-0.6
   range — coin-flippy.
3. If the model says "not done" → pending state, no commit until
   either the user resumes (chapter 8's "we should order pizza"
   continues the same turn) or the fallback silence fires.
4. If the model says "done" → bot interrupts the user. Bad.
5. This is exactly why the *fallback* silence timeout exists.
   Smart-turn is a speedup over the worst case, not a replacement
   for the safety net.

## 4. Account for the whole fallback wait

**Task.** Run the provider-free endpoint path probe:

```bash
uv run python docs/teaching/08-smart-turn/endpoint_wait_probe.py
```

Before reading its JSON, calculate the total for each path. Then change
the scripted classifier cost from 40 ms to 120 ms and predict which
fields and totals move.

**Hints**

1. Baseline VAD has no classifier or pending phase: its configured
   800 ms silence wait is its whole endpoint wait.
2. Smart accept is early silence plus classifier inference:
   `200 + 40 = 240 ms` in the original probe.
3. Smart fallback adds all three components:
   `200 + 40 + 800 = 1,040 ms`. `SMART_FALLBACK_MS` starts after
   classification; it is not a cap on the total endpoint wait.
4. Raising only inference cost should move
   `classification_inference_ms` and `endpoint_wait_ms`. It must not
   change `silence_wait_ms` or `pending_wait_ms`.
5. In a live run, compare the recorded components. Do not infer the
   user-visible delay from a configured timeout alone.

## Self-check

You should be able to: (a) describe what input smart-turn takes
and what it outputs, (b) explain why the fallback silence
threshold still has to be there even with smart-turn on, and (c)
name two utterance patterns that can challenge the classifier, and
(d) explain why STT-final → first-audio cannot measure an endpoint
detector's latency win, and (e) decompose a smart fallback's endpoint
wait into early silence, inference, and pending time.
