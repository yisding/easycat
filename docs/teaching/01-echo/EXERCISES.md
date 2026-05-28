# Chapter 1 — Exercises

## 1. Insert a 500 ms delay line

**Task.** Buffer chunks for 500 ms before forwarding them:

```python
buffer = []
async for chunk in transport.receive_audio():
    buffer.append(chunk)
    if sum(c.duration_ms for c in buffer) >= 500:
        old = buffer.pop(0)
        await transport.send_audio(old)
```

Now you have a delay line. Why does that create the sensation of
an *echo* rather than just "a delay"?

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

## Bonus — what if you bypass the protocol entirely?

Forget `Transport` for a minute. Write the same echo in pure
`sounddevice` callbacks. Compare line count, error handling, and
how you'd add *one more downstream consumer* (like STT). The
contrast is the whole pedagogical point of choosing the Protocol
shape.
