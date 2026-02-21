# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

EasyCat is a Python voice bot framework that plugs into OpenAI Agents SDK or PydanticAI. It handles the full audio pipeline: noise reduction → VAD → STT → agent → TTS, with pluggable providers at each stage.

## Commands

```bash
uv sync --group dev              # Install project + dev tools
uv run pytest                    # Run full test suite
uv run pytest tests/stt/test_stt_openai.py              # Run one test file
uv run pytest tests/test_metrics.py::TestLatencyStats::test_record_and_stats  # Run one test
uv run ruff check .              # Lint
uv run ruff format .             # Format
uv run python examples/ws_server.py  # Run an example
```

## Architecture

**Pipeline flow:** Transport (audio in) → NoiseReducer → VAD → STT → [SmartTurn] → Agent → TTS → Transport (audio out)

**Key modules:**
- `session.py` — Core orchestrator (~1000 LOC). Wires all pipeline stages, manages turn lifecycle and interruption.
- `config.py` — `EasyCatConfig` (simplified, auto-wires OpenAI providers) and `SessionConfig` (advanced, explicit providers). `create_session()` factory builds a wired Session.
- `events.py` — `EventBus` pub/sub with sync/async handlers. Two event layers: provider-scoped (`STTEvent`, `TTSEvent`) emitted by providers, mapped to EasyCat-level events (`STTFinal`, `TTSAudio`, `TurnStarted`, etc.) by Session.
- `providers.py` — `@runtime_checkable` Protocol definitions for all provider interfaces (`STTProvider`, `TTSProvider`, `VADProvider`, `Transport`, `NoiseReducer`). Providers use duck typing, not inheritance.
- `turn_manager.py` — 5-state FSM (IDLE → USER_SPEAKING → USER_PAUSED → PROCESSING → BOT_SPEAKING) with pre-roll buffering and interruption detection. Supports VAD (automatic) and PUSH_TO_TALK turn modes.
- `agent_runner.py` — Wraps agents with timeout, tracing, cancellation. Supports both simple and streaming agents.
- `smart_turn.py` — Optional ONNX-based endpoint detection that classifies whether a user has finished speaking, enabling faster turn transitions without waiting for silence timeout.

**Provider subpackages** (`stt/`, `tts/`, `transports/`, `telephony/`): one provider per file, each implementing the corresponding Protocol. Base classes (`STTBase`, `TTSBase`, `_ServerTransportBase`) provide shared plumbing.

**Agent adapters** (`agents/`): `BaseAgentAdapter` provides shared history/cancellation; `OpenAIAgentsAdapter` and `PydanticAIAdapter` wrap framework-specific agents.

**Dual-backend fallback:** VAD (Krisp → Silero → passthrough) and noise reduction (Krisp → RNNoise → passthrough) both try commercial backends first, then fall back to open-source, then no-op.

## Key Patterns

- **Protocol over inheritance** — all providers defined as `typing.Protocol` in `providers.py`
- **Async-first** — all I/O is async; providers are async iterators
- **Cooperative cancellation** — `CancelToken` (not exceptions) for turn/TTS cancellation
- **Factory functions** — `create_session()`, `create_vad()`, `create_noise_reducer()`
- **Provider registries** — `stt/factory.py` and `tts/factory.py` each have a central `_PROVIDER_TO_CONFIG` dict. To add a new STT/TTS provider: add an entry to the registry and a corresponding config dataclass.
- **Event bus injection** — Deepgram and ElevenLabs providers require an `EventBus` injected at construction (they emit provider-scoped events). OpenAI providers do not.
- **Noop stubs** (`stubs.py`) — `NoopSTT`, `NoopTTS`, `NoopVAD`, `NoopTransport` for test isolation

## Style

- Python ≥3.11, typing-first
- 4-space indent, 99-char line limit (ruff)
- Ruff rules: E, F, I, W, UP
- Commit format: `<scope>: <imperative summary>` (e.g., `stt: normalize partial transcript events`)

## Testing

- pytest with pytest-asyncio (`asyncio_mode = auto`)
- `@pytest.mark.integration` for live API tests (skipped without credentials)
- Tests mirror source structure: `tests/stt/`, `tests/tts/`, `tests/session/`, etc.
