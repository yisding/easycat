# Chapter 4 — VAD + Pre-roll Buffer

> Real speech detection. And why the buffer *before* the detection
> matters as much as the detection itself.

## Prerequisites

- Chapter 3 (and its breaker recordings)

## Learning objectives

1. Use a VAD provider (Silero by default via `create_vad()`).
2. Explain **pre-roll**: why a VAD that fires 200ms late chops off
   the start of every utterance — and how the ring buffer fixes it.
3. Build a minimal turn detector from `create_vad()` + a `deque`,
   then read EasyCat's production `TurnManager` and understand why
   its interface (EventBus-driven, FSM-shaped) is more elaborate.

## What you build

`docs/teaching/04-vad-preroll/main.py`:

- Starts from a copy of `docs/teaching/03-parrot-naive/main.py`.
- Replaces the chapter-3 silence timeout with two things:
  1. `create_vad()` consumed directly (no `TurnManager`).
  2. A `collections.deque` pre-roll buffer (~15 frames of 20ms).
- Wraps both in a tiny `MiniTurnDetector` class — ~60 lines, one
  `async def frames(audio_iter)` method that yields
  `("speech_started", chunk) / ("frame", chunk) / ("speech_ended", None)`.
- Replays the chapter-3 breaker recordings (read directly from
  `docs/teaching/03-parrot-naive/breakers/`) through both
  implementations.
- Saves both bundles to `docs/teaching/04-vad-preroll/runs/` and
  prints a side-by-side comparison.

**Why a toy version instead of `TurnManager`?** EasyCat's production
`TurnManager` (`src/easycat/turn_manager.py`) is driven by an
`EventBus` + callback surface so it can interoperate with Session,
interruption controllers, and cancel tokens.  Using it in isolation
would mean wiring subscribers before writing the interesting code.
The teaching goal is to make pre-roll *obvious*, so we build the
minimum that shows it, then point at the real thing.

## Narrative arc

1. **What a VAD actually does.** Classifies a small audio frame
   (10-30ms) as *speech* or *not-speech*. That's it. It is not a
   turn detector; it is the primitive that makes a turn detector
   possible.
2. **The pre-roll problem, demo'd.** Turn VAD on without pre-roll.
   Say "Hello" ten times. Note how "ello" often arrives at STT —
   VAD fires 100-200ms late and the STT missed the "H."
3. **The pre-roll fix.** Maintain a small ring buffer of recent
   frames. When VAD fires, flush the buffer into the STT stream
   before live frames. Feels like magic; it's a `deque(maxlen=15)`.
4. **Build `MiniTurnDetector`.** ~60 lines. Reader writes it
   themselves from `create_vad()`. Three states implicit in the
   code: no-speech, speech, pending-end.
5. **Read the production `TurnManager`.** Now open
   `src/easycat/turn_manager.py` and walk the 5-state FSM:
   - IDLE → USER_SPEAKING → USER_PAUSED → PROCESSING → BOT_SPEAKING
   Each of the extra states exists for a reason the toy version
   can't handle: overlap with bot speech, cancellation, actions.
   *The toy is a teaching artifact; the real one is battle-tested.*
6. **Replay the breakers.** Do the chapter-3 failures still happen?
   Which *new* failures appear? (Some VAD misfires on breath
   noises; some long thinking pauses now correctly *don't* end
   turns.)

## Key concepts

- `src/easycat/vad.py::create_vad()`; auto-backend fallback order
  Silero → FunASR → TEN → Krisp. There is no passthrough fallback:
  if none of these import, `create_vad()` raises. Silero is the
  default and its ONNX model is bundled, but it still requires
  `onnxruntime`, which ships in the `easycat[silero-vad]` /
  `easycat[quickstart]` / `easycat[all]` extras (not in the base
  `dev` group). For this chapter, install one of those extras, or
  the readers on a plain `uv sync --group dev` will hit a
  `RuntimeError` from `create_vad()` before hearing a single frame.
- `src/easycat/turn_manager.py` — the production version, used as
  *read-only reference material* in this chapter
- Pre-roll buffer: deque of recent frames, flushed on VAD-on
- VAD is *still* a threshold — it has its own false triggers on
  mouth clicks, keyboard noise, etc. (Chapter 10 is the real fix.)

## Exercises

1. Disable pre-roll (set `maxlen=0`). Replay your ch-3 breakers.
   How many come through missing the first phoneme?
2. Set VAD sensitivity high, then low. Name the symptom of each
   extreme.
3. Speak quickly vs slowly. Which one fools the VAD more often?
   Why? (Fast: runs words together, harder to split. Slow: more
   within-word silences that look like turn ends.)
4. Read `TurnManager.__init__` and list every parameter it takes
   that your `MiniTurnDetector` doesn't. For each one, name a
   scenario that parameter is defending against.

## Journal highlights

- `vad.active` events with start/end timestamps (you write these)
- Pre-roll flush as its own event
- Compare against chapter 3's bundle: turn-boundary accuracy should
  visibly improve on the breaker set

## Files created

- `docs/teaching/04-vad-preroll/main.py` (~100 lines including
  the `MiniTurnDetector`)
- `docs/teaching/04-vad-preroll/README.md`
- `docs/teaching/04-vad-preroll/comparison.md` (auto-generated from
  both bundles when the reader runs the script)

## Success criteria

- The reader can articulate *why* pre-roll matters without checking
  notes.
- The reader has seen a VAD misfire on a recording they know by
  heart from chapter 3.
- The reader can point to at least two responsibilities `TurnManager`
  handles that `MiniTurnDetector` ducks.

## Links forward

Chapter 5 replaces the parrot behavior with a real LLM — and
reintroduces the latency problem in a new, more painful form.
