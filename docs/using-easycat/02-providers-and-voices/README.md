# Chapter 2 — Pick Providers and Voices

> Choose STT and TTS independently, use shortcut specs for common cases, and
> drop to typed provider configs when you need a voice-specific option.

The first two chapters relied on EasyCat's OpenAI defaults. This chapter keeps
the app in `local` mode and varies only the speech providers, so you can hear
which configuration choice changed the experience.

## Prerequisites

- Python 3.11+.
- `uv sync --extra quickstart --extra deepgram --extra elevenlabs --group dev`
  from the repository root.
- `OPENAI_API_KEY` for the example agent and the `openai` / `deepgram-stt`
  profiles' TTS.
- `DEEPGRAM_API_KEY` for the `deepgram-stt` and `elevenlabs-voice` profiles.
- `ELEVENLABS_API_KEY` for the `elevenlabs-voice` profile.
- A microphone and speakers.
- Run `uv run easycat doctor` after exporting the keys. Target one provider
  while configuring it with `uv run easycat doctor --provider deepgram` or
  `uv run easycat doctor --provider elevenlabs`. If keys live in `.env`, run
  `uv run easycat doctor --env-file .env`; use `uv run easycat doctor --json`
  or `uv run easycat doctor --env-file .env --json` for parseable checks.
  When running a chapter command, add `--env-file .env` after `uv run`.

The offline `list` command below needs no credentials and no audio hardware.

## Run it

First ask EasyCat's registries which provider names are registered:

```bash
uv run python docs/using-easycat/02-providers-and-voices/main.py list
```

Then run one speech profile:

```bash
uv run python docs/using-easycat/02-providers-and-voices/main.py openai --voice alloy
uv run python docs/using-easycat/02-providers-and-voices/main.py deepgram-stt --voice nova
uv run python docs/using-easycat/02-providers-and-voices/main.py elevenlabs-voice
```

With keys in `.env`:

```bash
uv run --env-file .env python docs/using-easycat/02-providers-and-voices/main.py deepgram-stt
```

The profiles are intentionally small:

| Profile | STT | TTS | What it isolates |
|---|---|---|---|
| `openai` | `openai-realtime` | OpenAI, selected `voice` | A typed voice override on the default provider pair |
| `deepgram-stt` | `deepgram/nova-2` | OpenAI, selected `voice` | An STT-only swap |
| `elevenlabs-voice` | `deepgram/nova-2` | ElevenLabs Flash, selected `voice_id` | A TTS provider and voice-ID swap |

## Shortcut specs: provider, then model

The common selection syntax is a string:

```python
VoiceApp(agent=agent, stt="deepgram/nova-2", tts="openai")
```

The part before `/` is a registered provider name. The optional part after it
is the model. A bare provider name uses that provider config's current default
model.

When `VoiceApp` resolves the selected `EasyConfig` preset, the provider catalog
does three things:

1. validates the provider name and suggests close matches for typos;
2. reads the provider's registered credential environment variable; and
3. creates the concrete config dataclass with the requested model.

STT and TTS have separate registries, so they can be mixed independently. A
provider can appear in both registries without requiring you to use it for both
roles.

EasyCat validates the provider name locally. Model IDs are provider-owned and
can change independently, so a syntactically valid `provider/model` pair may
still be rejected when the remote provider receives it. Pin models you have
tested, and run a live validation before shipping a changed model.

## Typed configs: provider-specific controls

Shortcut strings deliberately expose only provider and model. Voices, speed,
language, stability, and other provider-specific settings belong to the
provider's typed config:

```python
from easycat.tts.openai_tts import OpenAITTSConfig

tts = OpenAITTSConfig(
    api_key=require_env("OPENAI_API_KEY"),
    model="gpt-4o-mini-tts",
    voice="nova",
    speed=1.0,
)
app = VoiceApp(agent=agent, stt="deepgram/nova-2", tts=tts)
```

Typed configs take precedence over shortcut parsing. Unlike a string shortcut,
the config is already constructed, so provide its credential explicitly—this
lesson uses `require_env(...)` and never hard-codes or prints a secret.

