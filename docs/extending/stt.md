# Writing a Custom STT Provider

`STTProvider` (defined in `easycat.providers`) is the speech-to-text
contract. Audio flows in through `send_audio`; transcripts flow out as
provider-scoped `STTEvent` objects from the `events()` async iterator.
Session consumes those events and emits the EasyCat-level
`STTPartial` / `STTFinal` events itself.

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

## Verifying conformance

```python
from easycat import STTProvider


def test_fixed_stt_conforms_to_protocol() -> None:
    assert isinstance(FixedSTT(), STTProvider)


async def test_fixed_stt_yields_final_after_commit() -> None:
    stt = FixedSTT("hi")
    await stt.start_stream()
    assert await stt.commit_segment()
    await stt.end_stream()
    events = [event async for event in stt.events()]
    assert [event.text for event in events] == ["hi"]
```

The in-tree behavioral contract lives in
[`tests/contracts/test_stt_provider_contracts.py`](../../tests/contracts/test_stt_provider_contracts.py);
mirror its cases (partial-before-final ordering, end-of-stream termination,
teardown via `aclose`) when your provider talks to a real backend.

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
    api_domains=("yours.example.com",),  # optional: for URL redaction
)
```

`YourSTT` must accept a `YourSTTConfig` instance as its constructor argument —
the same contract built-in providers follow. To receive the session
`EventBus`, declare `event_bus: EventBus | None = None` on `YourSTTConfig`; the
factory injects the bus into that optional config field before constructing the
provider. `YourSTTConfig` also needs an `api_key` field.
For the `"yours/model-name"` shortcut syntax, it also needs a `model` field (or
a `MODEL_FIELD: ClassVar[str]` naming the field to use if it is called
something else, e.g. ElevenLabs' `model_id`).

Once registered, `"yours"` participates in `create_stt_provider`,
`available_stt_providers`, and `stt="yours/some-model"` resolution exactly
like `"openai"` or `"deepgram"` do.

What each metadata field feeds:

| Field | Consumed by |
| --- | --- |
| `env_var` | `easycat doctor` env-var checks; auto-filled API key for `"yours/model"` shortcuts |
| `extra` | `easycat init` scaffold, to add the right install extra to a generated `pyproject.toml` |
| `api_domains` | validation's redaction, to scrub your API host from exported debug bundles |

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
