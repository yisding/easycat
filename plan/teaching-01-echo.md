# Chapter 1 — Echo

> Mic to speaker, continuously, through the `Transport` protocol.
> First encounter with async audio streams.

## Prerequisites

- Chapter 0
- Comfort with `async`/`await` and async generators

## Learning objectives

1. Read and use a `typing.Protocol` with duck-typed implementations
   — the design pattern EasyCat uses for every provider.
2. Write and consume an `async for` loop over a chunked audio
   stream.
3. Articulate why EasyCat's Transport is push (yield chunks) rather
   than pull (callback).

## What you build

`docs/teaching/01-echo/main.py`:

- Uses the existing local Transport from `src/easycat/transports/`.
- A ~20-line async loop that reads mic chunks and writes them to
  speakers.
- Runs until Ctrl-C.

Starting point: a copy of `docs/teaching/00-hello-audio/main.py`,
which the reader evolves into the async version. This is the first
chapter where the copy-forward convention kicks in.

## Narrative arc

1. **Re-read Chapter 0's play loop.** It was blocking. In a real
   system, we need to do other things while audio flows — detect
   speech, talk to APIs, play back synthesized audio.
2. **`async for` in one paragraph.** "A loop that can yield control
   while waiting for the next chunk." That's enough context.
3. **The Transport protocol.** Walk through `src/easycat/providers.py`'s
   `Transport` definition. Two directions: inbound audio frames,
   outbound audio bytes. No inheritance — just shape matching.
4. **Echo implementation.** Read from inbound, push straight to
   outbound. Maybe 10 lines of real logic.
5. **Why no `Session` yet?** We are writing the consumer end of
   Transport; the rest of the pipeline isn't needed to demonstrate
   the concept. Keep the scope tight.

## Key concepts

- `src/easycat/providers.py::Transport` protocol
- `async for chunk in transport.recv_audio():`
- `await transport.send_audio(chunk)`
- Duck-typed Protocol over inheritance — a theme that recurs in
  every provider surface
- **Not** introduced: sample-rate conversion, VAD, turn management,
  the full Session — all deferred to later chapters

## The one trap to mention

Sample-rate mismatches are a real bug (mic captures at 48 kHz,
pipeline expects 16 kHz). Mention it, show what it sounds like
if the reader forces a mismatch, but don't solve it here — the
Transport handles resampling internally. This plants a seed for
chapter 11 when we swap transports.

## Exercises

1. Make a **delay echo**: buffer 500ms before forwarding. Why does
   that create the sensation of an echo rather than just "a delay"?
2. Print `len(chunk)` for each chunk. Are chunks always the same
   size? Why not?
3. Run the script while playing music. Is feedback an issue? Why or
   why not? (Hint: speaker → mic loop; mitigated later by VAD + NR.)

## Journal highlights

None yet — the journal is introduced in chapter 2 once there's
state worth tracking.

## Files created

- `docs/teaching/01-echo/main.py`
- `docs/teaching/01-echo/README.md`

## Success criteria

- The reader can trace a single mic sample from OS input →
  Transport → their own code → Transport → OS output, naming each
  hop.
- The reader can define a `Protocol` and implement it with a class
  that does not inherit from it.

## Links forward

Chapter 2 keeps the inbound audio stream but routes it to an STT
provider instead of back to speakers.
