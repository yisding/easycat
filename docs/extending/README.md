# Extending EasyCat

EasyCat providers are duck-typed: every pluggable stage is defined as a
`typing.Protocol` in `easycat.providers`, so an out-of-tree provider is just a
class with the right methods — no base class, no registry entry, and no fork
of this repository. This directory teaches that path, one page per protocol:

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
shortcut string or config dataclass: `stt=`, `tts=`, `vad=`, `transport=`,
and `agent=`. Registry entries (`stt/factory.py`, `tts/factory.py`) are only
for providers shipped inside EasyCat itself.

## Verifying conformance

Each protocol in `easycat.providers` is `@runtime_checkable`, so the cheapest
conformance check is structural:

```python
from easycat import STTProvider

assert isinstance(MySTT(), STTProvider)
```

That catches missing methods but not behavior. For behavior, mirror the
offline protocol contract tests under [`tests/contracts/`](../../tests/contracts/README.md)
— they define what the Session actually relies on per stage (event ordering,
cancellation, teardown). Each extending page includes a minimal pytest
conformance test you can copy into your package.

## Scaffolding an external provider package

`easycat init` ships a `provider` template that generates a standalone
package skeleton — `pyproject.toml`, a Protocol-conforming provider with a
config dataclass, a conformance test, and a runnable demo that injects the
provider through `EasyConfig`:

```bash
uv run easycat init my-provider --template provider
```

Compare it with the other starting points via
`uv run easycat init --list-templates`, or
`uv run easycat init --list-templates --json` when automation needs the
catalog.

## Commands

```bash
uv run easycat docs --audience provider-maintainers        # provider-author route map
uv run easycat docs --audience provider-maintainers --json # same map for automation
uv run easycat init my-provider --template provider        # external package skeleton
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
