# Writing a Custom TTS Provider

`TTSProvider` (defined in `easycat.providers`) is the text-to-speech
contract. `synthesize()` returns an async iterator of provider-scoped
`TTSEvent` objects carrying `AudioChunk` payloads; Session maps them to the
EasyCat-level `TTSAudio` events and schedules playback.

Start an out-of-tree package with the TTS-specific scaffold:

```bash
uv run easycat init my-tts --template provider-tts
```

It includes the config, `easycat.tts_providers` entry point, named
registration, `offline` capability declaration, all four `version_info()`
fields, an offline `TTSProviderContractSuite`, and explicit TODOs for adding
credentials and an `integration_live` suite without contaminating local tests.

## The surface

| Member | Purpose |
| --- | --- |
| `synthesize(payload) -> AsyncIterator[TTSEvent]` | Stream audio for one text payload (`str` or `TTSInput`). |
| `async stop()` | Gracefully stop the current synthesis. |
| `async cancel()` | Immediately cancel and discard pending output (barge-in). |
| `version_info() -> dict[str, str]` | `provider` / `model` / `api_version` / `sdk_version` for the journal. |

## A complete provider

A silence generator that emits 50 ms of PCM per character — useful as a
deterministic stand-in and as the smallest complete shape:

```python
from collections.abc import AsyncIterator

from easycat import PCM16_MONO_24K, AudioChunk
from easycat.events import TTSEvent, TTSEventType
from easycat.tts.input import TTSInput


class SilenceTTS:
    """Synthesizes silence proportional to the payload length."""

    def __init__(self) -> None:
        self._cancelled = False

    async def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        self._cancelled = False
        text = payload if isinstance(payload, str) else payload.text
        frame = b"\x00\x00" * (PCM16_MONO_24K.sample_rate // 20)  # 50 ms
        for _ in text:
            if self._cancelled:
                return
            chunk = AudioChunk(data=frame, format=PCM16_MONO_24K)
            yield TTSEvent(type=TTSEventType.AUDIO, audio=chunk)

    async def stop(self) -> None:
        self._cancelled = True

    async def cancel(self) -> None:
        self._cancelled = True

    def version_info(self) -> dict[str, str]:
        return {"provider": "silence", "model": "none", "api_version": "v1", "sdk_version": "none"}
```

## Injecting it

```python
from easycat import EasyConfig, run

run(EasyConfig.mic(tts=SilenceTTS(), agent=my_agent))
```

See `examples/custom_tts_provider.py` for a runnable wrapper-style variant.

### Receiving the session EventBus

An injected TTS provider that reports provider-scoped errors or lifecycle
events implements the public synchronous hook:

```python
from easycat import EventBus


def set_event_bus(self, event_bus: EventBus) -> None:
    if self._event_bus is None:
        self._event_bus = event_bus
```

Session calls it before synthesis starts. This contract is independent of how
the provider stores its config; private `_config` / `_event_bus` probing remains
only as a compatibility fallback.

## Verifying conformance

```python
from easycat.testing import TTSProviderContractSuite


class TestSilenceTTS(TTSProviderContractSuite):
    provider_factory = SilenceTTS


async def test_silence_tts_streams_audio_events() -> None:
    tts = SilenceTTS()
    events = [event async for event in tts.synthesize("hi")]
    assert len(events) == 2
    assert all(event.audio is not None for event in events)
```

The suite verifies the async stream, normalized audio events, and idempotent
stop/cancel behavior. `isinstance(provider, TTSProvider)` checks member names
only and is not a behavioral conformance test. The in-tree use of the same
installable suite lives in
[`tests/contracts/test_tts_provider_contracts.py`](../../tests/contracts/test_tts_provider_contracts.py);
add live-backend cases for provider-specific cancellation and teardown.

## Register a shortcut name

Injecting an instance (above) works for one session. To make your provider
selectable everywhere built-ins are — `tts="yours/voice"` string shortcuts,
`easycat doctor` credential checks, `easycat init` scaffold extras, and
validation's URL redaction — register it with the catalog instead.

```python
from easycat import register_tts_provider

register_tts_provider(
    "yours",
    YourTTS,
    YourTTSConfig,
    env_var="YOURS_API_KEY",
    extra="yours",  # optional: install extra shipping your deps
    probe_module="easycat_yours",  # import checked by /health/ready
    api_domains=("yours.example.com",),  # optional: for URL redaction
)
```

`YourTTS` must accept a `YourTTSConfig` instance as its constructor argument —
the same contract built-in providers follow. A config that declares
`event_bus: EventBus | None = None` receives the session bus before provider
construction; no provider is required to consume it. Live instances use
`set_event_bus()` as described above. Credentialed providers declare
`env_var=...` and need an `api_key` field; local/self-hosted providers omit
both.
For the `"yours/voice-name"` shortcut syntax, it also needs a `model` field (or
a `MODEL_FIELD: ClassVar[str]` naming the field to use if it is called
something else, e.g. ElevenLabs' `model_id`).

Once registered, `"yours"` participates in `create_tts_provider`,
`available_tts_providers`, and `tts="yours/some-voice"` resolution exactly
like `"openai"` or `"elevenlabs"` do.

What each metadata field feeds:

| Field | Consumed by |
| --- | --- |
| `env_var` | Optional `easycat doctor` credential check and auto-filled API key for `"yours/voice"` shortcuts; omit for local/self-hosted providers |
| `extra` | `easycat init` scaffold, to add the right install extra to a generated `pyproject.toml` |
| `probe_module` | `/health/ready` import check; set this when the extra and Python module names differ, or when your provider ships no pip extra at all — `easycat plan` then reports it under `missing_backends` instead of reporting it ready |
| `capabilities` | static provider capabilities exposed by `easycat plan` and `/capabilities` |
| `api_domains` | validation's redaction, to scrub your API host from exported debug bundles |

For a credential-free local provider, omit `env_var`; shortcut parsing and the
planner then construct the config without reading an API key:

```python
register_tts_provider(
    "local-piper",
    LocalPiperTTS,
    LocalPiperConfig,  # no api_key field required
    extra="local-piper",
    probe_module="local_piper",
)

config = EasyConfig(stt=..., tts="local-piper/en_US", agent=...)
```

### Auto-registering from a pip-installed package

A third-party package can register itself automatically — no explicit call
required from the app — by exposing a zero-arg callable under the
`easycat.tts_providers` entry-point group:

```toml
# pyproject.toml of the third-party package
[project.entry-points."easycat.tts_providers"]
yours = "easycat_yours:register"
```

```python
# easycat_yours/__init__.py
from easycat import register_tts_provider

from ._provider import YourTTS, YourTTSConfig


def register() -> None:
    register_tts_provider(
        "yours",
        YourTTS,
        YourTTSConfig,
        env_var="YOURS_API_KEY",
        extra="yours",
        probe_module="easycat_yours",
        api_domains=("yours.example.com",),
    )
```

EasyCat scans this entry-point group lazily, once, the first time any TTS
factory function is called — so simply `pip install`ing the package is
enough for `tts="yours/voice"` to work with no import or registration call in
the app. A plugin that fails to load or register logs a warning instead of
breaking every other provider.

## Notes

- `cancel()` is the barge-in path — it must take effect quickly, even
  mid-stream. `stop()` may drain what was already buffered.
- Expose the optional typed `input_policy` property (`TTSInputPolicyProvider`
  in `easycat.providers`) when the provider accepts more than plain text.
- Best-effort word/phoneme alignment can be surfaced as
  `TTSEvent(type=TTSEventType.MARKERS, markers=[...])`; the shape is
  provider-native and recorded for debugging only.
