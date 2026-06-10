# Writing a Custom TTS Provider

`TTSProvider` (defined in `easycat.providers`) is the text-to-speech
contract. `synthesize()` returns an async iterator of provider-scoped
`TTSEvent` objects carrying `AudioChunk` payloads; Session maps them to the
EasyCat-level `TTSAudio` events and schedules playback.

## The surface

| Member | Purpose |
| --- | --- |
| `supports_ssml` (property) | Whether the provider accepts SSML natively. |
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

    supports_ssml = False

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

## Verifying conformance

```python
from easycat import TTSProvider


def test_silence_tts_conforms_to_protocol() -> None:
    assert isinstance(SilenceTTS(), TTSProvider)


async def test_silence_tts_streams_audio_events() -> None:
    tts = SilenceTTS()
    events = [event async for event in tts.synthesize("hi")]
    assert len(events) == 2
    assert all(event.audio is not None for event in events)
```

The in-tree behavioral contract lives in
[`tests/contracts/test_tts_provider_contracts.py`](../../tests/contracts/test_tts_provider_contracts.py);
mirror its cases (audio event streaming, `cancel()` discarding pending
output, teardown via `aclose`) when your provider talks to a real backend.

## Notes

- `cancel()` is the barge-in path — it must take effect quickly, even
  mid-stream. `stop()` may drain what was already buffered.
- Prefer exposing the typed `input_policy` property (the optional
  `TTSInputPolicyProvider` capability in `easycat.providers`) for new
  providers; `supports_ssml` remains the legacy structural flag.
- Best-effort word/phoneme alignment can be surfaced as
  `TTSEvent(type=TTSEventType.MARKERS, markers=[...])`; the shape is
  provider-native and recorded for debugging only.
