# Chapter 3 — Shape the Conversation

> Tune when turns begin and end, make barge-in feel natural, and decide how
> much input cleanup happens before speech reaches transcription.

The first three chapters fit inside `VoiceApp`'s high-level fields. Conversation
timing and signal processing use `EasyConfig`'s pipeline fields, so this chapter
builds an `EasyConfig.mic(...)` and hands it back to `VoiceApp`:

```python
config = EasyConfig.mic(
    agent=agent,
    smart_turn=True,
    enable_noise_reduction=True,
    turn_taking=TurnManagerConfig(...),
)
VoiceApp(config=config).run("local")
```

This is still the app-first lifecycle. You are choosing more of the pipeline,
not starting/stopping a `Session` yourself.

## Prerequisites

- Python 3.11+.
- `uv sync --extra quickstart --group dev` from the repository root. The
  quickstart extra includes the smart-turn ONNX runtime and LiveKit AEC used
  by the profiles below. Add `--extra rnnoise` when running the `clean`
  profile; the default-off backend stays opt-in to keep first-run installs
  lean.
- `OPENAI_API_KEY` for the default OpenAI STT, TTS, and example agent.
- A microphone and speakers. Use headphones for the `raw` profile so the bot's
  own speech does not feed back into the microphone at full volume.
- Run `uv run easycat doctor` after exporting the key. If it lives in `.env`,
  run `uv run easycat doctor --env-file .env`. Use
  `uv run easycat doctor --json` or
  `uv run easycat doctor --env-file .env --json` for parseable checks. When
  running a chapter command, add `--env-file .env` after `uv run`.

## Run it

Start with the transport-aware defaults, then change one policy profile:

```bash
uv run python docs/using-easycat/03-conversation-controls/main.py balanced
uv run python docs/using-easycat/03-conversation-controls/main.py vad-only
uv run python docs/using-easycat/03-conversation-controls/main.py fast
uv run python docs/using-easycat/03-conversation-controls/main.py clean
uv run python docs/using-easycat/03-conversation-controls/main.py raw
```

Push-to-talk uses a separate input loop because the application—not VAD—owns
the start/end button:

```bash
uv run python docs/using-easycat/03-conversation-controls/push_to_talk.py
```

With a project `.env`:

```bash
uv run --env-file .env python docs/using-easycat/03-conversation-controls/main.py fast
```

## The profiles

| Profile | Smart turn | AEC | Noise reduction | End-of-turn posture |
|---|---|---|---|---|
| `balanced` | Transport default | Transport default | Off | EasyCat's local-mic defaults |
| `vad-only` | Off | On | Off | VAD plus 700ms silence grace |
| `fast` | On, sensitivity `0.7` | On | Off | Semantic completion plus 400ms fallback |
| `clean` | On, sensitivity `0.6` | On | On | Clean input before turn decisions |
| `raw` | Off | Off | Off | Unprocessed comparison; headphones recommended |

The values are experiments, not universal production recommendations. Room
acoustics, microphone distance, language, STT model, and client transport all
change the best balance. Keep a journal and measure real calls before pinning
them.

## The input order

For the local pipeline, the important order is:

```text
microphone -> echo cancellation -> noise reduction -> VAD -> STT
                    ^                                  |
                    |                                  v
             speaker reference                 turn manager
```

- **Echo cancellation (AEC)** removes the bot audio that the microphone hears
  from the speakers. It needs the far-end speaker signal as a reference.
- **Noise reduction (NR)** suppresses steady background noise after echo is
  removed.
- **VAD** sees the cleaned signal and marks speech starts/pauses.
- **STT** turns the accepted user audio into partial/final text.
- The **turn manager** combines VAD, transcript punctuation, silence budgets,
  and optional smart-turn decisions into a complete user turn.

Ordering matters: if VAD sees the noisy, echo-heavy signal first, it can open
false turns that cleanup can no longer undo.

## VAD starts with sound

`TurnManagerConfig` controls the state machine around VAD:

```python
TurnManagerConfig(
    end_of_turn_silence_ms=700,
    punctuated_end_of_turn_silence_ms=250,
    pre_roll_ms=450,
)
```

`pre_roll_ms` keeps a short rolling buffer from before the VAD start event, so
the consonant that triggered speech detection is not clipped. Keep it at least
150 ms above `VADConfig.min_speech_duration_ms`; `EasyConfig` warns when those
typed configs drift below that margin. Silence values are fallback timing:
punctuation can shorten a pause, while an incomplete smart-turn decision can
retain the full grace period.

Do not chase low latency by setting every silence value to zero. That makes a
fast demo by cutting people off mid-thought.

### Choosing a VAD backend

`VADConfig(backend=...)` selects *which* detector runs. It defaults to
`"auto"`, which tries Silero → FunASR → TEN → Krisp and uses the first one
installed, so the profiles above need no backend at all. Name one explicitly
when you need it pinned:

```python
VADConfig(backend="silero")  # or "funasr", "ten", "krisp"
```

The difference between `"auto"` and a named backend is failure behaviour, and
that is the reason to care: `"auto"` walks the chain and settles for whatever
is present, while a named backend **fails loudly** when it is not installed.
Pin one in production so a missing extra is a startup error rather than a
silently different detector. Silero ships with the `quickstart` extra; the
others need `--extra funasr-vad`, `--extra ten-vad`, or a Krisp install (see
[the install guide](../../install.md)).

