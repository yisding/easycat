# EasyCat

Slim, batteries-included voice bot framework that plugs into idiomatic
OpenAI Agents SDK, PydanticAI agents, or PydanticAI workflows.

## Current capabilities
- Session runtime that wires the audio pipeline (noise reduction -> VAD -> STT -> agent -> TTS)
- Typed event system with an EventBus for streaming-first voice events
- Passive supervisor listen-in via session audio fan-out on the EventBus
- STT providers: OpenAI, Deepgram, ElevenLabs
- TTS providers: OpenAI, Deepgram, ElevenLabs
- VAD providers: Silero (open-source), TEN VAD (open-source), and Krisp (commercial)
- Noise reduction: RNNoise (open-source), Krisp (commercial), passthrough fallback
- Transports: Local (sounddevice), WebSocket server, WebRTC (aiortc), Twilio Media Streams server
- Telephony helpers: DTMF parsing/aggregation, voicemail detection, TwiML helpers, outbound calling (Twilio), screening + IVR navigation, per-number health / retry / compliance gates, caller-ID propagation to the agent or tools
- Reliability/observability: reconnecting WebSocket, timeouts, bounded queues, metrics/tracing
- Agent adapters: use OpenAI Agents SDK or PydanticAI directly and wrap with EasyCat
- Workflow adapter: use a stateful PydanticAI workflow as the session boundary

## Bring your own agent
EasyCat does not replace your agent framework. Build your agent or workflow with
your SDK of choice and hand it to EasyCat — `create_session` auto-detects
OpenAI Agents SDK and PydanticAI objects via `auto_adapt_agent`, so you don't
have to wrap them yourself.

### Quickstart (EasyCatConfig)
```python
from agents import Agent

from easycat import EasyCatConfig, create_session

agent = Agent(
    name="Support",
    instructions="Help customers with account issues.",
)

config = EasyCatConfig(
    openai_api_key="your-api-key",
    agent=agent,
)
session = create_session(config)
```

> Note: `EasyCatConfig` will automatically wire **OpenAI STT + OpenAI TTS** if
> you provide `openai_api_key` and do not override `stt` or `tts`. If you omit
> the API key, you must supply `stt` and `tts` configs explicitly. For most
> users, `EasyCatConfig` + `create_session` is the fastest way to get a working
> pipeline.
>
> The underlying bridge classes live in `easycat.integrations.agents`
> (`OpenAIAgentsBridge`, `PydanticAIBridge`, `GenericWorkflowBridge`,
> `RemoteResponsesAPIBridge`) for callers who want to construct them by hand.

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
from easycat.transports.twilio_media import twiml_connect_stream

TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")

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

### Outbound calls (Twilio REST)
Enable the outbound pipeline via `EasyCatConfig.telephony`:

```python
from easycat import (
    EasyCatConfig,
    OutboundCallConfig,
    TelephonyConfig,
    VoicemailDetectionConfig,
    create_session,
)

config = EasyCatConfig(
    openai_api_key="…",
    agent=your_agent,
    greeting="Hi, this is Lucy from Example Health.",
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
Set `EasyCatConfig.greeting` to have the bot synthesize a greeting on
the first `CallAnswered` event.  Works for both inbound (stream
start) and outbound (callee pickup).  Use this to play an
AI-disclosure or identification line before the caller's first
utterance — a requirement under the FCC's 2024 TCPA ruling and TX SB
140 for outbound AI calls.

### Opt-out auto-detection
The session listens on every STT final for phrases in
`easycat.telephony.OPT_OUT_PHRASES` (``"stop calling"``, ``"take me
off your list"``, ``"opt out"``, …).  On match the session:

1. emits an `OptOutDetected` event carrying the caller number, the
   matched phrase, and the full transcript text,
2. adds the caller to `session.dnc_list` when one is attached
   (pass a shared `DNCList` via `EasyCatConfig.dnc_list`),
3. enqueues an `EndCallAction(reason="opt_out")` so the call
   terminates after the agent's current utterance finishes.

Set `SessionConfig.opt_out_detection=False` to opt out of the
auto-wiring, or pass `opt_out_phrases=("retire me", …)` to replace
the built-in phrase list (language packs / industry-specific
terminology).

### Caller-ID exposure policy
Control whether the LLM sees the caller's number or only tool code
does via `EasyCatConfig.caller_id_exposure`:

- `"tools_only"` (default): number available at
  `session.call_identity.caller_number` for tools, hidden from the
  LLM prompt. Right for PII-sensitive workflows.
- `"system_message"`: prepend a short system note on every turn
  (`"The caller's phone number is +1555…"`). Use when the agent needs
  to greet by number, look up account, etc.
- `"off"`: hide from both layers.

```python
config = EasyCatConfig(
    openai_api_key="…",
    agent=your_agent,
    caller_id_exposure="system_message",
)
```

### Transport kind
Tools that should behave differently on a phone call vs. a browser
session read `session.transport_kind` — one of `"telephony"`,
`"webrtc"`, `"websocket"`, `"local"`, `"noop"`, or `"custom"`.  Use
it to skip "open this URL" prompts on phone calls or mute emoji in
voice-only surfaces.

## Session lifecycle

- `await session.stop()` performs graceful shutdown and releases live backend resources.
- `await session.shutdown()` force-cancels in-flight work, then releases the same live backend resources.
- `Session.close()` is lower-level and only finalizes the journal's clean-close marker.
- After a clean `stop()` or `shutdown()`, postmortem inspection is still supported: `session.journal.read()` and `session.export_debug_bundle(...)` continue to work.


## Pre-TTS output processors (easy mode)
If you want to change how the assistant is spoken (for example phone-number pacing
or custom pronunciations), pass processors in config:

```python
from easycat import (
    EasyCatConfig,
    PauseProcessor,
    PhoneticReplacementProcessor,
    create_session,
)

