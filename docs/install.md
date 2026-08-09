# Installation and optional extras

Use the root [quickstart](../README.md#install) for the shortest supported path.
This page is the authoritative dependency matrix for choosing a smaller install,
adding a transport/provider, or preparing a downstream application.

## Start keyless

From this repository, prove the CLI and offline pipeline before installing audio
drivers or configuring credentials:

```bash
uv sync --group dev
uv run easycat --version
uv run easycat console --voice-demo
```

Bare `easycat console` and `easycat console --voice-demo` stay offline even when
provider keys exist in the environment. Use `easycat console --live` only when
you intend to make provider requests.

## Application dependency before PyPI publication

EasyCat is not published to PyPI yet. Pin a reviewed Git revision in a portable
application:

```toml
[project]
dependencies = ["easycat[quickstart]"]

[tool.uv.sources]
easycat = { git = "https://github.com/yisding/easycat.git", rev = "<commit-sha>" }
```

An absolute editable path is appropriate only when the application and EasyCat
checkout are developed together:

```toml
[tool.uv.sources]
easycat = { path = "/path/to/easycat", editable = true }
```

Generated projects record an editable source when they are scaffolded from a
local checkout. To generate a project that can install in CI or on another
developer's machine without editing `pyproject.toml`, select the portable source
at scaffold time:

```bash
uv run easycat init my-agent --easycat-git https://github.com/yisding/easycat.git --easycat-git-rev <commit-sha>
```

`--easycat-git` and `--easycat-source` are mutually exclusive. The Git revision
is optional, but pinning a reviewed commit makes installs reproducible. Keep
private-repository credentials in Git's credential helper or SSH agent; the
scaffold rejects credentials embedded in HTTP(S) URLs.

## The quickstart bundle

The `quickstart` extra includes local audio, OpenAI providers, OpenAI Agents
SDK, NumPy, onnxruntime, Silero VAD, Smart Turn, and LiveKit AEC3:

```bash
uv sync --extra quickstart --group dev
```

Local microphone/speaker use also requires the PortAudio runtime:

```bash
# Debian/Ubuntu
sudo apt-get update
sudo apt-get install -y libportaudio2

# macOS
brew install portaudio
```

RNNoise remains opt-in because its Python binding brings a larger dependency
chain. TEN VAD is also separate because its license is non-permissive. Silero
uses EasyCat's bundled ONNX model; no torch required.

For a lean local OpenAI voice install with the same main pieces:

```bash
uv sync --extra local --extra openai --extra openai-agents --extra silero-vad --extra aec --group dev
```

## Extras by capability

Commands below are for this repository. In an application, use the equivalent
`uv add 'easycat[<extra>]'` form.

| Capability | Repository install |
| --- | --- |
| Local audio | `uv sync --extra local --group dev` |
| WebRTC browser transport | `uv sync --extra webrtc --group dev` |
| WebTransport | `uv sync --extra webtransport --group dev` |
| Twilio SDK/media streams | `uv sync --extra telephony --group dev` |
| FastAPI/uvicorn telephony server | `uv sync --extra telephony-fastapi --group dev` |
| OpenAI Agents SDK | `uv sync --extra openai-agents --group dev` |
| PydanticAI stable v1 | `uv sync --extra pydantic-ai --group dev` |
| PydanticAI stable v2 | `uv sync --extra pydantic-ai-v2 --group dev` |
| LangChain core | `uv sync --extra langchain --group dev` |
| LangGraph | `uv sync --extra langgraph --group dev` |
| LlamaAgents/LlamaIndex workflows | `uv sync --extra llama-agents --group dev` |
| OpenAI providers | `uv sync --extra openai --group dev` |
| Deepgram providers | `uv sync --extra deepgram --group dev` |
| ElevenLabs providers | `uv sync --extra elevenlabs --group dev` |
| Cartesia providers | `uv sync --extra cartesia --group dev` |
| LiveKit AEC3 | `uv sync --extra aec --group dev` |
| RNNoise | `uv sync --extra rnnoise --group dev` |
| TEN VAD | `uv sync --extra ten-vad --group dev` |
| Silero VAD | `uv sync --extra silero-vad --group dev` |
| FunASR VAD | `uv sync --extra funasr-vad --group dev` |
| Smart Turn | `uv sync --extra smart-turn --group dev` |
| Debugger UI | `uv sync --extra debugger --group dev` |

Krisp is supplied outside the optional-dependency table:

```bash
uv pip install krisp_audio
```

Deepgram, ElevenLabs, and Cartesia use EasyCat's core WebSocket/HTTP stack, so
their extras are install markers and do not add vendor SDKs. LangChain model
packages such as `langchain-openai` remain application choices.

The `pydantic-ai` extra targets stable v1. The `pydantic-ai-v2` extra installs
`pydantic-ai>=2.24.0,<3.0.0`; the two extras are mutually exclusive.

## Broad evaluation install

For a downstream application evaluating most integrations:

```bash
uv add 'easycat[all,pydantic-ai]'
# or, for stable PydanticAI v2:
uv add 'easycat[all,pydantic-ai-v2]'
```

In this repository:

```bash
uv sync --extra all --extra pydantic-ai --group dev
# or:
uv sync --extra all --extra pydantic-ai-v2 --group dev
```

The `all` extra deliberately omits `ten-vad` because of its non-permissive
license and omits the mutually exclusive `pydantic-ai` and `pydantic-ai-v2`
extras.

## Credentials without accidental overwrites

The root `.env.example` contains names, never secrets. Preserve an existing
`.env`, edit it in place, and ask doctor to load it:

```bash
test -e .env || cp .env.example .env
uv run easycat doctor --env-file .env
uv run easycat doctor --env-file .env --json
```

Doctor rejects known placeholder values. Its network liveness row does not
claim that credentials are valid; authenticated provider failures remain
separate from DNS/TLS/endpoint reachability.

## Runtime notes

- SoXR is a core dependency because transports and providers cross sample-rate
  boundaries. A dependency-free filtered resampler remains a fallback if the
  native backend fails.
- Cartesia and ElevenLabs WebSocket TTS keep one context-multiplexed socket per
  voice session by default and warm it during startup. Set
  `persistent_ws=False` to use one socket per utterance.
- Install only the extras selected by your transport, providers, agent
  framework, and processing features. `easycat doctor` reports missing
  requirements for the current scaffold/application rather than treating every
  hosted provider as mandatory.
