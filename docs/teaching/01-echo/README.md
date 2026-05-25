# Chapter 1 — Echo

> Mic to speaker, continuously, through the `Transport` protocol.
> First encounter with EasyCat and with async audio streams.

## Prerequisites

- [Chapter 0](../00-hello-audio/)
- `uv sync --extra quickstart --group dev`
- A mic and speakers. **Use headphones, or put the mic far from
  the speaker** — otherwise you will get a feedback loop the
  instant you press play.

> **Minimum to skip the ladder:** chapter 0 alone. This chapter
> assumes you can read raw PCM and nothing more.

## Diff from chapter 0

- **Added:** the `Transport` protocol (`src/easycat/providers.py`);
  `LocalTransport` driving the mic + speaker as async streams; the
  first `async for chunk in stream:` loop.
- **Removed:** chapter 0's synchronous `sd.rec` / `sd.play`.
  PortAudio now lives behind `LocalTransport`.

## Run it

```bash
uv run python docs/teaching/01-echo/main.py
```

Talk to your computer. Hear yourself, delayed by a few frames.
Ctrl-C to stop.

## The whole script

```python
async def echo(transport):
    async for chunk in transport.receive_audio():
        await transport.send_audio(chunk)
```

Three lines of actual logic. That's the point of this chapter.
The rest is the setup that gets you to "three lines."

## The Transport protocol

`Transport` is the first of EasyCat's provider protocols you will
meet. It lives at `src/easycat/providers.py`. In stripped-down
form:

```python
@runtime_checkable
class Transport(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def receive_audio(self) -> AsyncIterator[AudioChunk]: ...
    async def send_audio(self, chunk: AudioChunk) -> None: ...
```

Four methods. Any class that provides those four — with compatible
signatures — *is* a `Transport`. No inheritance, no registration,
no base class to inherit from. This is `typing.Protocol` doing
structural typing: "duck typing, but the type checker verifies."

`LocalTransport` (mic + speaker via PortAudio), `TwilioTransport`
(telephony), `WebRTCTransport` (browser), `WebSocketTransport`
(custom clients) all satisfy the same protocol. Your `echo`
function doesn't care which one it got. Chapter 13 will swap them
and you will not touch `echo` to do it.

## Why async, not callbacks

A callback API would look like:

```python
transport.on_audio(lambda chunk: transport.send_audio(chunk))
```

That works for echo. It starts falling over the instant you want
to "wait for STT to return, then send the transcript to an LLM,
then stream the response to TTS, all while still receiving mic
audio." Callbacks and `await` don't compose nicely; callbacks and
`async for` do. Every downstream chapter depends on being able to
write `async for chunk in stream:` — hence the choice at this layer.

## Architecture diagram

```
 ┌─────────┐   receive_audio()    ┌────────┐    send_audio()    ┌─────────┐
 │   Mic   │ ───────────────────► │  echo  │ ─────────────────► │ Speaker │
 └─────────┘   AudioChunks        └────────┘    AudioChunks     └─────────┘
```

## Pocket note

`LocalTransport` handles its own sample-rate choice internally
(24 kHz mono by default). Keep the phrase "sample-rate mismatch"
in a pocket for chapter 13 — it's what goes wrong when you start
swapping transports.

## Try breaking it

Insert a 500ms buffer before forwarding:

```python
buffer = []
async for chunk in transport.receive_audio():
    buffer.append(chunk)
    if sum(c.duration_ms for c in buffer) >= 500:
        old = buffer.pop(0)
        await transport.send_audio(old)
```

Now you have a delay line. Why does that create the sensation of
an *echo* rather than just "a delay"? (Hint: your brain is
comparing direct sound reaching your skull with delayed sound
reaching your ears.)

## What's next

[Chapter 2 — Transcribe](../02-transcribe/) keeps the inbound
stream but sends it to an STT provider instead of back to the
speaker. First journal, first taste of batch-vs-streaming latency.
