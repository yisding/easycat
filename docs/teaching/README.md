# Teaching: Voice Pipelines from Scratch

A progressive 16-chapter ladder for learning voice-AI pipelines
through EasyCat. Modeled after *Crafting Interpreters*, *Ray
Tracer in One Weekend*, and the `nanoGPT` tradition.

Each chapter is a **self-contained folder** under `docs/teaching/`
with a narrative `README.md` and a runnable `main.py` (or a couple
of scripts). Chapter `N+1` copies chapter `N`'s code as its starting
point and evolves from there — so every chapter folder is a frozen,
runnable artifact you can visit independently.

From this repository, `uv run easycat docs` prints the maintained
docs map that links back to this ladder. Use
`uv run easycat docs --json` when a script or coding agent needs the
same route map with command hints and audience labels; replace uppercase
placeholders such as `PATH` before running those hints. Use
`uv run easycat explain json-schema` for the JSON envelope and field contract.
Use `uv run easycat doctor --json` when a script or coding agent needs
parseable first-run environment checks; use
`uv run easycat doctor --env-file .env --json` when those checks should load
your `.env`.

> **Start here:** [`00-hello-audio/`](./00-hello-audio/).

After completing the prerequisites below, launch the first chapter from the
repository root:

```bash
uv run python docs/teaching/00-hello-audio/main.py
```

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
| 14 | [`14-bring-your-own-agent`](./14-bring-your-own-agent/) | Drop the agent framework. Bridge, session actions, pronunciation pipeline. |

### Ship — from demo to production

| # | Folder | What you add |
|---|---|---|
| 15 | [`15-operate-in-production`](./15-operate-in-production/) | `SessionManager`, lifecycle discipline, the debugger UI, the CLI. |

## Prerequisites

- Python 3.11+.
- `uv sync --extra quickstart --group dev` from the repo root.
  The `quickstart` extra bundles mic I/O, OpenAI, NumPy, and
  ONNX Runtime — enough for chapters 0-2 and 11-12. Chapters 3-10
  use Deepgram streaming STT by default, so run
  `uv sync --extra quickstart --extra deepgram --group dev` for
  those chapters. Chapter 10 gets RNNoise from `quickstart`; add
  `--extra aec` for real echo cancellation. Chapter 13's
  `deepgram-eleven` provider mix additionally needs `--extra
  deepgram --extra elevenlabs`; its WebRTC and Twilio transport
  variants need `--extra webrtc` and `--extra telephony`,
  respectively.
- A mic and speakers for the build chapters. Chapters 11 and 12
  ship checked-in bundles you can read without hardware.
- API keys, set as environment variables:
  - `OPENAI_API_KEY` — default STT / TTS / agent provider.
  - `DEEPGRAM_API_KEY` — used in chapters 3-10 for streaming STT.
  - `ELEVENLABS_API_KEY` — used in chapter 13's provider-swap mix.
- After setting the keys for a chapter, run `uv run easycat doctor`
  from the repo root. It catches missing keys, local audio problems,
  journal path issues, and provider reachability before you debug
  chapter code. If the keys live in a project `.env`, run
  `uv run easycat doctor --env-file .env`. Add `--json` when a script or coding
  agent needs the same environment/check rows.

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
- **Bundles are ZIP/JSON archives, not trust anchors.** `load_bundle`
  validates archive structure and path traversal, but bundles are not
  signed or tamper-evident. Only open bundles you generated yourself
  or got from a source you trust. Chapters 11 and 12 repeat this
  inline because the checked-in fixtures are the first ones most
  readers open.

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
