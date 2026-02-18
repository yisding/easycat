# EasyCat Repository Analysis

Thorough analysis of the codebase for possible improvements and architectural deficiencies.

**Baseline:** 713 tests pass, 11 skipped. Ruff linting clean. The codebase is well-structured overall -- the issues below are refinements, not critical failures.

---

## Critical / High-Impact

### 1. ~~Massive Code Duplication in `session.py`~~ — FIXED

All three duplication clusters have been resolved:

- **Tracer span cleanup** — Consolidated into `SpanManager` (`_span_manager.py`). All lifecycle methods now call `self._spans.finish_all(status)`.
- **TTS event loop** — Extracted into `TTSSynthesizer` (`tts_synthesizer.py`). Both basic and streaming paths delegate to `self._tts_synth.synthesize()`.
- **Agent invocation tracer branching** — `SpanManager` handles tracer-or-not checks internally, eliminating the `if self._tracer` / `else` branches.

### 2. ~~`SessionConfig` Uses `Any` for All Provider Fields~~ — FIXED

Provider fields are now typed as `STTProvider | None`, `TTSProvider | None`, etc., using the Protocol types from `providers.py`.

### 3. ~~Dual Turn State Tracking Creates Divergence Risk~~ — FIXED

`Session` no longer maintains its own `_turn_state`. The `turn_state` property now derives from `TurnManager.state` via a mapping (`_TM_TO_TURN_STATE`), making TurnManager the single source of truth.

---

## Medium-Impact

### 4. ~~No CI/CD Pipeline~~ — FIXED

Added `.github/workflows/ci.yml` with lint (ruff check + format) and test (pytest) jobs.

### 5. `OpenAISTT` Creates a New HTTP Client Per Request

`stt/openai_provider.py:81-83` -- Every call to `_transcribe_streaming` (every turn) creates a new `httpx.AsyncClient` and closes it afterward, unless one was injected. This means no HTTP connection pooling across turns. `OpenAITTS` correctly creates a persistent client in `__init__`, but `OpenAISTT` doesn't follow the same pattern.

**Recommendation:** Create the client in `__init__` and reuse it, matching the `OpenAITTS` pattern.

### 6. `LatencyStats.values` Grows Without Bound

`metrics.py:56` -- Every latency measurement is appended to `values` forever. For long-running sessions, this is a memory leak. There's no cap, ring buffer, or percentile computation.

**Recommendation:** Cap the list (e.g., 10000 entries), use a reservoir sample, or remove `values` and keep only aggregates.

### 7. Global Mutable Sentence Segmenter Hardcoded to English

`session.py:76`:
```python
_SENTENCE_SEGMENTER = pysbd.Segmenter(language="en", clean=False, char_span=True)
```

Module-level singleton, hardcoded to English, shared across all sessions.

**Recommendation:** Make the segmenter per-Session or per-config with a configurable language parameter.

### 8. No `close()` / Resource Cleanup Protocol for STT Providers

`OpenAITTS` has `close()` for its HTTP client. Neither `OpenAISTT` nor the Deepgram/ElevenLabs STT providers expose one. The `STTProvider` protocol doesn't include it. Long-running sessions may leak HTTP clients or WebSocket connections.

**Recommendation:** Add an optional `close()` to the `STTProvider`/`TTSProvider` protocols and call it during `Session.stop()`.

### 9. `resample()` Silently Swallows Non-Import Errors

`audio_utils.py:22-49` -- The soxr and scipy paths use bare `except Exception: pass`, meaning any error (including corrupted data causing a numpy crash) is silently caught and falls through to linear interpolation.

**Recommendation:** Catch `ImportError` narrowly for the fallback chain. Let other exceptions (data corruption, memory errors) propagate.

### 10. No `__aenter__`/`__aexit__` on `Session`

`Session` requires manual `start()` / `stop()` calls. Users can easily forget `stop()` on error paths.

**Recommendation:** Implement the async context manager protocol:
```python
async def __aenter__(self) -> Session:
    await self.start()
    return self

async def __aexit__(self, *exc_info) -> None:
    await self.stop()
```

### 11. Unbounded TTS Queue in Streaming Agent Path

`session.py:802`:
```python
tts_queue: asyncio.Queue[str | None] = asyncio.Queue()
```

The TTS queue in `_run_streaming_agent` is unbounded. If the agent produces text faster than TTS can synthesize, the queue grows without limit. The outbound audio queue has `BoundedAudioQueue` with explicit drop policies, but this intermediate queue does not.

**Recommendation:** Add a maxsize to the queue (e.g., 10 sentences) or use a bounded queue.

