# Writing a Custom STT Provider

`STTProvider` (defined in `easycat.providers`) is the speech-to-text
contract. Audio flows in through `send_audio`; transcripts flow out as
provider-scoped `STTEvent` objects from the `events()` async iterator.
Session consumes those events and emits the EasyCat-level
`STTPartial` / `STTFinal` events itself.

Start an out-of-tree package with the STT-specific scaffold:

```bash
uv run easycat init my-stt --template provider-stt
```

It includes the config, `easycat.stt_providers` entry point, named
registration, `offline` capability declaration, all four `version_info()`
fields, an offline `STTProviderContractSuite`, and explicit TODOs for adding
credentials and an `integration_live` suite without contaminating local tests.

## The surface

| Member | Purpose |
| --- | --- |
| `async start_stream()` | Begin a new STT stream session. |
| `async send_audio(chunk)` | Feed one `AudioChunk` to the active stream. |
| `async commit_segment() -> bool` | Finalize the current segment without ending the stream; return `False` if unsupported. |
| `async end_stream()` | Signal that no more audio will be sent. |
| `events() -> AsyncIterator[STTEvent]` | Yield `STTEvent(type=PARTIAL\|FINAL, text=...)` objects. |
| `version_info() -> dict[str, str]` | `provider` / `model` / `api_version` / `sdk_version` for the journal. |

## A complete provider

An echo-style provider that "transcribes" every committed segment to a fixed
string — the smallest shape that exercises the whole contract:

```python
import asyncio
from collections.abc import AsyncIterator

from easycat.audio_format import AudioChunk
from easycat.events import STTEvent, STTEventType


class FixedSTT:
    """Yields one FINAL transcript per committed segment."""

    def __init__(self, transcript: str = "hello world") -> None:
        self._transcript = transcript
        self._events: asyncio.Queue[STTEvent | None] = asyncio.Queue()

    async def start_stream(self) -> None:
        self._events = asyncio.Queue()

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass  # a real provider streams chunk.data to its ASR backend

    async def commit_segment(self) -> bool:
        await self._events.put(STTEvent(type=STTEventType.FINAL, text=self._transcript))
        return True

    async def end_stream(self) -> None:
        await self._events.put(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        while (event := await self._events.get()) is not None:
            yield event

    def version_info(self) -> dict[str, str]:
        return {"provider": "fixed", "model": "none", "api_version": "v1", "sdk_version": "none"}
```

## Injecting it

```python
from easycat import EasyConfig, run

run(EasyConfig.mic(stt=FixedSTT(), agent=my_agent))
```

See `examples/custom_stt_provider.py` for a runnable wrapper-style variant.

### Receiving the session EventBus

If an injected provider emits provider-scoped errors or lifecycle events,
implement the public synchronous attachment hook:

```python
from easycat import EventBus


def set_event_bus(self, event_bus: EventBus) -> None:
    if self._event_bus is None:  # preserve a bus explicitly supplied by the app
        self._event_bus = event_bus
```

Session calls `set_event_bus()` before provider work starts. This is the
instance-injection contract; the provider may store its config as `config`,
`_settings`, or anything else. Private `_config` / `_event_bus` probing remains
only for compatibility with older providers.

## Verifying conformance

```python
from easycat.testing import STTProviderContractSuite


class TestFixedSTT(STTProviderContractSuite):
    provider_factory = FixedSTT


async def test_fixed_stt_yields_final_after_commit() -> None:
    stt = FixedSTT("hi")
    await stt.start_stream()
    assert await stt.commit_segment()
    await stt.end_stream()
    events = [event async for event in stt.events()]
    assert [event.text for event in events] == ["hi"]
```

The suite verifies async event iteration, normalized events, repeated stream
cycles, end-of-stream termination, and the rule that
`commit_segment() -> True` means the provider accepted the request. Empty or
silent segments may produce no `FINAL`; consume `events()` to observe actual
transcript completion.
`isinstance(provider, STTProvider)` checks member names only and is not a
behavioral conformance test. The in-tree use of the same installable suite
lives in
[`tests/contracts/test_stt_provider_contracts.py`](../../tests/contracts/test_stt_provider_contracts.py).

## Register a shortcut name

Injecting an instance (above) works for one session. To make your provider
selectable everywhere built-ins are — `stt="yours/model"` string shortcuts,
`easycat doctor` credential checks, `easycat init` scaffold extras, and
validation's URL redaction — register it with the catalog instead.

```python
from easycat import register_stt_provider

register_stt_provider(
    "yours",
    YourSTT,
    YourSTTConfig,
    env_var="YOURS_API_KEY",
    extra="yours",  # optional: install extra shipping your deps
    probe_module="easycat_yours",  # import checked by /health/ready
    capabilities=frozenset({"native_endpointing"}),  # if the provider owns turns
    api_domains=("yours.example.com",),  # optional: for URL redaction
)
```

