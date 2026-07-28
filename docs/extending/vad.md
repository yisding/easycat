# Writing a Custom VAD Provider

`VADProvider` (defined in `easycat.providers`) is the voice activity
detection contract. The provider consumes `AudioChunk` objects and yields
`VADStartSpeaking` / `VADStopSpeaking` events that drive the turn manager's
state machine.

## The surface

| Member | Purpose |
| --- | --- |
| `process(chunk) -> AsyncIterator[Event]` | Yield `VADStartSpeaking` / `VADStopSpeaking` as speech state changes. |
| `configure(*, min_speech_duration_ms, min_silence_duration_ms, sensitivity)` | Adjust detection thresholds. |
| `version_info() -> dict[str, str]` | `provider` / `model` / `api_version` / `sdk_version` for the journal. |

## A complete provider

An RMS energy gate — no model download, fully offline, and the same shape the
`provider` scaffold template generates:

```python
import math
from array import array
from collections.abc import AsyncIterator

from easycat import Event, VADStartSpeaking, VADStopSpeaking
from easycat.audio_format import AudioChunk


class EnergyVAD:
    """Flags speech whenever PCM16 RMS energy crosses a threshold."""

    def __init__(self, threshold: float = 500.0) -> None:
        self._threshold = threshold
        self._speaking = False

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        samples = array("h", chunk.data)
        rms = math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0
        loud = rms >= self._threshold
        if loud and not self._speaking:
            self._speaking = True
            yield VADStartSpeaking()
        elif not loud and self._speaking:
            self._speaking = False
            yield VADStopSpeaking()

    def configure(
        self,
        *,
        min_speech_duration_ms: int = 250,
        min_silence_duration_ms: int = 150,
        sensitivity: float = 0.5,
    ) -> None:
        self._threshold = 1000.0 * (1.0 - sensitivity)

    def version_info(self) -> dict[str, str]:
        return {"provider": "energy", "model": "rms", "api_version": "v1", "sdk_version": "none"}
```

A production VAD should also debounce with the configured
`min_speech_duration_ms` / `min_silence_duration_ms` windows; the turn
manager tolerates chatty VADs, but debouncing avoids spurious turn churn.

## Injecting it

```python
from easycat import EasyConfig, run

run(EasyConfig.mic(vad=EnergyVAD(), agent=my_agent))
```

See `examples/custom_vad_provider.py` for a runnable wrapper-style variant,
and `examples/vad_backends.py` for pinning the built-in backends.

## Verifying conformance

```python
from easycat import VADStartSpeaking
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.testing import VADProviderContractSuite


class TestEnergyVAD(VADProviderContractSuite):
    provider_factory = EnergyVAD


async def test_energy_vad_detects_loud_audio() -> None:
    vad = EnergyVAD(threshold=100.0)
    loud = AudioChunk(data=b"\xe8\x03" * 160, format=PCM16_MONO_16K)  # 1000s
    events = [event async for event in vad.process(loud)]
    assert isinstance(events[0], VADStartSpeaking)
```

The suite verifies configuration, async event iteration, and balanced speech
boundaries. `isinstance(provider, VADProvider)` checks member names only and
is not a behavioral conformance test. The in-tree use of the same installable
suite lives in
[`tests/contracts/test_vad_provider_contracts.py`](../../tests/contracts/test_vad_provider_contracts.py).

## Notes

- `process()` is called on the hot audio path — keep per-chunk work bounded
  and never block the event loop.
- Yield events only on state *transitions*, not on every chunk.
