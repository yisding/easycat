# EasyCat

Slim, batteries-included voice bot framework that runs idiomatic agents and
workflows from OpenAI Agents SDK, PydanticAI, LangChain, LangGraph,
LlamaAgents, Remote Responses API, or your own async workflow.

### Quickstart (EasyConfig)
A voice bot in three lines — `run(EasyConfig.mic(agent=...))` is the one
canonical shape, identical in this README, `examples/openai_agents_voice.py`,
and the scaffold that `easycat init my-agent` writes:

```python
from agents import Agent

from easycat import EasyConfig, run

run(
    EasyConfig.mic(
        agent=Agent(name="assistant", instructions="You are a helpful voice assistant.")
    )
)
```

By default, `run(...)` shows live console feedback only on an interactive
stderr. Use `run(config, feedback="off")` to keep a process quiet, or
`feedback="on"` to force the same first-run transcript/status output when
stderr is redirected.

> Note: `EasyConfig` will automatically wire **OpenAI Realtime STT
> (gpt-realtime-whisper) + OpenAI TTS** from `OPENAI_API_KEY` (picked up from the
> environment) when you do not override `stt` or `tts`. The Realtime STT
> streams transcription over a WebSocket as audio arrives — sub-second
> stop-to-final latency, not a batch upload at end of turn. The Realtime
> API is priced separately from `/v1/audio/transcriptions`; see OpenAI's
> pricing page. If you omit the API key, you must supply `stt` and `tts`
> configs explicitly.
>
> The underlying bridge classes live in `easycat.integrations.agents`
> (`OpenAIAgentsBridge`, `PydanticAIBridge`, `GenericWorkflowBridge`,
> `LlamaAgentsBridge`, `RemoteResponsesAPIBridge`, `LangChainBridge`,
> `LangGraphBridge`) for callers who want to construct them by hand.

## Install

Python 3.11+ is required.

EasyCat is not published to PyPI yet, so `uv add 'easycat[quickstart]'`
will work only after launch. Until then, an application should depend on a
local checkout — scaffolds from `easycat init` wire this automatically with
a `[tool.uv.sources]` block; for a hand-written `pyproject.toml`, add:

```toml
[project]
dependencies = ["easycat[quickstart]"]

[tool.uv.sources]
easycat = { path = "/path/to/easycat", editable = true }
```

For this repository, four commands go from clone to a talking bot. Keys live
in a project `.env` — the same convention `easycat init` scaffolds:

```bash
uv sync --extra quickstart --group dev
echo 'OPENAI_API_KEY=your-api-key' > .env
uv run easycat doctor --env-file .env
uv run --env-file .env python examples/openai_agents_voice.py
```

Prefer exported shell variables? `uv run easycat doctor` and
`uv run python examples/openai_agents_voice.py` work the same once
`OPENAI_API_KEY` is exported.

## Choose Your Path

