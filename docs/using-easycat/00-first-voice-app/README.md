# Chapter 0 — Your First VoiceApp

> Wrap an agent in `VoiceApp`, let EasyCat wire the voice pipeline, and talk to
> it over your microphone and speakers.

This ladder begins at EasyCat's product-level surface. You provide the agent's
behavior and choose where it runs; EasyCat assembles the default STT, TTS, VAD,
transport, session, and lifecycle around it.

## Prerequisites

- Python 3.11+.
- `uv sync --extra quickstart --group dev` from the repository root.
- `OPENAI_API_KEY`. The quickstart extra supplies the OpenAI provider and
  OpenAI Agents SDK integrations used by this chapter.
- A working microphone and speakers.
- Run `uv run easycat doctor` after exporting the key. If it lives in `.env`,
  run `uv run easycat doctor --env-file .env`. Use
  `uv run easycat doctor --json` or
  `uv run easycat doctor --env-file .env --json` for parseable checks. When
  running the chapter with that file, add `--env-file .env` after `uv run`.

## Run it

```bash
uv run python docs/using-easycat/00-first-voice-app/main.py
```

With a project `.env`:

```bash
uv run --env-file .env python docs/using-easycat/00-first-voice-app/main.py
```

Speak after the session starts. Stop the process with <kbd>Ctrl</kbd>+<kbd>C</kbd>.

## The whole app

The important code is deliberately small:

```python
agent = Agent(
    name="feature-guide",
    instructions="Answer in one or two friendly sentences.",
)
app = VoiceApp(agent=agent)
app.run("local")
```

There are three decisions here:

1. `Agent(...)` defines the assistant's behavior. It remains a normal OpenAI
   Agents SDK object; tools and instructions still belong to your agent.
2. `VoiceApp(agent=agent)` defines the voice product. EasyCat recognizes the
   agent specification and chooses its bridge automatically.
3. `app.run("local")` chooses the runtime mode. The same app will move to a
   browser, WebSocket clients, and phone calls in the next chapter.

```text
microphone -> EasyCat input / turns / STT -> your Agent
 speakers  <- EasyCat output / TTS        <- response
```

Because no `stt`, `tts`, or `vad` override is supplied, EasyCat resolves its
documented defaults from `OPENAI_API_KEY`. That is useful at the beginning:
the first chapter is about the app boundary, not provider assembly. Chapter 2
will make those choices explicit.

## What VoiceApp owns

`VoiceApp` is a thin product-level orchestrator. It does not replace your
agent framework, and it does not hide the lower-level API permanently. It
selects an `EasyConfig` preset for the runtime mode, builds a fresh session,
and drives that session with the correct transport.

That division gives you a simple rule:

- Put **conversation behavior**—instructions, model choices, tools, and agent
  handoffs—on your agent or workflow.
- Put **voice runtime choices**—mode, STT, TTS, VAD, and debug capture—on
  EasyCat.

Later chapters graduate to `EasyConfig` and `Session` when an application
needs events, text turns, explicit lifecycle control, or per-connection
factories. Starting with `VoiceApp` keeps those details out of the way until
they buy you something.

## Make one change

Edit the agent's `instructions` in `main.py`. Ask for a different persona,
shorter answers, or a fixed opening phrase, then run the chapter again. If the
spoken behavior changes without any pipeline edits, you have located the
boundary between your agent and EasyCat.

Continue with [the exercises](./EXERCISES.md) to make that boundary concrete.

## What you should be able to answer now

> Which object owns the assistant's tools and instructions?

Your agent or workflow does.

> Which object chooses how that agent becomes a running voice application?

`VoiceApp` does; its runtime mode selects the transport-facing preset.

## What's next

Chapter 1 will keep this same app and switch its runtime mode across local,
browser, and WebSocket surfaces, then explain why phone and multi-client modes
use a fresh configuration per connection.
