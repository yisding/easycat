# Chapter 8 — Exercises

## 1. Drop the threshold and feel the false-positives

**Task.** Set `SMART_THRESHOLD = 0.3`. Re-run. How often does the
bot now interrupt you mid-sentence?

**Hints**

1. The classifier outputs `P(end-of-turn)` — at threshold 0.3 you
   accept way more "you're done" calls than you should. The
   model's confidence is bimodal on clean utterances but mushy on
   ambiguous ones (lists, trailing intonation), so lowering the
   threshold pulls in the mushy ones.
2. Read the journal: every `smart_turn.classify` record has
   `probability` and `confirmed`. Count how many records have
   `probability > 0.3` but `< 0.5` (the default). Those are the
   new false-positives you bought.
3. The tradeoff is real: tighter threshold = more latency wins
   but more user-interruption. Production tunes this per
   deployment based on barge-in F1 (chapter 12).

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

## Self-check

You should be able to: (a) describe what input smart-turn takes
and what it outputs, (b) explain why the fallback silence
threshold still has to be there even with smart-turn on, and (c)
name two utterance patterns that will reliably misclassify.
