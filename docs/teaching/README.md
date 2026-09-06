# Teaching: Voice Pipelines from Scratch

A progressive 16-chapter ladder for learning voice-AI pipelines
through EasyCat. Modeled after *Crafting Interpreters*, *Ray
Tracer in One Weekend*, and the `nanoGPT` tradition.

Each chapter is a **self-contained folder** under `docs/teaching/`
with a narrative `README.md` and a `main.py` entry point. Chapters
that demonstrate multiple approaches (2, 9, 11) also include named
companion scripts; chapter 12 has several standalone analysis scripts
and no single `main.py` — its README lists which to run and in what
order. Chapter `N+1` copies chapter `N`'s code as its starting
point and evolves from there — so every chapter folder is a frozen,
runnable artifact you can visit independently.

From this repository, `uv run easycat docs` prints the maintained
docs map that links back to this ladder. Use
`uv run easycat docs --audience learners` to narrow that map to learner-facing
routes, or `uv run easycat docs --audience learners --json` when automation
needs that smaller route map.
Coding agent? Use the root [AGENTS.md](../../AGENTS.md) for repository coding
rules; use [llms.txt](../../llms.txt) for machine-readable docs route discovery
or run `uv run easycat explain json-schema` (`uv run easycat doctor --json`
emits first-run environment checks as parseable rows).

> **Start here:** [`00-hello-audio/`](./00-hello-audio/). Copy the generated
> [progress worksheet](./PROGRESS.md) to keep an end-to-end completion record.

After completing the prerequisites below, launch the first chapter from the
repository root:

```bash
uv run python docs/teaching/00-hello-audio/main.py
```

For Chapters 0 and 1, their READMEs use the smaller
`uv sync --extra local --group dev` setup. Use the full `quickstart`
prerequisites below when you continue into provider-backed chapters.

## Hardware-free checkpoint spine

Every chapter now has at least one deterministic checkpoint that needs no
microphone, speaker, or provider credential. List the curated one-per-chapter
spine with concepts, prediction prompts, setup commands, evidence cues,
reflection prompts, and individual commands:

```bash
uv run python docs/teaching/offline_spine.py
uv run python docs/teaching/offline_spine.py --json
```

Write down your answer to **Predict** before running a checkpoint. After the
command, compare the observed JSON with **Look for**, then answer **Explain
after** using the exact fields that confirmed or overturned your prediction.
Keep the original prediction visible; a mismatch is evidence to explain, not an
answer to rewrite after the fact.

After finishing a chapter, replay only the cumulative spine you have completed.
For example, after Chapter 5:

```bash
uv sync --extra quickstart --group dev
uv run python docs/teaching/offline_spine.py --run --through 5 --jobs 4 --show-evidence
uv run python docs/teaching/offline_spine.py --run --through 5 --jobs 4 --json
```

The setup line matters on the otherwise-offline Chapters 11–12: their own
scripts need only the dev group, but cumulative replay still imports selected
probes from earlier provider-backed chapters.

After installing the full `quickstart` prerequisites below, execute all 16
checkpoints as a compact smoke run:

```bash
uv run python docs/teaching/offline_spine.py --run --jobs 4
uv run python docs/teaching/offline_spine.py --run --jobs 4 --json
```

The runner strips all `*_API_KEY` variables from each child process and
disables bytecode writes. Its curated probes keep generated artifacts in
temporary directories, so the full run leaves the checkout unchanged. The
runner captures successful output; rerun any printed chapter command directly
to study its full evidence, add `--show-evidence` to a human run, or read each
row's `observed` value in a JSON run. A pass requires exit code zero, one
parseable JSON document on stdout, and empty stderr; probes put intentional
failure scenarios inside the JSON instead of printing alarming errors. These
checkpoints are a hardware-free conceptual spine, not replacements for the
chapters' microphone/provider-backed main paths.

After editing a chapter, changing its copied code, or using one as a starting
point, run the repository validation lane from the root:

```bash
uv run easycat validate quick
uv run easycat validate quick --json
uv run easycat validate report .easycat/validation/latest.json
uv run easycat validate report .easycat/validation/latest.json --json
```

## Choose a starting point

| You have | Start with | Why |
|---|---|---|
| No mic or API keys | [Hardware-free checkpoint spine](./offline_spine.py), [`10-cleaning-signal`](./10-cleaning-signal/) offline replay, [`11-journal`](./11-journal/), or [`12-evals-and-latency`](./12-evals-and-latency/) | The spine reaches every chapter without credentials; chapter 10 uses checked-in WAV pairs, and chapters 11–12 use checked-in bundles. Chapter 12's `llm_judge.py` is the only optional live-key script. |
| A mic and speakers, but no API keys | [`00-hello-audio`](./00-hello-audio/) or [`01-echo`](./01-echo/) | They teach PCM and the `Transport` protocol without provider calls. |
| `OPENAI_API_KEY` | [`02-transcribe`](./02-transcribe/) | It adds STT and writes the first `RunBundle`. |
| `OPENAI_API_KEY` and `DEEPGRAM_API_KEY` | [`03-parrot-naive`](./03-parrot-naive/) through [`10-cleaning-signal`](./10-cleaning-signal/) | These chapters use streaming STT, VAD, TTS, agents, tools, smart-turn, interruption, and signal cleanup. |
| Provider or transport comparison work | [`13-swap-providers-and-transports`](./13-swap-providers-and-transports/) | It compares provider mixes and Local/WebRTC/Twilio transports after the eval chapters. |
| Production or custom-agent work | [`14-bring-your-own-agent`](./14-bring-your-own-agent/) or [`15-operate-in-production`](./15-operate-in-production/) | They focus on the bridge layer, `SessionManager`, lifecycle, debugger UI, and CLI. |

