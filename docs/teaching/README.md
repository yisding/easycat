# Teaching: Voice Pipelines from Scratch

A progressive 14-chapter ladder for learning voice-AI pipelines
through EasyCat. Modeled after *Crafting Interpreters*, *Ray
Tracer in One Weekend*, and the `nanoGPT` tradition.

Each chapter is a **self-contained folder** under `docs/teaching/`
with a narrative `README.md` and a runnable `main.py` (or a couple
of scripts). Chapter `N+1` copies chapter `N`'s code as its starting
point and evolves from there — so every chapter folder is a frozen,
runnable artifact you can visit independently.

> **Start here:** [`00-hello-audio/`](./00-hello-audio/).

## The ladder

### Build — assemble the pipeline

| # | Folder | What you add |
|---|---|---|
| 0 | [`00-hello-audio`](./00-hello-audio/) | Record and play raw PCM. No framework. |
| 1 | [`01-echo`](./01-echo/) | Mic → speaker through the `Transport` protocol. |
| 2 | [`02-transcribe`](./02-transcribe/) | Speak, see text. Batch vs streaming STT. First journal. |
| 3 | [`03-parrot-naive`](./03-parrot-naive/) | Turn-taking by silence timeout. Deliberately broken. |
| 4 | [`04-vad-preroll`](./04-vad-preroll/) | Real speech detection + a pre-roll ring buffer. |
| 5 | [`05-blocking-agent`](./05-blocking-agent/) | An LLM in the loop. Feels terrible. On purpose. |
| 6 | [`06-streaming-agent`](./06-streaming-agent/) | Sentence-level TTS overlap cuts first-audio latency. |
| 7 | [`07-tools`](./07-tools/) | Tool calls, fillers, session actions. |
| 8 | [`08-smart-turn`](./08-smart-turn/) | Endpoint classification — start earlier, not sooner. |
| 9 | [`09-interruption`](./09-interruption/) | Barge-in, cancel, heard-estimation. |

### Operate — the demo-to-production gap

| # | Folder | What you add |
|---|---|---|
| 10 | [`10-cleaning-signal`](./10-cleaning-signal/) | Noise reduction, AEC, half-duplex. |
| 11 | [`11-journal`](./11-journal/) | The journal as mental model. Pre-recorded bundles. |
| 12 | [`12-evals-and-latency`](./12-evals-and-latency/) | Percentiles, WER, barge-in F1, LLM-as-judge. |

### Generalise — the Protocol payoff

| # | Folder | What you add |
|---|---|---|
| 13 | [`13-swap-providers-and-transports`](./13-swap-providers-and-transports/) | Swap providers *and* transports; measure the tradeoffs. |

## Prerequisites

- Python 3.11+.
- `uv sync --extra quickstart --group dev` from the repo root.
  The `quickstart` extra bundles mic I/O, OpenAI, NumPy, and
  ONNX Runtime — enough for chapters 0-9 and 11-12. Chapter 10
  additionally wants the `rnnoise` and/or `aec` extras for real
  noise reduction / echo cancellation (falls back silently to
  passthrough without them). Chapter 13 pulls in Deepgram /
  ElevenLabs on top.
- A mic and speakers for the build chapters. Chapters 11 and 12
  ship checked-in bundles you can read without hardware.
- API keys, set as environment variables:
  - `OPENAI_API_KEY` — default STT / TTS / agent provider.
  - `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY` — used starting
    chapter 13 to demonstrate provider swap.

Each chapter's README lists its own prerequisites up front.

## Conventions

- **Copy, don't modify.** Chapter `N+1` copies chapter `N` as its
  starting point rather than editing in place. A little
  duplication is the intended cost; each folder stays readable on
  its own.
- **Each README gets one diagram and one exercise.** If a chapter
  is longer than one page, it's too long.
- **Journals are the single source of truth.** From chapter 2
  onward each runnable chapter dumps a `RunBundle` to
  `runs/*.bundle` in its own folder. The `runs/` directory is
  gitignored (see `.gitignore`). Chapters 11 and 12 ship
  checked-in bundles under `bundles/` instead.
- **Production code stays in `src/easycat/`.** These folders are
  teaching artifacts; they import from EasyCat but do not ship
  anything back.

## Pedagogical principles

1. **Small enough to hold in your head.** Each chapter introduces
   ~≤200 lines of new reader-facing code.
2. **Runnable at every checkpoint.** No "it'll work once we add
   three more files."
3. **Wrong version first.** Chapters 3, 5, 9 deliberately ship
   broken implementations to motivate the fix.
4. **Observable internal state.** Starting at chapter 2, every
   chapter either dumps a `RunBundle` or reads one.
5. **One axis of complexity per step.** If a chapter is about
   VAD, it is not also about noise reduction.