If capabilities depend on the selected model or config, supply a resolver
instead of declaring the capability for every variant:

```python
def resolve_capabilities(
    config: object,
    model: str | None,
) -> frozenset[str]:
    selected_model = config.model if isinstance(config, YourSTTConfig) else model
    if selected_model and selected_model.endswith("-native"):
        return frozenset({"native_endpointing"})
    return frozenset()


register_stt_provider(
    "yours",
    YourSTT,
    YourSTTConfig,
    capability_resolver=resolve_capabilities,
)
```

The resolver receives either the concrete config instance or `None`, plus the
model selected by shortcut parsing when available. Its result is combined with
the static `capabilities` set.

`YourSTT` must accept a `YourSTTConfig` instance as its constructor argument —
the same contract built-in providers follow. A config that declares
`event_bus: EventBus | None = None` receives the session bus before provider
construction; no provider is required to consume it. Live instances use
`set_event_bus()` as described above. Credentialed providers declare
`env_var=...` and need an `api_key` field; local/self-hosted providers omit
both.
For the `"yours/model-name"` shortcut syntax, it also needs a `model` field (or
a `MODEL_FIELD: ClassVar[str]` naming the field to use if it is called
something else; TTS configs such as `ElevenLabsTTSConfig` use
`MODEL_FIELD = "model_id"`).

Once registered, `"yours"` participates in `create_stt_provider`,
`available_stt_providers`, and `stt="yours/some-model"` resolution exactly
like `"openai"` or `"deepgram"` do.

What each metadata field feeds:

| Field | Consumed by |
| --- | --- |
| `env_var` | Optional `easycat doctor` credential check and auto-filled API key for `"yours/model"` shortcuts; omit for local/self-hosted providers |
| `extra` | `easycat init` scaffold, to add the right install extra to a generated `pyproject.toml` |
| `probe_module` | `/health/ready` import check; set this when the extra and Python module names differ, or when your provider ships no pip extra at all — `easycat plan` then reports it under `missing_backends` instead of reporting it ready |
| `capabilities` | planner and session behavior; declare `native_endpointing` when STT finals own turn boundaries — `easycat plan` then reports the `vad` role as `off` |
| `capability_resolver` | model/config-dependent capabilities; returns a `frozenset[str]` that is combined with static capabilities |
| `api_domains` | validation's redaction, to scrub your API host from exported debug bundles |

When `native_endpointing` is declared, `EasyConfig` drives turns from STT
FINAL events and disables its own VAD/smart-turn endpointing. Omit it when the
provider expects EasyCat to commit segments.

`easycat plan` and `/health/ready` report the same decision: the `vad` role
comes back as `off` with the `disabled` capability, so a missing VAD install
extra is **not** a blocking gap for that deployment. Set `smart_turn`,
`turn_taking=TurnManagerConfig(mode=TurnMode.PUSH_TO_TALK)`, or the voicemail
detector to take endpointing back, and the VAD role — and its extra — return.

For a credential-free local provider, omit `env_var`; shortcut parsing and the
planner then construct the config without reading an API key:

```python
register_stt_provider(
    "local-whisper",
    LocalWhisperSTT,
    LocalWhisperConfig,  # no api_key field required
    extra="local-whisper",
    probe_module="local_whisper",
)

config = EasyConfig(stt="local-whisper/base", tts=..., agent=...)
```

### Auto-registering from a pip-installed package

A third-party package can register itself automatically — no explicit call
required from the app — by exposing a zero-arg callable under the
`easycat.stt_providers` entry-point group:

```toml
# pyproject.toml of the third-party package
[project.entry-points."easycat.stt_providers"]
yours = "easycat_yours:register"
```

```python
# easycat_yours/__init__.py
from easycat import register_stt_provider

from ._provider import YourSTT, YourSTTConfig


def register() -> None:
    register_stt_provider(
        "yours",
        YourSTT,
        YourSTTConfig,
        env_var="YOURS_API_KEY",
        extra="yours",
        probe_module="easycat_yours",
        capabilities=frozenset({"native_endpointing"}),
        api_domains=("yours.example.com",),
    )
```

EasyCat scans this entry-point group lazily, once, the first time any STT
factory function is called — so simply `pip install`ing the package is
enough for `stt="yours/model"` to work with no import or registration call
in the app. A plugin that fails to load or register logs a warning instead
of breaking every other provider.

## Notes

- Providers that can report uncommitted audio may also implement
  `pending_commit_bytes() -> int | None` (the optional
  `PendingCommitReporter` capability) so the journal can explain commit
  decisions.
- Need the session `EventBus`? Declare the optional `event_bus` field on the
  provider config as shown above; both session construction and the public
  factory inject it when the caller has not already supplied one.
