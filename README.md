# EasyCat

Slim, batteries-included voice bot framework that runs idiomatic agents and
workflows from OpenAI Agents SDK, PydanticAI, LangChain, LangGraph,
LlamaAgents, Remote Responses API, or your own async workflow.

### Quickstart

`VoiceApp` is the beginner and product-level entry point: give it your agent,
then explicitly choose where it runs. The README, first example, feature ladder,
and default scaffold all use this same shape:

```python
from agents import Agent

from easycat import VoiceApp

app = VoiceApp(agent=Agent(name="assistant", instructions="You are a helpful voice assistant."))
app.run("local")
```

`local` uses your microphone and speakers. The same app can run in `browser`,
`websocket`, or `twilio` mode when you install that mode's extras and server
requirements. See [examples/voice_app.py](examples/voice_app.py). The separate
`easycat serve` command runs EasyCat's bundled playground (or an explicit
server manifest); it does not import the `VoiceApp` in your Python file.

Graduate to `EasyConfig` when you need explicit providers, turn taking,
journaling, or a caller-owned session:

```python
from agents import Agent

from easycat import EasyConfig, run

run(
    EasyConfig.mic(
        agent=Agent(name="assistant", instructions="You are a helpful voice assistant.")
    )
)
```

Async application? Use `await arun(config)`. Calling `run(...)` from an active
event loop fails before startup and points you to `arun(...)`.

The convenience helpers show live console feedback only on an interactive
stderr. Use `feedback="off"` to keep a process quiet or `feedback="on"` to
force first-run transcript/status output when stderr is redirected.

> `VoiceApp(...).run("local")` and `EasyConfig.mic(...)` automatically wire
> OpenAI Realtime STT (`gpt-realtime-whisper`) and OpenAI TTS from
> `OPENAI_API_KEY` when you do not override `stt` or `tts`. This is provider
> traffic and may incur charges. If you omit the key, supply STT and TTS
> configs explicitly.

## Install

Python 3.11+ is required.

First prove the checkout works without API keys, audio hardware, or provider
traffic:

```bash
uv sync --group dev
uv run easycat --version
uv run easycat console --voice-demo
```

`console --voice-demo` runs one scripted turn through the real audio pipeline
and writes a replayable debug bundle. Ambient API keys are ignored; only
`easycat console --live` opts into provider traffic that may incur charges.

Local microphone/speaker modes also need the PortAudio runtime. Install it
before the Python extra on Linux or macOS:

```bash
# Debian/Ubuntu
sudo apt-get update
sudo apt-get install -y libportaudio2

# macOS
brew install portaudio
```

EasyCat is not published to PyPI yet. For a portable application dependency,
pin a Git commit (replace `<commit-sha>` with the revision you audited):

```toml
[project]
dependencies = ["easycat[quickstart]"]

[tool.uv.sources]
easycat = { git = "https://github.com/yisding/easycat.git", rev = "<commit-sha>" }
```

Use `easycat = { path = "/path/to/easycat", editable = true }` only when
developing EasyCat and the application together. Generated scaffolds make that
editable source explicit when they are created from a local checkout. Generate
a portable scaffold directly with
`uv run easycat init my-agent --easycat-git https://github.com/yisding/easycat.git --easycat-git-rev <commit-sha>`;
Git and local source options are mutually exclusive.

For this repository, the commands below go from the checkout to a talking bot.
The first command preserves an existing `.env`; after it runs, edit `.env` and
add your key before running doctor:

```bash
uv sync --extra quickstart --group dev
test -e .env || cp .env.example .env
uv run easycat doctor --env-file .env
uv run --env-file .env python examples/openai_agents_voice.py
```

Prefer exported shell variables? `uv run easycat doctor` and
`uv run python examples/openai_agents_voice.py` work the same once
`OPENAI_API_KEY` is exported. Add `--json` (`uv run easycat doctor --json`, or
`uv run easycat doctor --env-file .env --json`) for parseable checks. See the
[installation and extras guide](docs/install.md) for browser, telephony,
provider, agent-framework, and local-model installs.

## Choose Your Path

