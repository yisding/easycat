# Chapter 8 — Smart-turn

> A tiny ML model that knows you're done talking before the silence
> confirms it.

## Prerequisites

- Chapter 7 (the starting-point code copies from there; the
  tool-call wiring carries forward unused for now)
- The smart-turn ONNX model (downloadable via the existing
  `smart_turn.py` setup path)

## Learning objectives

1. Explain the difference between VAD ("is this frame speech?") and
   endpoint detection ("was that the end of a turn?").
2. Run an ONNX model inline in an async pipeline without blocking
   the event loop.
3. Recognize the prosodic cues that make smart-turn work: falling
   pitch, completed syntax, declarative intonation.

## What you build

`docs/teaching/08-smart-turn/main.py`:

- Starts from a copy of `docs/teaching/07-tools/main.py` (the
  tool-call wiring carries forward unused; future chapters will
  exercise it again).
- Wires `smart_turn.py` into the turn-detection path.
- A sibling script `replay_and_compare.py` runs a saved recording
  through both chapter 7's VAD-silence pipeline (the
  timeout-based turn detector inherited from chapter 4) and this
  chapter's smart-turn pipeline and prints a timing table.

## Narrative arc

1. **VAD silence is a timeout in disguise.** You wait 800ms of
   silence to be "sure" the user is done. Most of that 800ms is
   slack — the user was done 300-500ms ago.
2. **The human equivalent.** We don't cue off silence, we cue off
   intonation. "I think we should go" falls at "go"; "I think we
   should" doesn't. You can literally hear the difference in the
   recordings.
3. **Smart-turn classifies.** Input: recent audio + transcript.
   Output: P(end-of-turn). When confident, fire the turn end
   immediately — skip most of the silence wait.
4. **Integration — in the toy detector first.** Extend the
   `MiniTurnDetector` you built in chapter 4 to call smart-turn
   whenever it sees a short pause *inside* a speech segment. If
   smart-turn says "end-of-turn", emit `speech_ended` immediately
   instead of waiting out the full silence timeout. ~20 new lines.
   Then, as reference reading, open
   `src/easycat/turn_manager.py` and find where the production
   FSM fires smart-turn — the same idea, wired through the
   5-state machine so it cooperates with barge-in (chapter 9) and
   cancel tokens.
5. **Async inference.** `run_in_executor` so ONNX doesn't block
   the event loop. Measure the inference time — should be <50ms
   per call on modern hardware.
6. **Replay comparison.** Same audio, two bundles, two first-audio
   latencies. Expect ~300-500ms savings on clean, declarative
   utterances.

## Key concepts

- `src/easycat/smart_turn.py` and the ONNX inference call
- Async-friendly inference: `run_in_executor` so the model doesn't
  block the event loop
- Confidence thresholds and false positives (bot interrupts you
  mid-thought because smart-turn guessed wrong)

## Exercises

1. Find a sentence with trailing rising intonation ("...and?").
   Does smart-turn wait, or does it misfire?
2. Set the confidence threshold artificially low (e.g., 0.3). What
   is the new failure mode? How often does the bot interrupt you?
3. Record yourself listing items: "apples, bananas, pears." Where
   does smart-turn think the turn ends? (Usually after "pears" if
   the intonation falls; sometimes after "bananas" if it doesn't.)

## Journal highlights

- `smart_turn.prediction` events with confidence scores and the
  audio window used
- Gap from last speech frame to `speech_ended` event (should be
  <300ms vs ~800ms from chapter 7)
- Side-by-side: chapter 7 bundle's VAD-timeout vs this chapter's
  smart-turn commits on the same recording

## Files created

- `docs/teaching/08-smart-turn/main.py`
- `docs/teaching/08-smart-turn/replay_and_compare.py`
- `docs/teaching/08-smart-turn/README.md`

## Success criteria

- The reader has halved (or better) end-of-turn latency vs chapter
  7 on a real recording, measured by journal.
- The reader has seen smart-turn misfire at least once and can
  describe *why* in terms of the input signal.

## Links forward

Chapter 9 is the hardest chapter: what if the user *starts talking
while the bot is talking*?