The ElevenLabs profile uses the same pattern, with names that match its API:

```python
ElevenLabsTTSConfig(
    api_key=require_env("ELEVENLABS_API_KEY"),
    model_id="eleven_flash_v2_5",
    voice_id="EXAVITQu4vr4xnSDxMaL",
)
```

Notice `voice` versus `voice_id`, and `model` versus `model_id`. Typed configs
make those provider differences visible to type checkers and editors instead
of hiding them in a free-form settings dictionary.

## Discover before you configure

The `list` profile calls two public functions:

```python
available_stt_providers()
available_tts_providers()
```

They return the built-in providers plus third-party providers discovered
through EasyCat's extension entry points. Use the returned names for the part
before `/`; do not maintain a second hard-coded provider list in your app.

Then use `easycat doctor --provider NAME` to check that provider's registered
credential and reachability. `doctor` checks readiness; it does not make the
billed STT/TTS request needed to judge transcription accuracy, voice quality,
or end-to-end latency.

Today `available_stt_providers()` returns `cartesia`, `deepgram`,
`elevenlabs`, `openai`, and `openai-realtime`, and
`available_tts_providers()` returns `cartesia`, `deepgram`, `elevenlabs`, and
`openai`. The three profiles above demonstrate the mechanics with two of them;
everything you learned applies unchanged to the rest.

### Cartesia, as a worked fourth provider

Cartesia appears in both registries, so it is a good check that you can read
this chapter's rules and apply them to a provider whose profile you have not
run:

```bash
uv sync --extra quickstart --extra cartesia --group dev
export CARTESIA_API_KEY="..."
uv run easycat doctor --provider cartesia
```

```python
# Shortcut form — provider, then model.
VoiceApp(agent=agent, stt="cartesia/ink-2", tts="cartesia/sonic-3")
```

```python
# Typed form, for the provider-specific controls a shortcut cannot express.
from easycat.stt.cartesia_provider import CartesiaSTTConfig
from easycat.tts.cartesia_tts import CartesiaTTSConfig

VoiceApp(
    agent=agent,
    stt=CartesiaSTTConfig(language="en"),
    tts=CartesiaTTSConfig(voice_id="<a Cartesia voice id>", speed=1.0),
)
```

Two provider-specific details the shortcut hides, both the kind of thing the
typed config exists to surface: Cartesia's STT default is `ink-2`, which ships
**built-in semantic turn detection**, so EasyCat leaves its own smart turn off
for it (chapter 3 covers what that means); and `ink-2` is currently
English-only, so a non-English `language` resolves to the multilingual
`ink-whisper` instead. As with every provider here, model IDs are
provider-owned — pin what you have tested.

## Specs are reusable; live providers are not

The string and dataclass values in this chapter are provider *specifications*.
`VoiceApp` can use them to construct a fresh live provider for each session,
including in the server modes from chapter 1.

An object returned by `create_tts_provider(...)` or
`create_stt_provider(...)` is already live-capable: it can own sockets,
buffers, cancellation state, and an in-flight utterance. Do not put one shared
live instance on a multi-client `VoiceApp`. Build it inside a per-connection
factory when you truly need to hand-construct providers.

## Audio alignment stays automatic

The providers in this chapter can emit different native sample rates. By
default, `EasyConfig` aligns TTS output to the selected transport, so swapping
OpenAI and ElevenLabs does not require you to add a resampler to this app.
Keep `auto_align_tts_output_to_transport=True` unless you intentionally own the
format boundary yourself.

Continue with [the exercises](./EXERCISES.md) to separate provider, model, and
voice changes by ear.

## What you should be able to answer now

> When is a `provider/model` string enough?

When provider and model are the only choices you need to make.

> When should you use a typed provider config?

When you need provider-specific fields such as voice, speed, language, or
stability.

> Does changing STT require changing TTS?

No. The two roles resolve independently.

## What's next

Chapter 3 keeps the provider pair fixed and shapes conversation timing with
VAD, smart turn, interruption, push-to-talk, noise reduction, and echo
cancellation.
