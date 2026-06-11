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

## Notes

- Providers that can report uncommitted audio may also implement
  `pending_commit_bytes() -> int | None` (the optional
  `PendingCommitReporter` capability) so the journal can explain commit
  decisions.
- Need the session `EventBus`? Take it as a constructor argument like the
  Deepgram/ElevenLabs providers do — Session never injects it implicitly.
