# Extending EasyCat

EasyCat providers are duck-typed: every pluggable stage is defined as a
`typing.Protocol` in `easycat.providers`, so an out-of-tree provider is just a
class with the right methods — no base class and no fork of this repository.
Live-instance injection is always available; reusable packages can additionally
register named configs and entry points. This directory teaches those paths,
one page per protocol:

- [stt.md](stt.md) — speech-to-text (`STTProvider`)
- [tts.md](tts.md) — text-to-speech (`TTSProvider`)
- [vad.md](vad.md) — voice activity detection (`VADProvider`)
- [transport.md](transport.md) — audio transports (`Transport`), including the
  public `easycat.transports` base classes
- [agent-bridge.md](agent-bridge.md) — agent frameworks (`ExternalAgentBridge`)

## The baseline: plain instance injection

You never need a registry to use a custom provider. Construct the instance
yourself and pass it to `EasyConfig` (or `Session.from_providers`) — this
works today for every stage:

```python
from easycat import EasyConfig, run

from my_provider_pkg import MySTT, MyTTS, MyVAD

run(
    EasyConfig.mic(
        stt=MySTT(),
        tts=MyTTS(),
        vad=MyVAD(),
        agent=my_agent,
    )
)
```

`EasyConfig` accepts provider *instances* anywhere it accepts a provider
shortcut string or config dataclass: `stt=`, `tts=`, `vad=`,
`noise_reduction=`, `echo_cancellation=`, `transport=`, and `agent=`.

An instance that emits provider-scoped events implements synchronous
`set_event_bus(event_bus)`. Session calls that public hook for every audio
stage before work starts; providers no longer need to expose a guessed private
attribute name. Registered config dataclasses can instead declare an optional
`event_bus` field, which session construction fills when unset.

Reusable STT, TTS, VAD, noise-reducer, and echo-canceller packages can register
a provider/config pair. Registration adds shortcut parsing, planner metadata,
readiness probes, and lazy package discovery while preserving direct injection:

- `register_stt_provider` / entry-point group `easycat.stt_providers`
- `register_tts_provider` / entry-point group `easycat.tts_providers`
- `register_vad_provider` / entry-point group `easycat.vad_providers`
- `easycat.noise_reduction.register_noise_reducer_provider` /
  entry-point group `easycat.noise_reducer_providers`
- `easycat.echo_cancellation.register_echo_canceller_provider` /
  entry-point group `easycat.echo_canceller_providers`

## Verifying conformance

Subclass the installable behavioral contract kit in your provider package:

```python
from easycat.testing import STTProviderContractSuite


class TestMySTT(STTProviderContractSuite):
    provider_factory = MySTT
```

The suite exercises the async signatures and lifecycle semantics Session
actually relies on. The `@runtime_checkable` protocols in
`easycat.providers` remain useful for dispatch, but `isinstance()` checks only
member names—not callability, async behavior, signatures, or return types—so
do not use it as a provider acceptance test. Each extending page shows the
matching suite and any surface-specific knobs.
EasyCat's installed pytest plugin registers the suite's `contract` marker, so
external projects can keep `strict_markers = true` without duplicating marker
configuration.

## Scaffolding an external provider package

`easycat init` ships one focused package starter per speech surface. Every
starter generates `pyproject.toml`, a typed config, a structural provider,
the matching lazy-discovery entry point, declared capabilities, an offline
contract suite, complete version metadata, and an opt-in live-test checklist:

| Surface | Template | Entry-point group | Offline example |
| --- | --- | --- | --- |
| STT | `provider-stt` | `easycat.stt_providers` | deterministic final transcript |
| TTS | `provider-tts` | `easycat.tts_providers` | deterministic PCM16 tone |
| VAD | `provider` | `easycat.vad_providers` | RMS energy gate |

`provider` remains the VAD template name for compatibility. Choose the
surface explicitly for new STT/TTS packages:

```bash
uv run easycat init my-stt --template provider-stt
uv run easycat init my-tts --template provider-tts
uv run easycat init my-vad --template provider
```

Compare it with the other starting points via
`uv run easycat init --list-templates`, or
`uv run easycat init --list-templates --json` when automation needs the
catalog.

## Commands

```bash
uv run easycat docs --audience provider-maintainers        # provider-author route map
uv run easycat docs --audience provider-maintainers --json # same map for automation
uv run easycat init my-stt --template provider-stt         # external STT package
uv run easycat init my-tts --template provider-tts         # external TTS package
uv run easycat init my-vad --template provider             # external VAD package
uv run python examples/custom_transport.py                 # runnable custom transport
uv run pytest tests/test_public_api.py                     # guard the public surfaces
uv run pytest tests/contracts                              # offline protocol contracts
```

## Ground rules for every provider

- **Async-first** — all I/O methods are `async`; streaming output is an
  async iterator.
- **`version_info()`** — every provider returns a `dict[str, str]` with
  `provider`, `model`, `api_version`, and `sdk_version` keys; the journal
  records it for postmortems.
- **Cooperative cancellation** — react to `stop()` / `cancel()` /
  `CancelToken` promptly instead of raising.
- **Optional teardown** — implement `async def aclose(self)` (or `close`)
  when you hold sockets or HTTP clients; Session calls it during `stop()`.
- **Events stay provider-scoped** — STT/TTS providers yield `STTEvent` /
  `TTSEvent` objects; Session maps them to EasyCat-level events. Never emit
  `STTFinal` / `TTSAudio` yourself.
- **Failures stay observable** — attach the injected config `event_bus` and
  publish provider failures as `Error` events before re-raising. Use the
  stable factories in `easycat.errors` when an EasyCat code applies (for
  example `EASYCAT_E304` for a mid-call provider disconnect).
