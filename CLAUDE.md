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

**Pipeline flow:** Transport (audio in) → NoiseReducer → VAD → STT → Agent → TTS → Transport (audio out)

**Key modules:**
- `session.py` — Core orchestrator (~1000 LOC). Wires all pipeline stages, manages turn lifecycle and interruption.
- `config.py` — `EasyCatConfig` (simplified, auto-wires OpenAI providers) and `SessionConfig` (advanced, explicit providers). `create_session()` factory builds a wired Session.
- `events.py` — `EventBus` pub/sub with sync/async handlers. Provider-scoped events (`STTEvent`, `TTSEvent`) get mapped to EasyCat-level events by Session.
- `providers.py` — `@runtime_checkable` Protocol definitions for all provider interfaces (`STTProvider`, `TTSProvider`, `VADProvider`, `Transport`, `NoiseReducer`). Providers use duck typing, not inheritance.
- `turn_manager.py` — 5-state FSM (IDLE → USER_SPEAKING → USER_PAUSED → PROCESSING → BOT_SPEAKING) with pre-roll buffering and interruption detection.
- `agent_runner.py` — Wraps agents with timeout, tracing, cancellation. Supports both simple and streaming agents.

**Provider subpackages** (`stt/`, `tts/`, `transports/`, `telephony/`): one provider per file, each implementing the corresponding Protocol. Base classes (`STTBase`, `TTSBase`, `_ServerTransportBase`) provide shared plumbing.

**Agent adapters** (`agents/`): `BaseAgentAdapter` provides shared history/cancellation; `OpenAIAgentsAdapter` and `PydanticAIAdapter` wrap framework-specific agents.

## Key Patterns

- **Protocol over inheritance** — all providers defined as `typing.Protocol` in `providers.py`
- **Async-first** — all I/O is async; providers are async iterators
- **Cooperative cancellation** — `CancelToken` (not exceptions) for turn/TTS cancellation
- **Factory functions** — `create_session()`, `create_vad()`, `create_noise_reducer()`
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