## The ladder

The [progress worksheet](./PROGRESS.md) pauses for closed-book integration
reviews after the Build, Operate, and Generalise phases, then closes with a Ship
review. Complete each phase gate before starting the next group of chapters.

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
| 10 | [`10-cleaning-signal`](./10-cleaning-signal/) | Noise reduction, AEC, duplex behavior. |
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
- Chapters 0-1 only need `uv sync --extra local --group dev` from
  the repo root.
- From chapter 2 onward, use `uv sync --extra quickstart --group dev`
  from the repo root. The `quickstart` extra bundles mic I/O,
  OpenAI, NumPy, and ONNX Runtime. Chapters 3-10 use Deepgram
  streaming STT by default, so run
  `uv sync --extra quickstart --extra deepgram --group dev` for
  those chapters. Chapter 10 additionally needs `--extra rnnoise`;
  add `--extra aec` for real echo cancellation. Chapter 13's
  `deepgram-eleven` provider mix additionally needs `--extra
  deepgram --extra elevenlabs`; its WebRTC and Twilio transport
  variants need `--extra webrtc` and `--extra telephony`,
  respectively.
- A mic and speakers for the live build chapters. Chapter 10's offline
  replay ships checked-in WAV pairs; chapters 11 and 12 ship checked-in
  bundles. Those paths need no audio hardware.
- API keys, set as environment variables:
  - `OPENAI_API_KEY` — default STT / TTS / agent provider.
  - `DEEPGRAM_API_KEY` — used in chapters 3-10 for streaming STT.
  - `ELEVENLABS_API_KEY` — used in chapter 13's provider-swap mix.
- Provider-backed chapters make live API calls that may incur charges.
  Review provider billing and usage limits before running them.
- Provider-backed scripts may send audio, transcripts, prompts, or eval
  content to configured services. Use non-sensitive test content and review
  provider data-handling policies first.
- After setting the keys for a chapter, run `uv run easycat doctor`
  from the repo root. It catches missing keys, local audio problems,
  journal path issues, and provider reachability before you debug
  chapter code. If the keys live in a project `.env`, run
  `uv run easycat doctor --env-file .env`; add `--json`
  (`uv run easycat doctor --env-file .env --json`) for the same
  environment/check rows as parseable output. When running a chapter script
  from `.env`, add `--env-file .env` after `uv run` in the command you run.

Each chapter's README lists its own prerequisites up front.

## Conventions

- **Copy, don't modify.** Chapter `N+1` copies chapter `N` as its
  starting point rather than editing in place. A little
  duplication is the intended cost; each folder stays readable on
  its own.
- **Narrative, exercises, self-check.** Each chapter keeps its concept
  narrative in `README.md`, one or more applied tasks in the dedicated
  `EXERCISES.md`, and a closing self-check. Generated source diffs can make
  a README long; keep the hand-authored explanation skimmable and use focused
  probes to isolate boundary claims. Generated handoffs connect those steps
  and return to the progress worksheet before pointing to the next chapter.
  Applied-task hints stay concealed behind numbered disclosures: learners take
  a first swing, reveal one hint, and make a fresh attempt before opening the
  next clue. A task is complete when the learner has
  kept an initial plan, the exact command or change plus an observation, and a
  causal explanation. Closing self-checks are closed-book retrieval gates:
  answer every numbered question, support each answer with attempt evidence,
  mark each answer pass or retry, and advance only at the chapter's N/N
  threshold. Phase reviews use the same mastery rule for synthesis: score
  **coverage**, **causality**, **evidence**, and **limits**, mark each criterion
  pass or retry, and enter the next phase only at 4/4. From chapter 2 onward,
  a generated prompt also
  revisits the checkpoint from two chapters earlier before the new narrative,
  then asks the learner to connect both concepts before checking the old probe.
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

1. **Small enough to hold in your head.** Each chapter introduces one
   primary question and keeps new runnable units focused. Generated source
   diffs show cumulative code without expanding that conceptual scope.
2. **Runnable at every checkpoint.** No "it'll work once we add
   three more files."
3. **Wrong version first.** Chapters 3, 5, 9 deliberately ship
   broken implementations to motivate the fix.
4. **Observable internal state.** Starting at chapter 2, every
   chapter either dumps a `RunBundle` or reads one.
5. **One primary axis per step.** Companion probes may expose adjacent
   boundary claims, but the chapter narrative keeps one question in focus.
6. **Recall is spaced and interleaved.** New chapters retrieve an earlier
   checkpoint before introducing their own concept; phase reviews then combine
   several chapters into one evidence-backed explanation.
