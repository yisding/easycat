# EasyCat

Slim, batteries-included voice bot framework that plugs into idiomatic
OpenAI Agents SDK or PydanticAI agents.

## Current capabilities
- Session runtime that wires the audio pipeline (noise reduction -> VAD -> STT -> agent -> TTS)
- Typed event system with an EventBus for streaming-first voice events
- STT providers: OpenAI, Deepgram, ElevenLabs
- TTS providers: OpenAI, Deepgram, ElevenLabs
- VAD providers: Silero (open-source) and Krisp (commercial)
- Noise reduction: RNNoise (open-source), Krisp (commercial), passthrough fallback
- Transports: Local (sounddevice), WebSocket server, WebRTC (aiortc), Twilio Media Streams server
- Telephony helpers: DTMF parsing/aggregation, voicemail detection, TwiML helpers
- Reliability/observability: reconnecting WebSocket, timeouts, bounded queues, metrics/tracing
- Agent adapters: use OpenAI Agents SDK or PydanticAI directly and wrap with EasyCat

## Bring your own agent
EasyCat does not replace your agent framework. Build your agent with your SDK of
choice, then wrap it with an EasyCat adapter when creating a session.

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

## Examples
Runnable examples live in the `examples/` directory:
- `local_chat.py`: local microphone/speaker loop
- `ws_server.py`: WebSocket server example
- `webrtc_server.py`: WebRTC voice chat with browser client
- `twilio_app.py`: Twilio Media Streams example
- `pydantic_ai_voice.py`: PydanticAI adapter example

### Quickstart: WebRTC in browser (fast path)
1. Install extras:
   `python -m pip install -e ".[webrtc,openai,openai-agents]"`
2. Set your key:
   `export OPENAI_API_KEY="your-api-key"`
3. Run the server:
   `python examples/webrtc_server.py`
4. Open:
   `http://localhost:8080`
   (auto-redirects to `webrtc_client.html` when using the bundled static client)

If browser clients are remote (not localhost), run behind HTTPS and configure
TURN (`TURN_SERVER_URL`, `TURN_USERNAME`, `TURN_CREDENTIAL`) for reliable NAT traversal.

## Repo layout
- src/easycat: library code
- tests: unit/integration tests (some are skipped without API keys)
- workstreams: design notes and task plans for upcoming work

## Install
Python 3.11+ is required.

```
python -m pip install -e .
```

### Simplest setup (local mic/speaker + OpenAI STT/TTS + OpenAI Agents SDK)
If you want the shortest path to a working end-to-end pipeline on your machine:

```
python -m pip install -e ".[local,openai,openai-agents,rnnoise]"
python -m pip install torch
export OPENAI_API_KEY="your-api-key"
python examples/local_chat.py
```

Optional dependencies you may need depending on providers/transports:
- sounddevice (LocalTransport)
- aiortc + aiohttp (WebRTCTransport): `pip install -e ".[webrtc]"`
- torch (Silero VAD)
- pyrnnoise + requests (RNNoise noise reduction backend)
- Krisp SDK (krisp_audio)
- Provider SDKs/keys for OpenAI, Deepgram, ElevenLabs
