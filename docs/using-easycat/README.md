# Teaching: Use EasyCat Feature by Feature

This is the app-builder teaching ladder. It starts with EasyCat's public API
and adds one product capability at a time: runtime modes, providers,
turn-taking, tools, lifecycle, debugging, evals, servers, telephony, and
production operations.

The existing [voice-pipeline ladder](../teaching/) takes the complementary
route: it starts with PCM bytes and builds the machinery underneath a voice
agent. Follow that ladder when you want to understand *how voice AI works*.
Follow this one when you want to learn *what EasyCat can do and how to use it*.

> **Start here:** [`00-first-voice-app/`](./00-first-voice-app/).

After completing the prerequisites below, run the first chapter from the
repository root:

```bash
uv run python docs/using-easycat/00-first-voice-app/main.py
```

From this repository, `uv run easycat docs` prints the maintained docs map.
Use `uv run easycat docs --audience learners` for the learner routes or
`uv run easycat docs --audience learners --json` for the same routes and
copyable commands as structured data.

## Choose this ladder when

- You have built an agent or workflow and want to give it a voice.
- You want to compare EasyCat's local, browser, WebSocket, and phone modes.
- You want a guided tour of configuration, providers, turn-taking, tools,
  observability, testing, and deployment.
- You prefer to begin with a working app and reveal lower-level control only
  when a feature needs it.

If you need to understand VAD pre-roll, streaming TTS overlap, endpointing, or
the journal record shape from first principles, jump sideways to the matching
chapter in the [voice-pipeline ladder](../teaching/).

## The ladder

Every chapter is a self-contained folder with a narrative `README.md`, a
runnable `main.py`, and exercises. The ladder is complete: all twelve rungs
below are published and runnable, so read them in order or jump to the feature
you need.

| # | Chapter | EasyCat features |
|---|---|---|
| 0 | [`00-first-voice-app`](./00-first-voice-app/) | `VoiceApp`, automatic pipeline wiring, and the local runtime |
| 1 | [`01-runtime-modes`](./01-runtime-modes/) | Browser, WebSocket, local, and Twilio runtime modes |
| 2 | [`02-providers-and-voices`](./02-providers-and-voices/) | STT/TTS provider specs, voices, and environment preflight |
| 3 | [`03-conversation-controls`](./03-conversation-controls/) | VAD, smart turn, interruption, push-to-talk, noise reduction, and AEC |
| 4 | [`04-tools-actions`](./04-tools-actions/) | Agent tools, tool events, session actions, and pronunciation rules |
| 5 | [`05-agent-bridges`](./05-agent-bridges/) | OpenAI Agents, PydanticAI, LangChain, LangGraph, LlamaAgents, the Remote Responses API, and custom bridges |
| 6 | [`06-session-control`](./06-session-control/) | `EasyConfig`, `Session`, events, text turns, and lifecycle |
| 7 | [`07-observability`](./07-observability/) | Journals, bundles, inspect, replay, diff, and the debugger |
| 8 | [`08-testing-evals`](./08-testing-evals/) | Offline turns, assertions, evals, and latency budgets |
| 9 | [`09-multi-caller`](./09-multi-caller/) | Per-connection factories, authentication, limits, and supervision |
| 10 | [`10-telephony`](./10-telephony/) | Twilio streams, outbound calls, screening, IVR, and call control |
| 11 | [`11-production-ops`](./11-production-ops/) | Validation, deployment, durability, metrics, and production teardown |

## Prerequisites

- Python 3.11+.
- `uv sync --extra quickstart --group dev` from the repository root.
- `OPENAI_API_KEY` for the default OpenAI STT, TTS, and example agent.
- A microphone and speakers for chapter 0.
- Run `uv run easycat doctor` after exporting the key. If it lives in a
  project `.env`, run `uv run easycat doctor --env-file .env`. Use
  `uv run easycat doctor --json` or
  `uv run easycat doctor --env-file .env --json` when you need parseable
  environment checks.
- When a chapter command needs keys from `.env`, add `--env-file .env` after
  `uv run`, for example:

  ```bash
  uv run --env-file .env python docs/using-easycat/00-first-voice-app/main.py
  ```

Later chapters list any additional extras and credentials before their run
commands.

## Conventions

- **Public API first.** Lessons use exported EasyCat surfaces unless the point
  of the chapter is extension or internals.
- **One product capability per rung.** The running app grows, but each chapter
  has one main user outcome.
- **Runnable checkpoints.** Every available chapter has a documented command
  and an offline guard; live provider calls remain an explicit learner action.
- **Cross-link concepts instead of duplicating them.** The voice-pipeline
  ladder remains the deep explanation of audio and pipeline mechanics.
- **Observe every feature.** Chapters add journals, bundles, tests, or CLI
  inspection as soon as those surfaces become relevant.