| You want to | Start here | First move |
|---|---|---|
| Run a local mic/speaker voice bot | [Install](#install) | `uv sync --extra quickstart --group dev`, then `uv run easycat doctor`; for `.env` keys, use `uv run easycat doctor --env-file .env` and `uv run --env-file .env python examples/openai_agents_voice.py` |
| No mic or API key yet | [Journal demo](examples/journal_demo.py) | `uv run easycat console`, then `uv run python examples/journal_demo.py` and teaching ch. [11 journal](docs/teaching/11-journal/) and [12 evals & latency](docs/teaching/12-evals-and-latency/) |
| Learn the pipeline step by step | [Teaching ladder](docs/teaching/) | Pick a chapter from its starting-point table |
| Choose a runnable example | [Examples matrix](examples/README.md) | Use its chooser for no-key, browser, provider, or debugging examples |
| Scaffold a new app | [CLI and scaffolds](#cli) | `uv run easycat init --list-templates` before `uv run easycat init my-agent` |
| Contribute or validate a change | [Contributing](CONTRIBUTING.md) and [validation workflow](docs/validation.md) | `uv run easycat validate quick` |
| Maintain architecture, package boundaries, or coding-agent context | [Architecture map](CLAUDE.md) and [agent guide](AGENTS.md) | Review provider registries, session lifecycle, `uv run easycat docs --audience maintainers`, and `uv run easycat docs --audience coding-agents` |
| Operate or debug sessions | [Observability](docs/observability.md) and [Docker deployment](docs/deployment/docker.md) | Run `easycat bundles list`; add `uv sync --extra debugger --group dev` for the UI |

## Learn the pipeline from scratch

The 16-chapter [teaching ladder](docs/teaching/) walks the entire voice
pipeline ground-up, in the spirit of *Crafting Interpreters* and
`nanoGPT`. Each chapter is a self-contained folder with a runnable
`main.py` and a narrative `README.md`. Start at
[`docs/teaching/00-hello-audio/`](docs/teaching/00-hello-audio/) and add
one stage per chapter (echo → transcribe → VAD → blocking agent →
streaming agent → tools → smart-turn → interruption → noise/AEC →
journal → evals → swap providers → BYO agent → operate in production).

For the maintained docs map, see [`docs/README.md`](docs/README.md).

## Optional extras

The `quickstart` extra bundles local audio, OpenAI providers, OpenAI Agents
SDK, RNNoise dependencies, numpy, and onnxruntime. It does not include TEN VAD;
install that optional extra separately only if you accept its non-permissive
license. Silero VAD runs on its bundled ONNX model via `onnxruntime` (already
in `quickstart`) — no torch required. If you want a leaner install with Silero,
add extras individually:

```bash
uv sync --extra local --extra openai --extra openai-agents --extra rnnoise --extra silero-vad --group dev
```

Optional dependencies you may need depending on providers, transports, agent
frameworks, and debugging/audio-processing features:
- sounddevice + numpy (LocalTransport and local audio buffers): `uv sync --extra local --group dev`
- aiortc + aiohttp (WebRTCTransport): `uv sync --extra webrtc --group dev`
- aioquic (WebTransportTransport): `uv sync --extra webtransport --group dev`
- FastAPI + Twilio SDK (Twilio Media Streams / outbound calls): `uv sync --extra telephony --group dev`
- OpenAI Agents SDK: `uv sync --extra openai-agents --group dev`
- PydanticAI stable v1: `uv sync --extra pydantic-ai --group dev`
- PydanticAI v2 beta pin: `uv sync --extra pydantic-ai-v2-beta --group dev`
- LangChain core: `uv sync --extra langchain --group dev`
- LangGraph: `uv sync --extra langgraph --group dev`
- LlamaAgents / LlamaIndex workflows: `uv sync --extra llama-agents --group dev`
- LiveKit AEC3 echo cancellation: `uv sync --extra aec --group dev`
- aiohttp debugger UI: `uv sync --extra debugger --group dev`
- numpy + onnxruntime (Smart Turn ONNX endpoint detector): `uv sync --extra smart-turn --group dev`
- ten-vad + numpy + onnxruntime (optional TEN VAD; review its non-permissive license): `uv sync --extra ten-vad --group dev`
- numpy + onnxruntime (Silero VAD): `uv sync --extra silero-vad --group dev` — runs the bundled ONNX model (no torch required)
- numpy + onnxruntime + kaldi-native-fbank (FunASR VAD): `uv sync --extra funasr-vad --group dev`
  runs the bundled FunASR FSMN-VAD model through EasyCat's in-tree runtime.
- pyrnnoise + requests (RNNoise noise reduction backend): `uv sync --extra rnnoise --group dev`
- Krisp SDK (krisp_audio): `uv pip install krisp_audio`
- Provider extras/keys:
  `uv sync --extra openai --group dev`,
  `uv sync --extra deepgram --group dev`,
  `uv sync --extra elevenlabs --group dev`,
  or `uv sync --extra cartesia --group dev` (Deepgram, ElevenLabs, and Cartesia
  use EasyCat's core WebSocket/HTTP stack — their extras are install markers and
  add no vendor SDK).

## CLI

The commands below use the installed CLI form. From this repository, prefix
them with `uv run`, for example `uv run easycat doctor`.

```bash
easycat console          # try EasyCat in your terminal — no API keys required
easycat console --voice-demo # run one scripted no-key turn through the full audio pipeline
easycat init my-agent    # scaffold a new project from a template
easycat init --list-templates # compare templates, base package requirements, env vars, files, preflight/check/fix/docs/json-schema/run commands
easycat init --list-templates --json # emit the machine-readable template catalog
easycat doctor           # check API keys, optional extras, provider reachability
easycat doctor --json    # emit machine-readable environment checks
easycat doctor --env-file .env --json # emit checks with project .env loaded
easycat serve            # serve the browser voice playground on localhost
easycat docs             # show docs for learning, maintenance, validation, operations
easycat docs --audience learners # filter docs by reader audience or broad role
easycat docs --audience learners --json # emit a filtered docs route map for learners
easycat docs --json      # emit docs routes, audiences, and command hints for automation
easycat docs --audience app-builders # filter docs to scaffold and app-building routes
easycat docs --audience app-builders --json # emit a filtered docs route map for app builders
easycat docs --audience operators # filter docs to deployment and observability routes
easycat docs --audience operators --json # emit a filtered docs route map for operators
easycat docs --audience maintainers # filter docs to architecture and maintenance routes
easycat docs --audience maintainers --json # emit a filtered docs route map for maintainers
easycat explain E102     # look up errors and CLI schema topics
easycat explain json-schema # document the --json envelope and command metadata
easycat explain --list   # list every error code and meta topic
easycat bundles list      # list captured debug bundles and crash dumps
easycat bundles list --json # emit machine-readable bundle list
easycat bundles show PATH # summarise a debug bundle or SQLite journal
easycat bundles show PATH --json # emit machine-readable bundle/journal summary
easycat bundles export PATH # write a redacted coding-agent context pack
easycat bundles export PATH --output DIR --json # emit context-pack metadata
easycat inspect PATH      # summarise a debug bundle or SQLite journal
easycat inspect PATH --json # emit machine-readable bundle/journal summary
easycat replay PATH       # replay a debug bundle or SQLite journal
easycat replay PATH --json # emit machine-readable replay summary
easycat validate quick       # run deterministic local validation
easycat validate quick --json # emit quick validation in the standard envelope
easycat validate contracts   # run offline provider/protocol contract validation
easycat validate contracts --json # emit contract validation in the standard envelope
easycat validate release     # run the strict installed-wheel release gate
easycat validate release --json # emit release validation in the standard envelope
easycat validate report .easycat/validation/latest.json # render latest validation report
easycat validate report .easycat/validation/latest.json --json # emit latest report in the standard envelope
```

From an empty directory, `easycat init --list-templates` shows the available
scaffolds with best-fit guidance, the base `easycat[...]` package requirement
and extras, required environment variables, optional environment knobs,
generated files, transport, framework, and copyable
create/preflight/check/fix/docs/json-schema/run commands.
`easycat init my-agent` scaffolds the same one shown above: the canonical
`run(EasyConfig.mic(agent=...))` shape.
Then `easycat doctor` validates your environment before the first run. If your
provider keys live in a project `.env`, use `easycat doctor --env-file .env`;
add `--json` for the same environment/check rows without Rich formatting.
Use `easycat docs --audience learners` to narrow the human map,
`easycat docs --audience app-builders` for scaffold and app-building routes,
`easycat docs --audience operators` for deployment and observability routes,
or `easycat docs --audience maintainers` for architecture and maintenance
routes; the `maintainers` and `operators` filters also include compound labels
such as `provider maintainers`, `release maintainers`, and
`operators and maintainers`.
Coding agent? Use [AGENTS.md](AGENTS.md) for repository coding rules; use
[llms.txt](llms.txt) for machine-readable docs route discovery or run
`easycat explain json-schema`.
`easycat explain json-schema` documents the standard `--json` envelope. It covers
the docs route map,
template catalog, scaffold output, doctor environment/checks output,
validation quick/contracts/release/report output, bundle list/show/export,
inspect, and replay command families, including command-specific fields such
as `entries`, `commands`, `catalog`, `audience`, `audience_filter`,
`available_audiences`, `available_audience_filters`,
`audience_alias_note`, `command_note`, `base_requirement`, `create_command`,
`repo_create_command`,
`next_step_commands`, `pyproject_name`, `run_command`, `check_command`,
`fix_command`, `environment`, `checks`, `validation`, `source_path`, and
`fidelity_effective`, and error fields such as `report_path`, `path`, and
`output_path`. Replace uppercase or angle-bracket placeholders in command
hints, such as `PATH` or `<session_id>`, before running them.

## Current capabilities
- Session runtime that wires the audio pipeline (`AudioProcessingConfig` controls optional noise reduction, echo cancellation, VAD, and smart-turn tuning) -> STT -> agent -> TTS
- Typed event system with an EventBus for streaming-first voice events and
  configurable handler-error policy
- Passive supervisor listen-in via session audio fan-out on the EventBus
- STT providers: OpenAI, Deepgram, ElevenLabs, Cartesia
- TTS providers: OpenAI, Deepgram, ElevenLabs, Cartesia
- VAD providers: Silero (open-source), FunASR ONNX VAD (open-source), optional TEN VAD (non-permissive license), and Krisp (commercial)
- Noise reduction: RNNoise (open-source), Krisp (commercial), passthrough fallback
- Transports: Local (sounddevice), WebSocket server, WebRTC (aiortc), WebTransport (aioquic), Twilio Media Streams server
- Telephony helpers: DTMF parsing/aggregation, voicemail detection, TwiML helpers, outbound calling (Twilio), screening + IVR navigation, per-number health / retry / compliance gates, caller-ID propagation to the agent or tools
- Reliability/observability: reconnecting WebSocket, timeouts, bounded queues, metrics/tracing
- Agent/workflow adapters: OpenAI Agents SDK, PydanticAI, LangChain, LangGraph, LlamaAgents, Remote Responses API, and generic workflows

## Bring your own agent
EasyCat does not replace your agent framework. Build your agent or workflow with
your SDK of choice and hand it to EasyCat — `create_session` auto-detects
OpenAI Agents SDK, PydanticAI, LangChain, LangGraph, LlamaAgents, Remote
Responses API URLs, and your own async workflow objects with
`on_user_turn(...)` via `auto_adapt_agent`, so you don't have to wrap them
yourself.

The top-level import surface is intentionally curated and lazy. See the
[public API contract](docs/public-api.md) before adding or depending on new
`from easycat import ...` names. The canonical entry point is the
[quickstart](#quickstart-easyconfig) at the top of this README.

### Advanced: own the lifecycle
When you need the session object itself — event subscriptions, text turns, debug bundles, or your own event loop — follow the graduation guide [from EasyConfig to Session](docs/from-easyconfig-to-session.md).

## Telephony (inbound + outbound)

### Inbound calls (Twilio Media Streams)
Point Twilio's inbound webhook at a handler that returns
`<Connect><Stream>` TwiML and passes actual webhook form values through
as `<Parameter>` children:

```python
import os
from urllib.parse import parse_qsl

from fastapi import Request, Response
from easycat.telephony import validate_twilio_webhook_signature
from easycat.transports import TwilioStreamTokenStore
from easycat.transports.twilio_media import twiml_connect_stream

TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
stream_tokens = TwilioStreamTokenStore(
    os.getenv("TWILIO_STREAM_TOKEN_SECRET") or TWILIO_AUTH_TOKEN or None
)

@app.post("/twiml")
async def twiml(request: Request) -> Response:
    form_items = parse_qsl((await request.body()).decode(), keep_blank_values=True)
    if TWILIO_AUTH_TOKEN and not validate_twilio_webhook_signature(
        auth_token=TWILIO_AUTH_TOKEN,
        url=str(request.url),  # must be Twilio's exact public URL
        params=form_items,
        signature=request.headers.get("x-twilio-signature"),
    ):
        return Response(status_code=403)

    form = dict(form_items)
    xml = twiml_connect_stream(
        "wss://your-app.example.com/twilio",
        parameters={
            "Direction": form.get("Direction") or "inbound",
            "From": form.get("From", ""),
            "To": form.get("To", ""),
            "CallerName": form.get("CallerName", ""),
        },
        stream_token=stream_tokens.issue(),
    )
    return Response(content=xml, media_type="application/xml")
```

`TwilioTransport` parses `start.customParameters` and writes a
`CallIdentity` (caller / called numbers, direction, optional display
name, and any extra fields you pass) onto
`session.call_identity`. Tool code inside your agent reads
`session.call_identity.caller_number` directly. Do not pass
`"{{From}}"`-style placeholders to `twiml_connect_stream`; Twilio
forwards those verbatim in generated TwiML. When webhook validation is
enabled behind a proxy, validate against the same public URL Twilio
called, not an internal service URL.

Pass the same token store to the WebSocket transport with
`TwilioTransportConfig(stream_token_validator=stream_tokens.consume)`.
The built-in store is in-memory and suited to a single app process. For
multiple workers or replicas, route TwiML and WebSocket traffic to the
same process or provide a shared validator/store.

### Outbound calls (Twilio REST)
Enable the outbound pipeline via `EasyConfig.telephony`:

```python
from easycat import (
    EasyConfig,
    OutboundCallConfig,
    SessionPolicyConfig,
    TelephonyConfig,
    VoicemailDetectionConfig,
    create_session,
)

config = EasyConfig(
    agent=your_agent,
    session_policy=SessionPolicyConfig(
        greeting="Hi, this is Lucy from Example Health.",
    ),
    telephony=TelephonyConfig(
        enable_outbound_call_manager=True,
        outbound=OutboundCallConfig(
            from_number="+15559876543",
            twilio_account_sid="AC…",
            twilio_auth_token="…",
            twiml_url="https://your-app.example.com/outbound.twiml",
            status_callback_url="https://your-app.example.com/status",
            voicemail_detection=VoicemailDetectionConfig(
                mode="detect_end_of_greeting",  # or "detect"
                detection_timeout_s=30,
            ),
        ),
    ),
)
session = create_session(config)
```

With the outbound manager enabled you also get:

- `NumberHealthMonitor` — per-number answer rate, block count, pacing
- `CallDispositionTracker` — human / voicemail / IVR disposition stats
- `RetryStrategy` attached to the manager — `manager.retry_strategy.record_attempt(number, reason)` decides RETRY / SMS_FALLBACK / NO_RETRY
- `DNCList`, `check_calling_hours`, and `detect_opt_out` helpers you can hook into `manager.dnc_list` / `manager.compliance_check` for TCPA-friendly calling

Start the session before placing calls, and feed Twilio status callbacks
back into the same event bus:

```python
from urllib.parse import parse_qsl

from fastapi import HTTPException, Request, Response
from easycat.telephony import emit_call_status, validate_twilio_webhook_signature


await session.start()
manager = session.outbound_call_manager
if manager is None:
    raise RuntimeError("Outbound manager is not configured")

call_sid = await manager.place_call("+15551234567")


@app.post("/status")
async def status(request: Request) -> Response:
    form_items = parse_qsl((await request.body()).decode(), keep_blank_values=True)
    if TWILIO_AUTH_TOKEN and not validate_twilio_webhook_signature(
        auth_token=TWILIO_AUTH_TOKEN,
        url=str(request.url),
        params=form_items,
        signature=request.headers.get("x-twilio-signature"),
    ):
        raise HTTPException(status_code=403)
    await emit_call_status(dict(form_items), session.event_bus)
    return Response(status_code=204)
```

When the session places an outbound call via `CallInitiated`,
`session.call_identity` is stamped with `direction="outbound"` and the
dialed number.  `TwilioTransport` mirrors the other direction: on the
``<Stream>`` start event it parses caller-ID + geographic
customParameters and emits ``CallAnswered``, so observers like
``CallDispositionTracker`` see inbound and outbound calls through the
same lifecycle.

### Bot speaks first
Set `EasyConfig.session_policy.greeting` (or pass
`session_policy=SessionPolicyConfig(greeting=...)`) to have the bot
synthesize a greeting on the first `CallAnswered` event.  Works for
both inbound (stream start) and outbound (callee pickup).  Use this to
play an AI-disclosure or identification line before the caller's first
utterance — a requirement under the FCC's 2024 TCPA ruling and TX SB
140 for outbound AI calls.

### Opt-out auto-detection
The session listens on every STT final for phrases in
`easycat.telephony.OPT_OUT_PHRASES` (``"stop calling"``, ``"take me
off your list"``, ``"opt out"``, …).  On match the session:

1. emits an `OptOutDetected` event carrying the caller number, the
   matched phrase, and the full transcript text,
2. adds the caller to `session.dnc_list` when one is attached
   (pass a shared `DNCList` via `SessionPolicyConfig(dnc_list=...)`),
3. enqueues an `EndCallAction(reason="opt_out")` so the call
   terminates after the agent's current utterance finishes.

Set `SessionPolicyConfig(opt_out_detection=False)` to opt out of the
auto-wiring, or pass `opt_out_phrases=("retire me", …)` to replace the
built-in phrase list (language packs / industry-specific terminology).

### Caller-ID exposure policy
Control whether the LLM sees the caller's number or only tool code
does via `SessionPolicyConfig.caller_id_exposure`:

- `"tools_only"` (default): number available at
  `session.call_identity.caller_number` for tools, hidden from the
  LLM prompt. Right for PII-sensitive workflows.
- `"system_message"`: prepend a short system note on every turn
  (`"The caller's phone number is +1555…"`). Use when the agent needs
  to greet by number, look up account, etc.
- `"off"`: hide from both layers.

```python
config = EasyConfig(
    agent=your_agent,
    session_policy=SessionPolicyConfig(caller_id_exposure="system_message"),
)
```

### Transport kind
Tools that should behave differently on a phone call vs. a browser
session read `session.transport_kind` — one of `"telephony"`,
`"webrtc"`, `"websocket"`, `"local"`, `"noop"`, or `"custom"`.  Use
it to skip "open this URL" prompts on phone calls or mute emoji in
voice-only surfaces.

## Session lifecycle

- `async with session:` is the one public teardown idiom — entering starts the session, exiting calls `stop(force=True)`.
- `await session.stop()` is the single public teardown verb: the default (`force=False`) drains in-flight work gracefully; `force=True` aggressively cancels the pipeline first.
- `await session.wait_closed()` blocks until the session has stopped — the idiomatic pair for `async with session: await session.wait_closed()`.
- After a clean `stop()`, postmortem inspection is still supported: `session.journal.read()` and `session.export_debug_bundle(...)` continue to work.


## Pre-TTS output processors (easy mode)
If you want to change how the assistant is spoken (for example phone-number pacing
or custom pronunciations), pass processors in config:

```python
from easycat import (
    EasyConfig,
    PauseProcessor,
    PhoneticReplacementProcessor,
    create_session,
)

config = EasyConfig(
    output_processors=[
        # Replace names/terms with pronunciation-friendly spellings.
        # e.g. "Siobhan" -> "shi-vawn"
        #      "Nguyen" -> "win"
        #
        # Then apply phone-number pause formatting (via regex).
        # Note: processor order matters.
        PhoneticReplacementProcessor(
            {
                "Siobhan": "shi-vawn",
                "Nguyen": "win",
            }
        ),
        PauseProcessor(
            pattern=r"\+?\d[\d\s().-]{5,}\d",
            unit_pattern=r"\d",
            minimum_units=7,
            pause_ms=140,
        ),
    ],
)
session = create_session(config)
```

Or use the convenience helper for the common pronunciation + phone-number stack:

```python
from easycat import EasyConfig, create_session, default_pronunciation_processors

config = EasyConfig(
    output_processors=default_pronunciation_processors(
        name_pronunciations={"Siobhan": "shi-vawn", "Nguyen": "win"},
        phone_pause_ms=140,
    ),
)
session = create_session(config)
```



Need pauses for any custom pattern (not just phone numbers)?

```python
PauseProcessor(
    # match "ticket #48291" style spans
    pattern=r"ticket\s+#?\d+",
    # pause between matched digits
    unit_pattern=r"\d",
    pause_ms=180,
    minimum_units=2,
    # for style="ellipsis": 1 => "...", 2 => "... ..."
    ellipsis_count=1,
)
```

Notes:
- `strip_markdown=True` still works and is automatically composed with processors.
- Providers that do not support SSML automatically fall back to plain text.
- Pause length is adjustable via `pause_ms` for SSML and `ellipsis_count` for ellipsis style.
- For provider authors, `synthesize` accepts either a legacy `str` or `TTSInput`;
  expose `input_policy` with `TTSInputPolicy.native_ssml()` only when the backend
  accepts SSML unchanged.

### Local/open-source speech pipeline
EasyCat ships with hosted STT/TTS providers (OpenAI, Deepgram, ElevenLabs, and
Cartesia). To run fully local speech, plug in your own STT/TTS implementations and use
the same `EasyConfig` surface:

```python
from easycat import EasyConfig, create_session

from my_local_agent import LocalAgent
from my_local_stt import LocalSTTProvider
from my_local_tts import LocalTTSProvider

session = create_session(
    EasyConfig.mic(
        stt=LocalSTTProvider(...),
        tts=LocalTTSProvider(...),
        agent=LocalAgent(...),
    )
)
```

This keeps the pipeline (VAD → STT → agent → TTS) identical while letting you
swap in open-source models for fully local operation. Provider instances are
accepted by
`audio_processing=AudioProcessingConfig(vad=..., noise_reduction=..., echo_cancellation=...)`
when you have custom audio-processing stages; the shorter legacy `vad=`,
`noise_reduction=`, and `echo_cancellation=` aliases remain supported.

## Inspecting conversation flow

Observability is handled by the journal runtime. Enable it via `debug="light"`
(in-memory) or `debug="full"` (SQLite WAL, crash-durable) and tail records
live or read them after the session ends:

```python
import asyncio

from easycat import EasyConfig, JournalRecordKind, create_session


async def tail(session, stop_tailing: asyncio.Event) -> None:
    async for record in session.journal.follow(stop=stop_tailing):
        if record.kind == JournalRecordKind.EVENT:
            print(f"[{record.name}] {record.data}")


async def main() -> None:
    config = EasyConfig(debug="light")
    async with create_session(config) as session:
        stop_tailing = asyncio.Event()
        tail_task = asyncio.create_task(tail(session, stop_tailing))
        try:
            await session.wait_closed()
        finally:
            stop_tailing.set()
            await tail_task


asyncio.run(main())
```

Records carry `session_id`, `turn_id`, and monotonic sequence numbers so
cross-system traces join cleanly.

Use `debug="full"` when you need durable inspection. EasyCat writes SQLite
journals under `.easycat/journals/`; pass `record_to="runs"` on `EasyConfig`
or `create_text_session(...)` when you also want a timestamped debug bundle
exported on shutdown. After the run, inspect a journal with:

```bash
uv run easycat inspect .easycat/journals/<session_id>.sqlite
```

### Hook directly into agent/tool events
You can subscribe to agent stream events (including tool calls) via the session:

```python
session = create_session(config)

registrations = session.subscribe_agent_events(
    on_delta=lambda e: print("delta:", e.text),
    on_final=lambda e: print("final:", e.text),
    on_tool_started=lambda e: print("tool start:", e.tool_name, e.call_id),
    on_tool_delta=lambda e: print("tool delta:", e.call_id, e.delta),
    on_tool_result=lambda e: print("tool result:", e.call_id, e.result),
)

# Later, detach all handlers in one call:
session.unsubscribe_handlers(registrations)
```

### OpenAI Agents SDK (idiomatic)
```python
from agents import Agent

from easycat import EasyConfig, create_session

agent = Agent(
    name="Support",
    instructions="Help customers with account issues.",
)

config = EasyConfig(
    agent=agent,
)
session = create_session(config)
```

### PydanticAI (idiomatic)
```python
from pydantic_ai import Agent as PydanticAgent

from easycat import EasyConfig, create_session

pydantic_agent = PydanticAgent(
    "openai:gpt-5.2",
    system_prompt="Help customers with account issues.",
)

config = EasyConfig(
    agent=pydantic_agent,
)
session = create_session(config)
```

The `pydantic-ai` extra targets stable PydanticAI v1. The
`pydantic-ai-v2-beta` extra pins `pydantic-ai==2.0.0b7` exactly for local
verification and apps that want to opt into the prerelease before it is stable.

### Workflows (recommended for multi-step voice apps)

For voice apps with step-based control flow, define a workflow object with
an async `on_user_turn(text) -> str` method and hand it to
`create_session`.  `auto_adapt_agent` wraps it in a
`GenericWorkflowBridge`, so no import dance is needed.

```python
from easycat import EasyConfig, create_session


class BookingWorkflow:
    def __init__(self) -> None:
        self.flight = None

    async def on_user_turn(self, text: str) -> str:
        if self.flight is None:
            self.flight = {"flight_number": "AK456"}
            return "I found flight AK456. What seat would you like?"
        return "Got it. I saved seat 1A for you."


workflow = BookingWorkflow()

config = EasyConfig(
    agent=workflow,  # auto-adapted to GenericWorkflowBridge
)
session = create_session(config)
```

Need recorder access, cancellation tokens, or handoffs? Add a
`recorder: AgentRecorder` parameter to `on_user_turn` — the bridge
flips into deep mode and calls your method with the live recorder plus
a cancel token.

In most cases, you can just pass your PydanticAI agent or workflow to
`EasyConfig(agent=...)` and call `create_session(config)`; EasyCat
auto-adapts it to the right bridge. Under the hood, simple single-agent
assistants use `PydanticAIBridge`, while step-based workflows with
specialist pinning or programmatic hand-offs use `GenericWorkflowBridge`.

### LangChain and LangGraph

Pass a LangChain `Runnable` directly as `EasyConfig(agent=...)`; EasyCat
wraps it in `LangChainBridge` and streams text deltas, tool calls, and
journal cursors from `astream_events()`. Install the repo extra with
`uv sync --extra langchain --group dev`; model packages such as `langchain-openai`
are installed separately by your app.

Pass a compiled LangGraph graph directly the same way. To be auto-adapted,
the graph must be compiled with a checkpointer, for example
`graph.compile(checkpointer=InMemorySaver())`; EasyCat then uses
`LangGraphBridge` so thread IDs, checkpoints, node cursors, and barge-in
state edits stay LangGraph-native. Install the repo extra with
`uv sync --extra langgraph --group dev`; see `examples/langchain_voice.py` and
`examples/langgraph_voice.py` for runnable versions.

### LlamaAgents / LlamaIndex Workflows

For LlamaAgents' `llama-index-workflows` package, pass a `Workflow`
instance directly or construct `LlamaAgentsBridge` when you need to set
the start-event key. By default the bridge sends the user turn as
`StartEvent(message=...)` and preserves the workflow `Context` across
turns.

```python
from workflows import Workflow, step
from workflows.events import StartEvent, StopEvent

from easycat import EasyConfig, create_session


class GreetingWorkflow(Workflow):
    @step
    async def greet(self, ev: StartEvent) -> StopEvent:
        return StopEvent(result=f"Hello, {ev.message}")


session = create_session(
    EasyConfig(
        agent=GreetingWorkflow(),
    )
)
```

To call a workflow mounted on a LlamaAgents workflow server, construct
the bridge with a `WorkflowClient` or `base_url`:

```python
from easycat.integrations.agents import LlamaAgentsBridge

bridge = LlamaAgentsBridge(base_url="http://localhost:8080", workflow_name="greet")
```

Workflows that stream `ProgressEvent(msg=...)` style events are surfaced
as EasyCat text deltas. Human-in-the-loop workflows that emit
`InputRequiredEvent(prefix=...)` pause after speaking the prompt; the
next user turn is sent back as `HumanResponseEvent(response=...)` and
the same workflow handler resumes. If your workflow uses custom start
or human-response events, pass `start_event_factory=` or
`human_response_event_factory=` when constructing the bridge.

## Examples
Runnable examples live in the `examples/` directory. The maintained command
matrix is [`examples/README.md`](examples/README.md); it lists every runnable
example with its command, required extras/packages, and environment variables.

Use it to find examples for local mic/speaker bots, WebSocket and browser
transports, WebRTC and WebTransport, Twilio Media Streams, provider swaps,
agent bridges, function tools, session actions, turn-taking controls, custom
providers, debug bundles, and journal inspection.

### Quickstart: WebRTC in browser (fast path)
1. Install extras:
   `uv sync --extra webrtc --extra openai --extra openai-agents --group dev`
2. Set your key and preflight:
   `export OPENAI_API_KEY="your-api-key"`
   `uv run easycat doctor`
   If keys live in `.env`, use `uv run easycat doctor --env-file .env`.
3. Run the server:
   `uv run python examples/webrtc_server.py`
   Or with `.env`: `uv run --env-file .env python examples/webrtc_server.py`
4. Open:
   `http://localhost:8080`
   (auto-redirects to `webrtc_client.html` when using the bundled static client)

If browser clients are remote (not localhost), run behind HTTPS and configure
TURN (`TURN_SERVER_URL`, `TURN_USERNAME`, `TURN_CREDENTIAL`) for NAT traversal.
The public `/config` endpoint hides TURN credentials by default; set
`WEBRTC_EXPOSE_ICE_CREDENTIALS=1` only for trusted demos or short-lived TURN
credentials when browser-side relay candidates are required. The bundled
browser client is served same-origin, so WebRTC signaling sends no wildcard
CORS headers by default; if you host the browser UI elsewhere, pass explicit
`cors_allowed_origins=("https://your-ui.example",)` to `WebRTCTransportConfig`.

## Repo layout
- src/easycat: library code
- tests: unit/integration tests (some are skipped without API keys)

## Factory APIs

EasyCat supports two complementary factory styles:

- String-based provider selection (`create_stt_provider` / `create_tts_provider`) for dynamic setups.
- Config-object or provider-instance wiring via `EasyConfig` + `create_session`.

Both styles now resolve provider classes through the same central registries in
`easycat.stt.factory` and `easycat.tts.factory`, so adding providers only
requires updating one mapping per domain.
