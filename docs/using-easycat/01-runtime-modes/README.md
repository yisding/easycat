# Chapter 1 — Run It Anywhere

> Keep one voice product and choose whether it runs on your machine, in a
> browser, behind a WebSocket, or on a phone call.

Chapter 0 ended with one explicit deployment choice:

```python
app.run("local")
```

This chapter turns that string into a command-line argument. The agent and
voice configuration stay the same while `VoiceApp` selects the matching
transport, server, and session ownership model.

## Prerequisites

- Python 3.11+.
- `uv sync --extra quickstart --extra webrtc --extra telephony --group dev`
  from the repository root. `quickstart` covers local mode and the example
  agent, `webrtc` adds browser mode, and `telephony` adds Twilio mode.
- `OPENAI_API_KEY` for the default OpenAI STT, TTS, and example agent.
- Run `uv run easycat doctor` after exporting the key. If keys live in `.env`,
  run `uv run easycat doctor --env-file .env`. Use
  `uv run easycat doctor --json` or
  `uv run easycat doctor --env-file .env --json` for parseable checks. When
  running the chapter with that file, add `--env-file .env` after `uv run`.
- Twilio mode also needs `TWILIO_STREAM_URL` and `TWILIO_AUTH_TOKEN`. The
  stream URL must be a public `wss://` endpoint that reaches the media listener;
  the auth token validates Twilio's signed `/twiml` webhook.

You can run the local, browser, and WebSocket modes without configuring
Twilio.

## Run it

Choose one mode:

```bash
uv run python docs/using-easycat/01-runtime-modes/main.py local
uv run python docs/using-easycat/01-runtime-modes/main.py browser
uv run python docs/using-easycat/01-runtime-modes/main.py websocket
uv run python docs/using-easycat/01-runtime-modes/main.py twilio
```

With keys in a project `.env`, place the loader immediately after `uv run`:

```bash
uv run --env-file .env python docs/using-easycat/01-runtime-modes/main.py browser
```

The source change from chapter 0 is the final line: instead of fixing
`"local"`, the script passes the selected mode to the same `VoiceApp`.

## Four modes, four boundaries

| Mode | EasyCat owns | You connect with | Default listener |
|---|---|---|---|
| `local` | One mic/speaker session | The machine's audio devices | None |
| `browser` | WebRTC signaling, Opus audio, and a bundled client | The printed browser URL | `127.0.0.1:8080` |
| `websocket` | Per-client PCM/JSON sessions | Your own WebSocket client | `127.0.0.1:8765` |
| `twilio` | TwiML HTTP and media-stream listeners | A configured Twilio number | HTTP `:8000`, media `:8766` |

### Local

```bash
uv run python docs/using-easycat/01-runtime-modes/main.py local
```

This is the only single-session mode. It records from your microphone, plays
through your speakers, and stops on <kbd>Ctrl</kbd>+<kbd>C</kbd>. Later, when you
need direct lifecycle control, `app.session("local")` can return that one
unstarted session to you.

### Browser

```bash
uv run python docs/using-easycat/01-runtime-modes/main.py browser
```

Open the printed URL and click **Start**. The browser negotiates a WebRTC peer
connection, sends microphone audio as Opus, receives synthesized speech, and
uses a data channel for transcript, turn, interruption, and latency events.
The bundled page makes this the best server mode for human development.

### WebSocket

```bash
uv run python docs/using-easycat/01-runtime-modes/main.py websocket
```

This starts a headless server for application clients. Each connection gets a
fresh transport and a fresh session. Binary messages carry PCM16 audio; JSON
messages negotiate format and carry control/events. The
[browser playground guide](../../browser-playground.md) documents that wire
vocabulary. If you just want to talk from a browser, choose `browser` mode
instead.

### Twilio

```bash
uv run python docs/using-easycat/01-runtime-modes/main.py twilio
```

Twilio needs more than a one-word local switch because its media WebSocket must
be reachable from the public internet. The script reads:

- `TWILIO_STREAM_URL` — the public `wss://` URL Twilio should stream audio to.
- `TWILIO_AUTH_TOKEN` — the account secret used to validate the signed TwiML
  webhook before EasyCat mints a per-call media token.

Point the number's voice webhook at the public `/twiml` route. Chapter 10 will
add outbound calls, screening, DTMF, voicemail detection, and call control;
this rung only establishes the deployment boundary.

## One app does not mean one shared session

`local` mode builds one session. The three server modes can host concurrent
callers, so EasyCat builds a new transport, pipeline, agent bridge, and session
for every connection.

The `Agent(...)` passed in this lesson is a declarative framework
specification. EasyCat knows it can safely adapt that specification into a
fresh bridge per session. A built provider or bridge instance can hold mutable
stream or conversation state and must not be shared across callers. For those
advanced cases, construct the app with a per-connection factory:

```python
def config_factory(transport):
    return EasyConfig.browser(transport=transport, agent=make_agent())

app = VoiceApp(config_factory=config_factory)
```

Chapter 6 will build that factory and take ownership of the resulting
`Session`. For now, remember the rule: reusable *specifications* may live on
`VoiceApp`; mutable, already-built collaborators belong inside a
`config_factory`.

## `run()` versus `serve()`

`app.run(mode)` is the synchronous application entry point and owns the event
loop. Use it for a script like this chapter.

`await app.serve(mode)` performs the same job without calling `asyncio.run`.
Use it when EasyCat is one service inside an async application that already
owns the loop. Do not call `run()` from inside that loop.

## Bind safely

Browser and WebSocket modes bind to loopback by default. To listen on a public
interface, provide a serve token:

```python
app.run("browser", host="0.0.0.0", serve_token="read-from-your-secret-store")
```

EasyCat rejects a non-loopback browser or WebSocket bind without authentication
unless you explicitly opt into the unsafe override. Do not hard-code the token
in source control. Twilio uses its own signed-webhook and per-stream token
boundary rather than this browser/WebSocket bearer token.

Continue with [the exercises](./EXERCISES.md) to compare the modes and their
ownership boundaries.

## What you should be able to answer now

> Which mode gives a human a bundled client?

`browser`.

> Which modes create a fresh session per connection?

`browser`, `websocket`, and `twilio`.

> When should an async server call `serve()` instead of `run()`?

When the surrounding application already owns the event loop.

## What's next

Chapter 2 keeps the runtime mode fixed and makes the automatic provider choices
explicit: STT model, TTS provider, voice, credentials, and the preflight that
proves the requested combination is installed.