## Smart turn asks whether the thought is complete

VAD answers “did sound stop?” Smart turn answers “does the trailing audio sound
like a complete turn?” It runs at a pause; it does not replace speech
detection.

The beginner-facing `smart_turn_sensitivity` is 0–1. Higher sensitivity treats
a lower semantic-completion probability as enough to end the turn, which feels
faster but risks premature endpoints. The `fast` profile pairs `0.7` with a
400ms silence fallback so an incomplete/error decision still has a bounded
path forward.

EasyCat enables smart turn by default for the local-mic preset unless the STT
provider owns native endpointing. Server transports default it off. An explicit
`smart_turn=True` or `False` always wins.

## Interruption is a state transition, not a second mode

While the bot is speaking, a real VAD speech-start becomes barge-in. EasyCat
cancels the active agent/TTS work, stops queued playback, emits an
`Interruption` event, and updates compatible agent history to reflect what the
listener likely heard.

Try it with `balanced` or `clean`: ask for a long answer, then speak while the
answer is playing. On speakerphone, AEC is what helps VAD distinguish your new
speech from the bot's own output.

The default history policy truncates the assistant message to the estimated
heard portion. The lower-level session config also supports an explicit
interruption message for agent bridges that accept interleaved system/developer
messages; the compatible truncate behavior remains the app default.

## Push-to-talk gives ownership to the UI

In `TurnMode.PUSH_TO_TALK`, VAD does not decide when turns start or end. The
application calls `session.start_turn()` and `session.end_turn()` from a button,
hotkey, GPIO pin, or client control message.

`push_to_talk.py` uses EasyCat's stdin adapter: press Enter once to start
capturing, speak, and press Enter again to end the turn. It has to construct the
`Session` because the UI callback needs those two session methods directly.
That is a feature-specific reason to move below `VoiceApp.run`, not a reason to
hand-wire providers.

Push-to-talk is useful in noisy rooms and radio-style interfaces, but it moves
timing burden onto the user. Smart turn is disabled in that profile because the
button is already the authoritative endpoint.

## Cleanup is not free

NR and AEC add dependencies, CPU work, and more state to debug. The local and
browser presets enable AEC where loopback is likely; telephony leaves it off by
default because PSTN media has no local speaker loop. Noise reduction remains
opt-in.

Use `raw` only as a controlled comparison. If `clean` improves false VAD starts
or transcript quality, keep it and verify latency on the hardware you will
ship. If it does not, the simpler pipeline is easier to operate.

### Choosing a noise-reduction backend

`enable_noise_reduction=True` turns cleanup on with defaults. The
`noise_reduction=` field selects the backend, the same shape as `vad=`:

```python
from easycat.noise_reduction import NoiseReducerConfig

EasyConfig(
    agent=agent,
    noise_reduction=NoiseReducerConfig(backend="rnnoise", fallback_policy="error"),
)
```

`backend="auto"` (the default) tries Krisp, then RNNoise, then falls back to a
**passthrough** reducer that returns audio unchanged. That fallback is the trap
worth knowing about: with the default `fallback_policy="passthrough"` a
missing extra produces a warning and a pipeline that silently does no
cleaning, so a deployment can look configured while doing nothing. Set
`fallback_policy="error"` — or name a backend, which also fails loudly when it
is missing — to make that a startup failure instead. RNNoise needs
`--extra rnnoise`; Krisp is licensed separately.

Echo cancellation follows the same pattern via `echo_cancellation=` and
`EchoCancellationConfig`, with its own `fallback_policy`.

## Two more turn-shaped knobs

`greeting=` speaks one line as soon as the session starts, before the caller
says anything:

```python
EasyConfig(agent=agent, greeting="Hi, you've reached support. How can I help?")
```

That matters for turn-taking, which is why it belongs in this chapter: the
greeting is bot speech, so the interruption rules above apply to it — a caller
who talks over the greeting barges in exactly as they would over any other
reply. On a phone call a greeting is close to mandatory; a caller who hears
silence assumes the line is dead.

`timeouts=` bounds how long each slow stage may take before the turn is
abandoned:

```python
from easycat.timeouts import TimeoutConfig

EasyConfig(agent=agent, timeouts=TimeoutConfig(agent_timeout=15.0))
```

The defaults are 10s for STT, 30s for the agent, and 5s until TTS's first audio
byte. These are *deadlines*, not the silence timers above: `end_of_turn_silence_ms`
decides when the caller has finished speaking, while `agent_timeout` decides
when to give up waiting for a reply. Pair a tightened `agent_timeout` with
`on_agent_failure` (chapter 4) so the caller hears something when it fires.
Every field is in the
[EasyConfig field reference](../../reference/easyconfig.md).

Continue with [the exercises](./EXERCISES.md) to compare timing and barge-in
without changing providers or agent instructions.

## What you should be able to answer now

> Does smart turn replace VAD?

No. VAD finds speech/pause boundaries; smart turn judges completeness at a
pause.

> Why is push-to-talk a separate script?

The input adapter must call `Session.start_turn()` and `Session.end_turn()`.

> Why can AEC improve interruption behavior?

It removes the bot's speaker audio before VAD decides whether new user speech
has begun.

## What's next

Chapter 4 adds agent tools, tool events, session actions, and pronunciation
rules while keeping this conversation pipeline intact.