### 12. Pre-1.0 Dependencies With No Upper Bound

`pyproject.toml`:
```
pydantic-ai = ["pydantic-ai>=0.1"]
openai-agents = ["openai-agents>=0.0.7"]
```

Pre-1.0 packages with no upper bound. Breaking API changes will silently break EasyCat.

**Recommendation:** Add upper bounds like `<1.0` for pre-release dependencies.

---

## Low-Impact / Nice-to-Have

### 13. `EventBus.emit()` Swallows Handler Errors

`events.py:328-345` -- Handler exceptions are logged but never surfaced. For critical events like `Error` or `TurnEnded`, a swallowed handler exception can leave the pipeline in an inconsistent state.

**Recommendation:** Add an `on_handler_error` callback to `EventBus`, or at minimum collect and return failed handlers.

### 14. `with_tts_timeout` and `with_stt_timeout` Raise in `finally` Block

`timeouts.py:149-157` -- Raising in `finally` can mask the original exception from the `try` body. If the generator body raises before the timeout fires, the original exception is replaced by the timeout error.

**Recommendation:** Use `raise err from None` outside the `finally`, or store and re-raise after the `finally` block.

### 15. `Span.set_error` Type Should Be `BaseException`

`tracing.py:59` accepts `Exception`, but `Error` event wraps `BaseException`. `KeyboardInterrupt` or `SystemExit` during a pipeline stage would cause a type mismatch.

**Recommendation:** Change to `BaseException`.

### 16. `_drain_outbound_audio` Fragile Exit Condition

`session.py:1049-1061` -- If `_is_running` is False but the queue isn't empty, the loop continues. If the transport `send_audio` blocks or errors repeatedly, the drain loop spins. The exit depends on `close()` eventually raising `QueueEmpty`, which is correct but fragile.

**Recommendation:** Add a max-iteration or timeout guard.

### 17. `BotStartedSpeaking` / `BotStoppedSpeaking` Event Gaps

In the streaming path, `BotStartedSpeaking` is emitted only after the first TTS chunk arrives. If TTS fails before producing any audio, neither lifecycle event fires. Consumers listening for these events see an incomplete lifecycle.

**Recommendation:** Emit `BotStartedSpeaking` when TTS *begins* (before first chunk), not when the first chunk arrives. Or emit a `BotSpeakingFailed` event on error.

### 18. Missing `__init__.py` in Some Test Subdirectories

`tests/agent/`, `tests/audio/`, `tests/events/`, `tests/providers/`, `tests/session/`, `tests/turns/`, `tests/vad/`, `tests/websocket/` lack `__init__.py`. While pytest discovers tests without them, this can cause issues with name collisions and prevents relative imports within test packages.

### 19. `pysbd` Upstream Warning

Test output shows `SyntaxWarning: invalid escape sequence '\s'` from pysbd. This will become an error in Python 3.16+.

**Recommendation:** Pin pysbd version or add a warning filter until upstream fixes it.

### 20. `openai-agents` Not in Core Dependencies

The `openai-agents` package is listed under optional dependencies, but `OpenAIAgentsAdapter` is imported in `__init__.py`. The adapter guards its framework import with `try/except ImportError`, so this works at runtime. However, mypy/pyright will flag the unguarded import of `OpenAIAgentsAdapter` itself from `__init__.py` if the framework isn't installed. Consider lazy-loading the adapter classes.

---

## Summary

| Priority | Issue | Status |
|----------|-------|--------|
| ~~High~~ | ~~Session.py code duplication (~3 clusters)~~ | **FIXED** (SpanManager + TTSSynthesizer) |
| ~~High~~ | ~~SessionConfig typed as Any~~ | **FIXED** |
| ~~High~~ | ~~Dual turn state tracking~~ | **FIXED** (derived from TurnManager) |
| ~~Medium~~ | ~~No CI/CD~~ | **FIXED** |
| Medium | OpenAISTT client-per-request | Open |
| Medium | LatencyStats unbounded growth | Open |
| Medium | Hardcoded English segmenter | Open |
| Medium | No STT close() protocol | Open |
| Medium | resample() error swallowing | Open |
| Medium | No async context manager | Open |
| Medium | Unbounded TTS queue | Open |
| Medium | Pre-1.0 dep upper bounds | Open |
| Low | EventBus error swallowing | Open |
| Low | Timeout raise-in-finally | Open |
| Low | Span.set_error type | Open |
| Low | Drain loop fragility | Open |
| Low | Bot lifecycle event gaps | Open |
| Low | Missing test __init__.py | Open |
| Low | pysbd warnings | Open |
| Low | Lazy adapter loading | Open |
