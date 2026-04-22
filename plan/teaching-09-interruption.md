# Chapter 9 — Interruption / Barge-in

> Three versions of the same feature, each one better — and each
> one teaching something the previous one didn't.

**"Wrong version first"** chapter, in triplicate.

## Prerequisites

- Chapter 8

## Learning objectives

1. Implement cooperative cancellation of an in-flight TTS stream
   using `CancelToken` (not exceptions).
2. Estimate "what the user actually heard" by mapping TTS bytes to
   wall-clock playback duration.
3. Mutate conversation history so the bot's memory reflects what
   came out of the speaker, not what the LLM said it would say.

## What you build

Three scripts in `docs/teaching/09-interruption/`, each a small
diff off the previous:

- `ignore.py` — bot ignores user mid-speech. (Starting point: copy
  of `docs/teaching/08-smart-turn/main.py`.)
- `cancel.py` — bot cuts off immediately on user speech.
- `estimate.py` — bot cuts off AND adjusts its memory to match
  what the user actually heard.

All three can be run on the same recording for an A/B/C comparison.
The chapter README includes the A/B/C transcript drift.

## Narrative arc

1. **Why interruption is the hardest problem in voice UX.** Turn
   taking is easy when turns are disjoint. Turn overlapping is
   where the subtle behavior lives.
2. **Version A (ignore).** Trivial. Awful. Establishes a baseline.
   The bot plows through its full response while you try to stop
   it. Like talking to an answering machine.
3. **Version B (cancel).** Introduce `CancelToken`. Walk through
   `src/easycat/session/_turn_context.py`. Demo: bot stops
   mid-word. Much better. But not solved:
   - What does the bot *think* it said?
   - The conversation history still contains the full response.
   - Next turn: the bot behaves as if the user heard the whole
     thing. Dialogues drift out of coherence.
4. **Version C (estimate).** Walk through
   `src/easycat/session/interruption.py`. Map TTS bytes to audible
   duration; compute the character index where the cut happened;
   rewrite conversation history to reflect that.
5. **The subtle edge cases.** Spend real time on these:
   - Playback buffered by OS sound driver — *bytes sent to the
     driver* ≠ *bytes heard by the user*.
   - Markdown stripping means `len(text)` ≠ synthesis units. See
     `session/tts_helpers.py`.
   - Network-buffered TTS chunks that arrive *after* the cancel
     signal and must be dropped.

## Key concepts

- `CancelToken` — cooperative cancellation, not exceptions
- `src/easycat/session/_turn_context.py` — per-turn state, cancel
  token, playback tracking
- `src/easycat/session/interruption.py` — the byte-to-heard
  estimator
- `src/easycat/session/tts_helpers.py` — text normalization for
  interruption math
- Transcript memory mutation — a load-bearing behavior, not
  cosmetic

## Exercises

1. Run chapter 9c and interrupt the bot after exactly one word.
   Inspect the memory in the bundle — did it record just that one
   word?
2. Break the estimator deliberately: send the bot a response full
   of markdown formatting. Where does the estimate drift, and in
   which direction?
3. Play TTS through speakers far from the mic vs close. Does the
   bot interrupt itself on its own audio? Why or why not?
   (Hint: VAD + NR + echo cancellation. Chapter 10 covers NR
   *and* AEC; this chapter's "ignore" version often self-triggers
   on speakerphone setups precisely because AEC isn't wired in
   yet.)

## Journal highlights

- `interruption.start` events
- `interruption.estimate` records with byte and character offsets,
  plus the assumed heard-text
- History-mutation events with before/after conversation state
- For version A: none of the above — the absence is the lesson

## Files created

- `docs/teaching/09-interruption/ignore.py`
- `docs/teaching/09-interruption/cancel.py`
- `docs/teaching/09-interruption/estimate.py`
- `docs/teaching/09-interruption/README.md`

## Success criteria

- The reader can name three reasons *"bytes sent to TTS"* ≠
  *"audio heard by user."*
- The reader has run the A/B/C scripts end-to-end and can
  articulate the step change each version delivers.

## Links forward

Chapter 10 widens the lens to *cleaning the signal* — noise
reduction *and* echo cancellation, plus why their pipeline order
matters and why "the bot heard itself" is a different problem from
"there's a fan in the room."
