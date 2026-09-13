# Writing a Custom VAD Provider

`VADProvider` (defined in `easycat.providers`) is the voice activity
detection contract. The provider consumes `AudioChunk` objects and yields
`VADStartSpeaking` / `VADStopSpeaking` events that drive the turn manager's
state machine.

Start an out-of-tree package with the compatibility-preserved VAD scaffold:

```bash
uv run easycat init my-vad --template provider
```

Use `provider-stt` or `provider-tts` for those speech surfaces; `provider`
intentionally remains the VAD starter so existing automation does not break.

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
from dataclasses import dataclass

from easycat import Event, VADStartSpeaking, VADStopSpeaking
from easycat.audio_format import AudioChunk


@dataclass
class EnergyVADConfig:
    threshold: float = 500.0


class EnergyVAD:
    """Flags speech whenever PCM16 RMS energy crosses a threshold."""

    def __init__(
        self,
        config: EnergyVADConfig | None = None,
        *,
        threshold: float | None = None,
    ) -> None:
        configured = config or EnergyVADConfig()
        self._threshold = configured.threshold if threshold is None else threshold
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

## Injecting it directly

```python
from easycat import EasyConfig, run

run(EasyConfig.mic(vad=EnergyVAD(), agent=my_agent))
```

See `examples/custom_vad_provider.py` for a runnable wrapper-style variant,
and `examples/vad_backends.py` for pinning the built-in backends.

If an injected VAD emits provider-scoped events beyond its returned
start/stop iterator, implement synchronous `set_event_bus(event_bus)`. Session
calls this public hook for VAD, noise-reduction, and echo-cancellation
instances as well as STT, TTS, and transports.

## Registering a named VAD

Reusable packages can make a config selectable by shortcut from `EasyConfig`,
`easycat.toml`, and the provider planner:

```python
from easycat import register_vad_provider


def register() -> None:
    register_vad_provider(
        "energy",
        EnergyVAD,
        EnergyVADConfig,
        capabilities=frozenset({"offline"}),
    )
```

The provider constructor receives the registered config. `EasyConfig(vad="energy")`
and a manifest `vad = "energy"` both resolve `EnergyVADConfig()` and construct
`EnergyVAD(config)` when the session starts. If the config declares a model
field (or a custom `MODEL_FIELD` class variable), `"energy/model-name"` fills
that field using the same shortcut grammar as STT/TTS.

For automatic discovery from an installed package, publish the zero-argument
registrar under the `easycat.vad_providers` entry-point group:

```toml
[project.entry-points."easycat.vad_providers"]
energy = "my_energy_vad:register"
```

EasyCat loads this group lazily on the first VAD lookup. Registration metadata
also accepts `extra`, `probe_module`, `capabilities`, optional `env_var`, and
`api_domains`, matching the STT/TTS catalogs. Local VADs normally omit
`env_var`; no dummy API key is required.

A built-in or registered backend may declare a probe module so the planner can
report it missing without a pip extra — the case for a commercial SDK that ships
no PyPI package, where there is no extra to install and `easycat plan` would
otherwise report a backend the session refuses to construct as ready.

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
