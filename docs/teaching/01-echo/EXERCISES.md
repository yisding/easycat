# Chapter 1 — Exercises

<!-- BEGIN auto:navigation -->
[← Back to chapter](./README.md) · [Ladder index](../) · [Progress worksheet](../PROGRESS.md) · [Chapter 2 — Transcribe →](../02-transcribe/)
<!-- END auto:navigation -->

<!-- BEGIN auto:exercise-protocol -->
> **Completion evidence for every task**
>
> 1. **Before hints:** keep your initial prediction or plan.
> 2. **After the attempt:** keep the exact command or change and one observed field,
>    measurement, or behavior.
> 3. **Before moving on:** explain in one sentence why the evidence supports or changes
>    your model.
>
> A task is complete when all three are present. Keep a wrong first answer visible;
> it is evidence to explain after revealing hints, not an answer to rewrite.
<!-- END auto:exercise-protocol -->

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
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 3</summary>

There are two paths from your mouth to your brain: through
   air-to-ear (instant) and through skull-to-cochlea (also
   instant). Both reach you before the loop.

</details>

<details markdown="1">
<summary>Hint 2 of 3</summary>

The delayed copy reaches your *ears* (the speaker) 500 ms after
   the original. Two arrivals from the same sound at different
   times is the definition of an acoustic echo.

</details>

<details markdown="1">
<summary>Hint 3 of 3</summary>

If you played the delayed copy *into your skull* directly, it
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
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 3</summary>

`False` means the chunk was not fully accepted for delivery. It does not
   mean the coroutine failed or raised.

</details>

<details markdown="1">
<summary>Hint 2 of 3</summary>

`True` means accepted, not heard. A transport queue, network, jitter buffer,
   device buffer, and speaker can still sit downstream.

</details>

<details markdown="1">
<summary>Hint 3 of 3</summary>

The four connection/audio methods satisfy `TransportLike`; the full
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

<!-- BEGIN auto:self-check-protocol -->
> **Closed-book retrieval gate**
>
> 1. Close the chapter narrative and every hint disclosure.
> 2. Answer every numbered question below from memory, aloud or in writing.
> 3. Support each answer with at least one observed field, measurement, or behavior
>    from your attempt record.
> 4. Mark each answer **pass** or **retry** in your progress record.
>
> If an answer needs notes, reopen only the section that owns the weak concept,
> correct your explanation, close it, and retry. Continue only when every answer
> passes without looking.
<!-- END auto:self-check-protocol -->

1. What inbound and outbound audio shape does the echo loop require, and which
   probe result confirms that contract?
2. What evidence distinguishes a produced chunk from one accepted by
   `send_audio(...)`, and why does the loop consume inbound audio with async
   iteration?
3. Why can the same control flow use Local, WebSocket, WebRTC, or Twilio
   transports, and which boundary owns their differences?

<!-- BEGIN auto:exercise-completion -->
---
Self-check complete? Prepare the cumulative spine, then replay it through this chapter:

```bash
uv sync --extra local --group dev
uv run python docs/teaching/offline_spine.py --run --through 1 --jobs 4 --show-evidence
```

- [Review the chapter narrative](./README.md)
- [Update the progress worksheet](../PROGRESS.md)
- [Continue to Chapter 2 — Transcribe →](../02-transcribe/)
<!-- END auto:exercise-completion -->
