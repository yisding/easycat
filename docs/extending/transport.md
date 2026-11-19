# Writing a Custom Transport

`Transport` (defined in `easycat.providers`) is the audio I/O contract:
connection lifecycle plus bidirectional audio streaming. Unlike the other
stages, EasyCat also ships public building blocks in `easycat.transports` so
you do not have to reimplement queueing and degradation reporting:

- `AudioQueueMixin` — inbound audio queue, sentinel-based shutdown, the
  `receive_audio()` iterator, `wait_for_client()`, and rate-limited
  `TransportDegraded` emission via `self._emit_degraded(...)`.
- `ServerTransportBase` — `AudioQueueMixin` plus a managed `websockets`
  server (`connect()` starts it, `disconnect()` tears it down; you implement
  `_handle_connection(ws)`).
- `TransportDegraded` — the event both emit on the session bus when frames
  are dropped or a peer tears down abnormally.

This surface is part of the documented public API — see the
[public API contract](../public-api.md).

## The surface

| Member | Purpose |
| --- | --- |
| `async connect()` | Establish the connection (or start the server). |
| `async disconnect()` | Close the connection; unblock `receive_audio()`. |
| `receive_audio() -> AsyncIterator[AudioChunk]` | Yield captured audio until end-of-stream. |
| `async send_audio(chunk) -> bool` | Deliver bot audio; `False` means silently dropped. |
| `async clear_audio()` | Optional: drop buffered outbound audio on barge-in. |
| `version_info() -> dict[str, str]` | `provider` / `model` / `api_version` / `sdk_version` for the journal. |