| You want to | Start here | First move |
|---|---|---|
| Run a local mic/speaker voice bot | [Install](#install) | `uv sync --extra quickstart --group dev`, then `uv run easycat doctor`; for `.env` keys, use `uv run easycat doctor --env-file .env` and `uv run --env-file .env python examples/openai_agents_voice.py` |
| No mic or API key yet | [Journal demo](examples/journal_demo.py) and [hardware-free teaching spine](docs/teaching/#hardware-free-checkpoint-spine) | Run `uv run easycat console --voice-demo` or `uv run python examples/journal_demo.py`; use `uv run python docs/teaching/offline_spine.py --run --jobs 4` for one credential-free checkpoint from every chapter |
| Learn EasyCat feature by feature | [EasyCat feature ladder](docs/using-easycat/) | Start with [`VoiceApp`](docs/using-easycat/00-first-voice-app/), then add one product capability per chapter |
| Learn the pipeline step by step | [Teaching ladder](docs/teaching/) | Pick a chapter from its starting-point table |
| Choose a runnable example | [Examples matrix](examples/README.md) | Use its chooser for no-key, browser, provider, or debugging examples |
| Scaffold a new app | [CLI and scaffolds](#cli) | `uv run easycat init --list-templates` before `uv run easycat init my-agent` |
| Contribute or validate a change | [Developer textbook](docs/development/) and [Contributing](CONTRIBUTING.md) | Read the system map, then use the [validation workflow](docs/validation.md) and run `uv run easycat validate quick` |
| Maintain architecture, package boundaries, or coding-agent context | [Architecture map](CLAUDE.md) and [agent guide](AGENTS.md) | Review provider registries, session lifecycle, `uv run easycat docs --audience maintainers`, and `uv run easycat docs --audience coding-agents` |
| Operate or debug sessions | [Observability](docs/observability.md) and [Docker deployment](docs/deployment/docker.md) | Run `easycat bundles list`; add `uv sync --extra debugger --group dev` for the UI |

## Learn the pipeline from scratch

The 16-chapter [teaching ladder](docs/teaching/) walks the entire voice pipeline
ground-up, from audio chunks through production operations. Each chapter is a
self-contained folder with a runnable program and exercises. Use this route when
you want to understand the machinery rather than only assemble an application.

## Learn EasyCat feature by feature

The [EasyCat feature ladder](docs/using-easycat/) starts with a working
`VoiceApp`, then adds runtime modes, providers, conversation controls, tools,
agent frameworks, sessions, debugging, evals, servers, telephony, and operations
one capability at a time.

## CLI

These examples use the installed form. From this repository, prefix commands
with `uv run`, for example `uv run easycat doctor`.

```bash
easycat console --voice-demo        # deterministic, keyless proof of life
easycat init --list-templates       # compare application/provider scaffolds
easycat doctor                      # inspect local readiness without changing it
easycat doctor --env-file .env --json
easycat serve                       # bundled browser playground on loopback
easycat docs --audience app-builders
easycat validate quick              # deterministic local validation
easycat bundles list                # discover captured runs and journals
```

`easycat init --list-templates` reports each scaffold's base `easycat[...]`
package requirement and extras, required environment variables, optional
environment knobs, generated files, and copyable
create/preflight/check/fix/docs/json-schema/run commands. Add `--json` for the
machine-readable catalog. A generated default app
uses the same `VoiceApp(...).run("local")` shape as the quickstart.

Use the [CLI reference](docs/cli.md) for every command family, `easycat docs`
for audience-specific routes, and `easycat explain json-schema` for the stable
JSON envelope. Coding agents should start with [AGENTS.md](AGENTS.md) and
[llms.txt](llms.txt).

## Current capabilities

- Session pipeline: noise reduction, echo cancellation, VAD, turn taking, STT,
  agent streaming, output processors, and TTS.
- STT and TTS: OpenAI, Deepgram, ElevenLabs, and Cartesia.
- VAD: Silero, FunASR, optional TEN VAD, and Krisp. Noise reduction: RNNoise,
  Krisp, and a passthrough fallback.
- Transports: Local, WebSocket, WebRTC, WebTransport, and Twilio Media Streams.
- Agent/workflow adapters: `OpenAIAgentsBridge`, `PydanticAIBridge`,
  `LangChainBridge`, `LangGraphBridge`, `LlamaAgentsBridge`,
  `RemoteResponsesAPIBridge`, and `GenericWorkflowBridge`.
- Typed events, interruption, telephony actions, multi-session servers,
  durable journals, replay, latency analysis, and debugger tooling.

## Build beyond the quickstart

EasyCat does not replace your agent framework. Pass an OpenAI Agents SDK,
PydanticAI, LangChain, LangGraph, LlamaAgents, Remote Responses API, or your own
async workflow object to `VoiceApp`/`EasyConfig`; the matching bridge is selected
automatically. Construct a bridge directly only when you need bridge-specific
options.

The top-level import surface is curated and lazy. See the
[public API contract](docs/public-api.md) before adding or depending on new
`from easycat import ...` names.

Detailed routes:

- [VoiceApp → EasyConfig → Session](docs/from-easyconfig-to-session.md) for
  lifecycle ownership, typed event subscriptions, async turns, and postmortems.
- [Runtime modes](docs/using-easycat/01-runtime-modes/) and the
  [browser playground](docs/browser-playground.md) for local and server modes.
- [Providers and voices](docs/using-easycat/02-providers-and-voices/) and
  [provider authoring](docs/extending/) for hosted, local, and third-party stages.
- [Tools and output processors](docs/using-easycat/04-tools-actions/) and
  [agent bridges](docs/using-easycat/05-agent-bridges/).
- [Telephony](docs/using-easycat/10-telephony/) and
  [production servers](docs/deployment/production-servers.md).
- [Observability](docs/observability.md), [testing and evals](docs/testing-and-evals.md),
  and the [validation workflow](docs/validation.md).
- [Runnable examples](examples/README.md) with exact extras, environment
  variables, and commands.

For the complete maintained map, run `uv run easycat docs` or open
[docs/README.md](docs/README.md). Repository architecture lives in
[CLAUDE.md](CLAUDE.md); contribution commands live in
[CONTRIBUTING.md](CONTRIBUTING.md).
