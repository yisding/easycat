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

## Not yet in this repo
- EasyCatConfig / create_session convenience layer (planned in workstreams)
- Runnable examples or CLI
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