config = EasyCatConfig(
    openai_api_key="your-api-key",
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
from easycat import EasyCatConfig, create_session, default_pronunciation_processors

config = EasyCatConfig(
    openai_api_key="your-api-key",
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
- For provider authors, `synthesize` accepts either a legacy `str` or `TTSInput`.

### Local/open-source speech pipeline
EasyCat ships with hosted STT/TTS providers (OpenAI, Deepgram, ElevenLabs). To
run fully local speech, plug in your own STT/TTS implementations and use
`SessionConfig` directly:

```python
from easycat import Session, SessionConfig

from my_local_stt import LocalSTTProvider
from my_local_tts import LocalTTSProvider

session = Session(
    SessionConfig(
        stt=LocalSTTProvider(...),
        tts=LocalTTSProvider(...),
        # keep using local transport to stay offline
        ...
    )
)
```

This keeps the pipeline (VAD → STT → agent → TTS) identical while letting you
swap in open-source models for fully local operation.

## Inspecting conversation flow

Observability is handled by the journal runtime. Enable it via `debug="light"`
(in-memory) or `debug="full"` (SQLite WAL, crash-durable) and tail records
live or read them after the session ends:

```python
import asyncio

from easycat import EasyCatConfig, JournalRecordKind, create_session

config = EasyCatConfig(openai_api_key="your-api-key", debug="light")
session = create_session(config)

async def tail(session):
    async for record in session.journal.follow():
        if record.kind == JournalRecordKind.EVENT:
            print(f"[{record.name}] {record.data}")

asyncio.create_task(tail(session))
```

Records carry `session_id`, `turn_id`, and monotonic sequence numbers so
cross-system traces join cleanly.

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

from easycat import Session, SessionConfig
from easycat.integrations.agents import OpenAIAgentsBridge

agent = Agent(
    name="Support",
    instructions="Help customers with account issues.",
)

bridge = OpenAIAgentsBridge(agent=agent)
session = Session(SessionConfig(agent=bridge, ...))
```

### PydanticAI (idiomatic)
```python
from pydantic_ai import Agent as PydanticAgent

from easycat import Session, SessionConfig
from easycat.integrations.agents import PydanticAIBridge

pydantic_agent = PydanticAgent(
    "openai:gpt-5.2",
    system_prompt="Help customers with account issues.",
)

bridge = PydanticAIBridge(agent=pydantic_agent)
session = Session(SessionConfig(agent=bridge, ...))
```

### Workflows (recommended for multi-step voice apps)

For voice apps with step-based control flow, define a workflow object with
an async `on_user_turn(text) -> str` method and hand it to
`create_session`.  `auto_adapt_agent` wraps it in a
`GenericWorkflowBridge`, so no import dance is needed.

```python
from easycat import EasyCatConfig, create_session


class BookingWorkflow:
    def __init__(self) -> None:
        self.flight = None

    async def on_user_turn(self, text: str) -> str:
        if self.flight is None:
            self.flight = {"flight_number": "AK456"}
            return "I found flight AK456. What seat would you like?"
        return "Got it. I saved seat 1A for you."


workflow = BookingWorkflow()

config = EasyCatConfig(
    openai_api_key="your-api-key",
    agent=workflow,  # auto-adapted to GenericWorkflowBridge
)
session = create_session(config)
```

Need recorder access, cancellation tokens, or handoffs? Add a
`recorder: AgentRecorder` parameter to `on_user_turn` — the bridge
flips into deep mode and calls your method with the live recorder plus
a cancel token.

In most cases, you can just pass your PydanticAI agent or workflow to
`EasyCatConfig(agent=...)` and call `create_session(config)`; EasyCat
auto-adapts it to the right bridge. Under the hood, simple single-agent
assistants use `PydanticAIBridge`, while step-based workflows with
specialist pinning or programmatic hand-offs use `GenericWorkflowBridge`.

## Examples
Runnable examples live in the `examples/` directory:

**Transports**
- `openai_agents_voice.py`: local microphone/speaker loop with OpenAI Agents SDK
- `ws_server.py`: WebSocket server (multi-session)
- `ws_browser_example.py`: browser mic/speaker over WebSocket + static web client
- `ws_supervisor_server.py`: browser caller + passive supervisor listen-in over WebSocket
- `webrtc_server.py`: WebRTC voice chat with browser client
- `webrtc_observability_server.py`: WebRTC + FastAPI dashboard streaming live events
- `twilio_app.py`: Twilio Media Streams example

**Agents**
- `pydantic_ai_voice.py`: single-agent PydanticAI example
- `pydantic_ai_workflow_voice.py`: workflow-level PydanticAI example (multi-agent hand-off)
- `function_tools_openai.py` / `function_tools_pydantic.py`: agent function-calling tools
- `session_actions_openai.py` / `session_actions_pydantic.py`: agent-initiated session actions (end-call)

**Provider swaps**
- `deepgram_stt.py`: Deepgram STT + OpenAI TTS
- `elevenlabs_tts.py`: OpenAI STT + ElevenLabs TTS (typed config with voice customization)
- `cartesia_voice.py`: Cartesia STT + Cartesia TTS
- `combined_providers.py`: Deepgram STT + ElevenLabs TTS together (stages compose)

**Turn-taking**
- `push_to_talk.py`: manual `start_turn`/`end_turn` instead of VAD
- `smart_turn_demo.py`: ONNX-based endpoint detection for faster turn transitions

**Advanced**
- `custom_stt_provider.py` / `custom_tts_provider.py` / `custom_vad_provider.py`: inject a
  user-written provider via `SessionConfig`
- `debug_bundle.py`: record with `debug="light"`, export a `RunBundle`, inspect it
- `journal_demo.py`: one-turn synthetic session that dumps journal records (no API keys)

### Quickstart: WebRTC in browser (fast path)
1. Install extras:
   `uv sync --extra webrtc --extra openai --extra openai-agents`
2. Set your key:
   `export OPENAI_API_KEY="your-api-key"`
3. Run the server:
   `uv run python examples/webrtc_server.py`
4. Open:
   `http://localhost:8080`
   (auto-redirects to `webrtc_client.html` when using the bundled static client)

If browser clients are remote (not localhost), run behind HTTPS and configure
TURN (`TURN_SERVER_URL`, `TURN_USERNAME`, `TURN_CREDENTIAL`) for reliable NAT traversal.

## Repo layout
- src/easycat: library code
- tests: unit/integration tests (some are skipped without API keys)

## Install
Python 3.11+ is required.

```
uv sync
```

### Quickstart (local mic/speaker + OpenAI STT/TTS + OpenAI Agents SDK)
The fastest path to a working end-to-end pipeline on your machine:

```
uv sync --extra quickstart
export OPENAI_API_KEY="your-api-key"
uv run python examples/openai_agents_voice.py
```

The `quickstart` extra bundles local audio, OpenAI providers, noise reduction,
and a lightweight VAD (TEN VAD) so you can skip torch.  If you prefer Silero
VAD (requires torch), install extras individually:

```
uv sync --extra local --extra openai --extra openai-agents --extra rnnoise
uv pip install torch
```

Optional dependencies you may need depending on providers/transports:
- sounddevice (LocalTransport)
- aiortc + aiohttp (WebRTCTransport): `uv sync --extra webrtc`
- numpy + onnxruntime (Smart Turn ONNX endpoint detector): `uv sync --extra smart-turn`
- ten-vad + numpy (TEN VAD; use latest ten-vad for macOS/Windows ONNX support)
- torch (Silero VAD)
- pyrnnoise + requests (RNNoise noise reduction backend)
- Krisp SDK (krisp_audio)
- Provider SDKs/keys for OpenAI, Deepgram, ElevenLabs

## Factory APIs

EasyCat supports two complementary factory styles:

- String-based provider selection (`create_stt_provider` / `create_tts_provider`) for dynamic setups.
- Config-object based provider wiring via `EasyCatConfig` + `create_session`.

Both styles now resolve provider classes through the same central registries in
`easycat.stt.factory` and `easycat.tts.factory`, so adding providers only
requires updating one mapping per domain.
