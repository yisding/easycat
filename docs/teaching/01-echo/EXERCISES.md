# Chapter 1 — Exercises

<!-- BEGIN auto:navigation -->
[← Back to chapter](./README.md) · [Ladder index](../) · [Chapter 2 — Transcribe →](../02-transcribe/)
<!-- END auto:navigation -->

## 1. Insert a 500 ms delay line

**Task.** Buffer chunks for 500 ms before forwarding them:

```python
buffer = []
async for chunk in transport.receive_audio():
    buffer.append(chunk)
    if sum(c.duration_ms for c in buffer) >= 500:
        old = buffer.pop(0)
        accepted = await transport.send_audio(old)
        if not accepted:
            print("delayed chunk rejected")
```

Now you have a delay line. Why does that create the sensation of
an *echo* rather than just "a delay"?

<!-- BEGIN auto:exercise-hints -->
<details markdown="1">
<summary>Reveal hints after your first attempt</summary>

**Hints**

1. There are two paths from your mouth to your brain: through
   air-to-ear (instant) and through skull-to-cochlea (also
   instant). Both reach you before the loop.
2. The delayed copy reaches your *ears* (the speaker) 500 ms after
   the original. Two arrivals from the same sound at different
   times is the definition of an acoustic echo.
3. If you played the delayed copy *into your skull* directly, it
   wouldn't feel like an echo — it would feel like a delay.

**Wider points to check yourself on**

- What's the minimum delay that makes the brain register a second
  arrival as a distinct echo (vs. just reverb)? (~50 ms is the
  rough psycho-acoustic line.)
- Why does this matter for chapter 10? Speakerphones radiate the
  TTS audio back to the mic with a similar delay. The bot ends up
  hearing itself in just the same way you hear your delayed voice.

</details>
<!-- END auto:exercise-hints -->

## 2. Make rejection observable

**Task.** Run the provider-free contract probe:

```bash
uv run python docs/teaching/01-echo/transport_contract_probe.py
```

Change its acceptance sequence to reject the first and third chunks. Predict
the two counters before rerunning it. Then remove `version_info()` from
`ScriptedTransport` and predict which structural checks change.

<!-- BEGIN auto:exercise-hints -->
<details markdown="1">
<summary>Reveal hints after your first attempt</summary>

**Hints**

1. `False` means the chunk was not fully accepted for delivery. It does not
   mean the coroutine failed or raised.
2. `True` means accepted, not heard. A transport queue, network, jitter buffer,
   device buffer, and speaker can still sit downstream.
3. The four connection/audio methods satisfy `TransportLike`; the full
   `Transport` also inherits `version_info()` for journaled provider metadata.

</details>
<!-- END auto:exercise-hints -->

## Bonus — what if you bypass the protocol entirely?

Forget `Transport` for a minute. Write the same echo in pure
`sounddevice` callbacks. Compare line count, error handling, and
how you'd add *one more downstream consumer* (like STT). The
contrast is the whole pedagogical point of choosing the Protocol
shape.

## Self-check

You should be able to: (a) state and trace the inbound/outbound audio shape
from microphone input to speaker output, (b) distinguish a chunk being
produced from `send_audio(...)` accepting it and explain why the loop uses
async iteration, and (c) explain why the same control flow works with Local,
WebSocket, WebRTC, or Twilio transports.

<!-- BEGIN auto:exercise-completion -->
---
Self-check complete? Prepare the cumulative spine, then replay it through this chapter:

```bash
uv sync --extra local --group dev
uv run python docs/teaching/offline_spine.py --run --through 1 --jobs 4 --show-evidence
```

- [Review the chapter narrative](./README.md)
- [Continue to Chapter 2 — Transcribe →](../02-transcribe/)
<!-- END auto:exercise-completion -->
