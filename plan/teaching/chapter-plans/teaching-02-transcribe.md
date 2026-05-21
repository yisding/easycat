# Chapter 2 — Transcribe

> **Historical planning note.** The shipped curriculum lives under `docs/teaching/`;
> use this file for original intent and rationale.
>
> Speak, see text. Twice — once batch, once streaming. Feel the
> latency difference.

## Prerequisites

- Chapter 1
- An OpenAI API key (or any provider in `stt/factory.py`)

## Learning objectives

1. Swap a provider through `stt/factory.py` without touching consumer
   code.
2. Distinguish **partial** transcripts (best-guess, may revise) from
   **final** (committed).
3. Explain why streaming STT exists when batch works "fine."
4. Read a minimal `RunBundle` — the reader's first journal.

## What you build

Two scripts in `docs/teaching/02-transcribe/`:

- `batch.py` — record 5 seconds, call
  `easycat.transcribe_file(path)`, print transcript. ~15 lines
  thanks to the `easycat.quick` helper.
- `streaming.py` — stream audio to STT directly via
  `create_stt_provider` (`easycat.quick` doesn't cover streaming
  — intentionally, so the chapter can show what the helper hides),
  print partials as they arrive. ~40 lines.

Both write a `RunBundle` to
`docs/teaching/02-transcribe/runs/*.bundle` on exit
(the `runs/` directory is gitignored).

## Narrative arc

1. **The batch version.** Easy, works. You speak for 5 seconds,
   wait, get text. Feels OK for voicemail transcription. Feels
   terrible for a conversation.
2. **Measure perceived latency.** Walltime ≈ speech duration +
   network roundtrip. Unavoidable with batch.
3. **Now stream.** Each partial appears ~150ms after its audio.
   Print partials with timestamps.
4. **Why partials revise themselves.** "going two" → "go into" →
   "go into town." STT is a guesser under time pressure. Partials
   are bets; finals are commitments.
5. **First journal dump.** Open the bundle with `load_bundle()`.
   Read the STT records. Every partial and final is recorded with
   a timestamp. This is your first look at the substrate chapter
   11 will fully expose.

## Key concepts

- `easycat.transcribe_file` — the batch convenience helper; read
  its source in `src/easycat/quick.py` once to see what it wraps
  (it's ~30 lines, no magic)
- `easycat.create_stt_provider` + `STTProviderConfig` — the
  underlying factory, used in the streaming script
- `src/easycat/stt/factory.py` and its `_PROVIDER_TO_CONFIG`
  registry pattern
- `STTProvider` protocol in `providers.py`
- Partial vs final transcripts
- `ExecutionJournal` — first appearance; 30-second tour only. The
  full tour is chapter 11.
- Not introduced: VAD, turn-taking, TTS, agents — all deferred

## Exercises

1. Say a word the STT consistently gets wrong. Read the partials
   in the bundle — when did it commit to the wrong guess?
2. Run the batch script twice on the same recording. Does it give
   the same transcript? (Usually yes; STT is mostly deterministic
   at temperature 0. Usually.)
3. Force a bad network by adding `await asyncio.sleep(2)` inside
   the streaming consumer. What happens to partials vs finals?
   (Partials keep arriving; finals get backed up.)

## Journal highlights

- `stage.stt.execute` records, one per partial and final.
- `STTPartial` events with monotonically increasing revision
  numbers.
- Exactly one `STTFinal` per utterance.

## Files created

- `docs/teaching/02-transcribe/batch.py`
- `docs/teaching/02-transcribe/streaming.py`
- `docs/teaching/02-transcribe/README.md`

## Sidebar — Partials can flap; never act on them

The fact that partials revise has a hard implication for the
agent layer: **never fire the agent on a partial.** Always wait
for `STTFinal`. A naive agent that prefetches on partials commits
to a guess that may evaporate two partials later, wasting LLM
tokens and producing audio for a sentence the user didn't say.
This is the single most common source of "my voice bot is
weirdly confident about things I didn't say." Chapter 6 reinforces
the rule when it wires the agent in.

## Success criteria

- The reader has personally watched a partial transcript revise
  itself in real time.
- The reader can name two concrete reasons streaming STT exists:
  (a) lower perceived latency for the user, (b) earlier signal for
  downstream stages (turn-end detection, smart-turn priming).
- The reader knows that downstream stages should *not* act on
  partials — only on finals.

## Links forward

Chapter 3 glues STT to TTS with the most obvious possible turn
detector — a fixed silence timeout — and watches it fail.
