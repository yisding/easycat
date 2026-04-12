# EasyCat

Slim, batteries-included voice bot framework that plugs into idiomatic
OpenAI Agents SDK, PydanticAI agents, or PydanticAI workflows.

## Current capabilities
- Session runtime that wires the audio pipeline (noise reduction -> VAD -> STT -> agent -> TTS)
- Typed event system with an EventBus for streaming-first voice events
- STT providers: OpenAI, Deepgram, ElevenLabs
- TTS providers: OpenAI, Deepgram, ElevenLabs
- VAD providers: Silero (open-source), TEN VAD (open-source), and Krisp (commercial)
- Noise reduction: RNNoise (open-source), Krisp (commercial), passthrough fallback
- Transports: Local (sounddevice), WebSocket server, WebRTC (aiortc), Twilio Media Streams server
- Telephony helpers: DTMF parsing/aggregation, voicemail detection, TwiML helpers
- Reliability/observability: reconnecting WebSocket, timeouts, bounded queues, metrics/tracing
- Agent adapters: use OpenAI Agents SDK or PydanticAI directly and wrap with EasyCat
- Workflow adapter: use a stateful PydanticAI workflow as the session boundary

## Bring your own agent
EasyCat does not replace your agent framework. Build your agent or workflow with
your SDK of choice, then wrap it with an EasyCat adapter when creating a session.

### Quickstart (EasyCatConfig)
```python
from easycat import EasyCatConfig, create_session
from easycat.agents import OpenAIAgentsAdapter
from agents import Agent

agent = Agent(
    name="Support",
    instructions="Help customers with account issues.",
)

config = EasyCatConfig(
    openai_api_key="your-api-key",
    agent=OpenAIAgentsAdapter(agent),
)
session = create_session(config)
```

> Note: `EasyCatConfig` will automatically wire **OpenAI STT + OpenAI TTS** if
> you provide `openai_api_key` and do not override `stt` or `tts`. If you omit
> the API key, you must supply `stt` and `tts` configs explicitly. For most
> users, `EasyCatConfig` + `create_session` is the fastest way to get a working
> pipeline.

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

## Event-by-event logging (barge-in, ASR, TTS)
EasyCat can now attach a built-in event trace logger that prints one log line
per EventBus event so it is easy to inspect conversation flow (including
`Interruption` / barge-in, `STTPartial` / `STTFinal`, and TTS events).

```python
import logging

from easycat import EasyCatConfig, EventLoggingConfig, create_session

logging.basicConfig(level=logging.INFO)

config = EasyCatConfig(
    openai_api_key="your-api-key",
    event_logging=EventLoggingConfig(
        enabled=True,
        include_partials=True,
        include_audio_events=False,  # set True to log every TTSAudio chunk
        include_text=True,          # set False to log only text lengths
    ),
)
session = create_session(config)
```

By default, logs are emitted to logger name `easycat.event_trace` and include
a per-session event index + relative timestamp for easier debugging.

For production ingestion, you can enable JSON logs + event throttling and inspect
a small in-memory ring buffer for "last N events" snapshots:

```python
event_logging=EventLoggingConfig(
    enabled=True,
    json_mode=True,
    sample_rates={"STTPartial": 0.25},   # keep every 4th partial
    min_interval_s={"TTSAudio": 0.25},   # max 4 audio logs/second
    ring_buffer_size=500,
)
```

Events now carry `session_id` and `turn_id` correlation fields, and tool events
also include `call_id`, making cross-system traces easier to join.

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
from easycat.agents import OpenAIAgentsAdapter

agent = Agent(
    name="Support",
    instructions="Help customers with account issues.",
)

adapter = OpenAIAgentsAdapter(agent)
session = Session(SessionConfig(agent=adapter, ...))
```

### PydanticAI (idiomatic)
```python
from pydantic_ai import Agent as PydanticAgent

from easycat import Session, SessionConfig
from easycat.agents import PydanticAIAdapter

pydantic_agent = PydanticAgent(
    "openai:gpt-5.2",
    system_prompt="Help customers with account issues.",
)

adapter = PydanticAIAdapter(pydantic_agent)
session = Session(SessionConfig(agent=adapter, ...))
```

### PydanticAI workflows (recommended for voice apps)

For many voice apps, the best PydanticAI integration point is not a single
agent but a workflow object that owns the current specialist/step and decides
which PydanticAI agent handles each user turn.

This maps well to PydanticAI's programmatic hand-off style:
- the caller can stay pinned to one specialist across turns
- you do not pay an extra router-model call on every turn
- the workflow can keep private per-agent histories while EasyCat only sees the
  spoken response for the current turn

```python
from easycat import EasyCatConfig, create_session
from easycat.agents import WorkflowTurnResult


class BookingWorkflow:
    def __init__(self) -> None:
        self.active_agent_id = "flight_search"
        self.flight = None

    async def on_user_turn(self, text: str) -> str | WorkflowTurnResult:
        if self.flight is None:
            self.flight = {"flight_number": "AK456"}
            self.active_agent_id = "seat_selection"
            return WorkflowTurnResult(
                text="I found flight AK456. What seat would you like?",
                structured_output=self.flight,
                active_agent_id=self.active_agent_id,
            )

        return WorkflowTurnResult(
            text="Got it. I saved seat 1A for you.",
            structured_output={"row": 1, "seat": "A"},
            active_agent_id=self.active_agent_id,
        )


workflow = BookingWorkflow()

config = EasyCatConfig(
    openai_api_key="your-api-key",
    agent=workflow,  # auto-adapted to PydanticAIWorkflowAdapter
)
session = create_session(config)
```

Use `PydanticAIAdapter` for simple single-agent assistants. Use
`PydanticAIWorkflowAdapter` when your voice app has step-based control flow,
specialist pinning, or programmatic hand-offs between turns.

## Examples
Runnable examples live in the `examples/` directory:
- `local_chat.py`: local microphone/speaker loop
- `ws_server.py`: WebSocket server example
- `ws_browser_example.py`: browser mic/speaker over WebSocket + static web client
- `webrtc_server.py`: WebRTC voice chat with browser client
- `twilio_app.py`: Twilio Media Streams example
- `pydantic_ai_voice.py`: single-agent PydanticAI example
- `pydantic_ai_workflow_voice.py`: workflow-level PydanticAI example
- `pydantic_ai_support_workflow.py`: pinned-specialist support workflow example

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
uv run python examples/local_chat.py
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
