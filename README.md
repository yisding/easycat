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
- Transports: Local (sounddevice), WebSocket server, Twilio Media Streams server
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

> Note: `SessionConfig` requires real provider implementations (unless you set
> `enable_noise_reduction=False`, which allows a no-op noise reducer). For most
> users, `EasyCatConfig` + `create_session` is the fastest way to get a working
> pipeline.

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
- `twilio_app.py`: Twilio Media Streams example
- `pydantic_ai_voice.py`: PydanticAI adapter example

## Not yet in this repo
- Optional dependency extras in packaging

## Repo layout
- src/easycat: library code
- tests: unit/integration tests (some are skipped without API keys)
- workstreams: design notes and task plans for upcoming work

## Install
Python 3.11+ is required.

```
python -m pip install -e .
```

Optional dependencies you may need depending on providers/transports:
- sounddevice (LocalTransport)
- torch (Silero VAD)
- RNNoise shared library (rnnoise)
- Krisp SDK (krisp_audio)
- Provider SDKs/keys for OpenAI, Deepgram, ElevenLabs