Optional capability attributes the wiring inspects with `getattr`:
`audio_format` (the transport's PCM contract), `preferred_tts_output_format`,
and `default_echo_cancellation_enabled`.

## A complete transport

An in-memory loopback built on `AudioQueueMixin` — push audio in with
`feed()`, collect bot audio from `sent`:

```python
from easycat import AudioChunk
from easycat.transports import AudioQueueMixin


class MemoryTransport(AudioQueueMixin):
    """In-memory Transport: feed() audio in, collect bot audio from sent."""

    transport_kind = "memory"  # appears as TransportDegraded.provider

    def __init__(self) -> None:
        self._init_audio_queue(
            max_pending_chunks=256,
            max_pending_bytes=4 * 1024 * 1024,
        )
        self.sent: list[AudioChunk] = []

    async def connect(self) -> None:
        self._connected = True
        self._client_connected.set()

    async def disconnect(self) -> None:
        self._enqueue_sentinel()  # unblocks receive_audio()
        self._connected = False
        await self._drain_emit_tasks()

    def feed(self, chunk: AudioChunk) -> None:
        # Drops + emits TransportDegraded("inbound_queue_full") when full.
        self._enqueue_chunk(chunk, context="memory")

    async def send_audio(self, chunk: AudioChunk) -> bool:
        self.sent.append(chunk)
        return self._connected

    async def clear_audio(self) -> None:
        self.sent.clear()  # nothing buffered downstream; drop what we hold

    def version_info(self) -> dict[str, str]:
        return {"provider": "memory", "model": "none", "api_version": "v1", "sdk_version": "none"}
```

`receive_audio()`, `is_connected`, and `wait_for_client()` come from the
mixin. Both queue limits are enforced independently: `max_pending_chunks`
bounds ordinary small-frame latency, while `max_pending_bytes` prevents a few
large frames from retaining disproportionate memory. Either overflow drops the
new frame and emits `TransportDegraded("inbound_queue_full")`.

For a transport that hosts its own WebSocket server, subclass
`ServerTransportBase` instead and implement `_handle_connection(ws)` — see
`src/easycat/transports/websocket.py` for the canonical subclass.

## Injecting it

```python
from easycat import EasyConfig, create_session

session = create_session(EasyConfig(transport=MemoryTransport(), agent=my_agent))
```

`EasyConfig` distinguishes transport *instances* from transport *configs*
structurally, so any object with the audio surface above is accepted. See
`examples/custom_transport.py` for a runnable wrapper-style variant.

To emit `TransportDegraded` or other provider-scoped events on the session
bus, expose a synchronous `set_event_bus(event_bus)` method. Session calls it
before `connect()`. `AudioQueueMixin` already implements this hook and preserves
an explicitly configured bus, so subclasses such as `MemoryTransport` need no
additional wiring. Private `_event_bus` attachment remains a legacy fallback,
not an extension contract.

If the transport emits EasyCat events directly, also expose synchronous
`set_session_id(session_id)`. Session calls it during construction, after bus
attachment and before any transport `connect()` or `warmup()` hook can run, so
those events remain attributable when multiple sessions share one `EventBus`.
`AudioQueueMixin` implements this hook and stamps its built-in diagnostics;
custom events should copy the bound ID into their `session_id` field too.

## Verifying conformance

```python
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.testing import TransportContractSuite


class TestMemoryTransport(TransportContractSuite):
    provider_factory = MemoryTransport


async def test_memory_transport_round_trips_audio() -> None:
    transport = MemoryTransport()
    await transport.connect()
    transport.feed(AudioChunk(data=b"\x00\x00" * 160, format=PCM16_MONO_16K))
    await transport.disconnect()
    received = [chunk async for chunk in transport.receive_audio()]
    assert len(received) == 1
```

The suite verifies connection/send/disconnect semantics, repeated and
concurrent connection calls, active receiver termination, idempotent
disconnect, and idempotent playback clearing. These are public-surface checks;
the suite does not require test-only lifecycle hooks from a transport.
`isinstance(transport, Transport)` checks member names only and is not a
behavioral conformance test. The in-tree use of the same installable suite
lives in [`tests/contracts/test_transport_contracts.py`](../../tests/contracts/test_transport_contracts.py).

## Notes

- `disconnect()` must enqueue the end-of-stream sentinel
  (`_enqueue_sentinel()`), or the session's audio reader never unblocks.
- Emit `TransportDegraded` (via `self._emit_degraded(reason, detail)`)
  instead of logging when you drop frames — the journal is the single
  source of truth for observability.
- `clear_audio()` is invoked only when present; implement it if your
  transport buffers outbound audio, so barge-in feels instant.

## Optional: protected playback for in-band DTMF

`Session.play_dtmf_tones()` sends DTMF key presses as ordinary audio frames.
A barge-in `clear_audio()` during the burst would cut the key sequence on the
far end, so a transport that buffers audio far away (such as a telephony
media stream) can implement the optional `ProtectedPlayback` protocol from
`easycat.runtime.capabilities`. The router detects it with
`protected_playback(transport)`, which checks that all four hooks are
callable:

| Member | Contract |
| --- | --- |
| `async wait_playout_fence(*, timeout_s) -> bool` | Send a mark and wait until audio queued before it is no longer queued: played, or discarded by a `clear` (Twilio echoes pending marks when it clears), so `True` does not prove the audio was heard. `False` on timeout, with no stream, or when the stream changed. A `clear` during the fence is not deferred. |
| `begin_protected_playback(*, max_duration_s) -> object` | Synchronous. Arm the guard and return an opaque token. Raise `RuntimeError` with no active stream, or when the stream the preceding fence targeted is no longer active (it ended or was replaced during the fence), so tones never go out on a replacement stream. |
| `async end_protected_playback(token) -> asyncio.Future` | Send the completion mark and return a future that resolves `"played"`, `"timeout"` (the guard deadline passed), or `"disconnected"`. |
| `abort_protected_playback(token \| None) -> None` | Release one guard, or all guards and fence waiters for `None`. Unknown tokens are ignored. |

While a guard is active, `clear_audio()` defers the wire clear and holds
untagged `send_audio()` calls. The router tags every tone frame with the
token, as the `PROTECTED_PLAYBACK_CHUNK_ATTR` attribute, and tagged frames are
never held. When the completion mark is acknowledged, the transport writes the
deferred clear before it releases held audio, so audio queued after the tones
is cleared and the tones are not truncated. When the guard deadline passes
first, nothing has confirmed playback: the transport still writes the deferred
clear before it releases held audio, but that clear also discards any tones
the far end has not played yet, so some keys can go unplayed and the outcome
is `"timeout"`, not `"played"`. Stream teardown
must abort the guards for that stream so a held send never reaches a
replacement stream, and must reject later frames tagged with an aborted
guard's token so the rest of a cut key sequence is never sent.
`TwilioTransport` and `TwilioConnectionTransport` are the in-tree
implementations. A transport without the capability still plays the tones; a
barge-in during the burst can then cut them short.

Tone frames are 20 ms PCM16 frames in the transport's
`preferred_tts_output_format`, else its `audio_format`, else 8 kHz mono, so
expose one of those when `send_audio()` ignores `chunk.format`. Tone frames
carry `AudioChunk._easycat_dtmf_tone=True`. A transport that reports delivery
(`reports_audio_delivery = True`) and builds new chunks for
`TransportAudioDelivered` must copy that field, as it copies the session and
turn stamps, so the router keeps tones out of turn accounting and masks them
under `journal_redaction="pii"`. The last frame of each burst also carries
`AudioChunk._easycat_dtmf_burst_end=True`; a transport that buffers audio
until a pre-roll threshold may start playout on that frame even below its
pre-roll, so a short burst is not left waiting for audio that never comes.
