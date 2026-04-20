# Chapter 7 — Smart-turn

> A tiny ML model that knows you're done talking before the silence
> confirms it.

## Prerequisites

- Chapter 6
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

`docs/teaching/07-smart-turn/main.py`:

- Starts from a copy of `docs/teaching/06-streaming-agent/main.py`.
- Wires `smart_turn.py` into the turn-detection path.
- A sibling script `replay_and_compare.py` runs a saved recording
  through both chapter 6 and chapter 7 pipelines and prints a
  timing table.

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
4. **Integration.** Where in the `turn_manager` FSM does the
   smart-turn call fire? Walk through `src/easycat/smart_turn.py`.
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
- Gap from last speech frame to turn-committed event (should be
  <300ms vs ~800ms from chapter 6)
- Side-by-side: chapter 6 bundle's VAD-timeout vs chapter 7
  bundle's smart-turn commits on the same recording

## Files created

- `docs/teaching/07-smart-turn/main.py`
- `docs/teaching/07-smart-turn/replay_and_compare.py`
- `docs/teaching/07-smart-turn/README.md`

## Success criteria

- The reader has halved (or better) end-of-turn latency vs chapter
  6 on a real recording, measured by journal.
- The reader has seen smart-turn misfire at least once and can
  describe *why* in terms of the input signal.

## Links forward

Chapter 8 is the hardest chapter: what if the user *starts talking
while the bot is talking*?
