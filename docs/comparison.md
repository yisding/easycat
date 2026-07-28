# Choosing EasyCat, Pipecat, or LiveKit Agents

The three projects overlap, but they optimize for different starting points.
Choose EasyCat when you already have an idiomatic Python agent or workflow and
want to add a voice session, durable local journals, replay, and provider or
transport swaps without moving the application into a new agent runtime.
Choose Pipecat when a broad multimodal pipeline, provider catalog, and native
client ecosystem are the center of the product. Choose LiveKit Agents when a
WebRTC room, cross-platform LiveKit clients, and managed agent-server
orchestration are the center.

This is a fit guide, not a scorecard. The competitor links and capability
summary were reviewed on 2026-07-27; verify them before making a long-lived
platform decision.

## Capability and product fit

| Question | EasyCat | Pipecat | LiveKit Agents |
| --- | --- | --- | --- |
| Primary abstraction | `VoiceApp` for app-first use, `EasyConfig` for explicit wiring, and `Session` for lifecycle control | Ordered pipelines of frames and frame processors | Agents and `AgentSession` instances that join LiveKit rooms as realtime participants |
| Existing agent code | Bridges OpenAI Agents SDK, PydanticAI, LangChain, LangGraph, LlamaAgents, Remote Responses API, and generic async workflows | Provider services and processors live inside a Pipecat pipeline; Pipecat Flows adds structured conversation state | Agent, task, workflow, tool, and handoff abstractions live inside the Agents runtime |
| Media scope | Audio-first voice sessions over local audio, browser/WebRTC, WebSocket, WebTransport, and Twilio Media Streams | Voice and multimodal pipelines spanning audio, video, images, and data | Voice, video, text, transcriptions, and vision over LiveKit rooms |
| Provider ecosystem | Focused built-ins for OpenAI, Deepgram, ElevenLabs, and Cartesia speech, plus swappable protocols | The project documents more than 100 service integrations across speech, models, media, and analytics | Broad STT, LLM, TTS, realtime, and avatar plugins, plus managed LiveKit Inference |
| Client surface | Browser playground and transport protocols; no separate suite of native mobile client SDKs | Official web, mobile, and native clients using RTVI | LiveKit's WebRTC SDK ecosystem across major web, mobile, and native platforms |
| Telephony | Twilio Media Streams, inbound/outbound calls, callbacks, DTMF, transfer, and hangup helpers | PSTN and SIP through several WebSocket, WebRTC, and telephony integrations | SIP and telephony integrated with the LiveKit server and Cloud |
| Deployment | Self-host the Python process; Docker and production-server guides are included, but there is no EasyCat managed cloud | Self-host anywhere Python runs or use Pipecat Cloud | Self-host the agents and LiveKit server, or use LiveKit Cloud for builds, scaling, inference, and observability |
| Postmortem workflow | Durable local journals, debug bundles, redacted export, deterministic replay, latency CLI, and a local debugger | Pipeline metrics and observers, with Whisker and Tail in the wider Pipecat ecosystem | Cloud session transcripts, traces, logs, and recordings; self-hosted deployments can export OpenTelemetry data |

Sources: EasyCat's [architecture](architecture.md),
[agent bridges](extending/agent-bridge.md), [observability](observability.md),
and [Twilio chapter](using-easycat/10-telephony/README.md);
Pipecat's [introduction](https://docs.pipecat.ai/overview/introduction),
[server overview](https://docs.pipecat.ai/api-reference/server/introduction),
[transports](https://docs.pipecat.ai/pipecat/learn/transports), and
[deployment overview](https://docs.pipecat.ai/pipecat/deployment/overview);
LiveKit Agents'
[introduction](https://docs.livekit.io/agents/),
[models overview](https://docs.livekit.io/agents/integrations/plugins/),
[deployment overview](https://docs.livekit.io/deploy/agents/), and
[observability overview](https://docs.livekit.io/agents/build/record).

## Normalized scheduling benchmark

The repository includes a provider-free comparison at one shared external
boundary: an accepted transcript/text turn to the first audio frame accepted
by the framework's transport or output sink. Each framework receives the same
20 ms LLM double and 20 ms TTS double. Workers stay alive while their order is
randomized for every warmup and measured round, so dependency installation and
process startup are outside the metric.

Snapshot generated 2026-07-28 UTC from clean EasyCat commit
`d6b613b82346a8c556341853847123c930c1dd05`, using Python 3.12.13 on Linux
aarch64. The isolated locks pin `livekit-agents==1.6.6`,
`pipecat-ai==1.4.0`, and `websockets==15.0.1`.

| Framework | P50 (ms) | P95 (ms) | Samples |
| --- | ---: | ---: | ---: |
| EasyCat | 41.22 | 41.55 | 30 |
| Pipecat | 41.31 | 42.10 | 30 |
| LiveKit Agents | 42.84 | 43.80 | 30 |

EasyCat recorded the lowest P50 and P95 in this run. The 0.09 ms P50
difference from Pipecat is too small to imply a perceptible product advantage;
the useful result is that EasyCat's session and journaling path does not add a
large scheduling penalty in this controlled workload. Re-run on the target
hardware before using the ranking in a decision.

The [raw JSON artifact](../perf/framework-latency-2026-07-28.json) contains all
samples, exact lock hashes, runtime metadata, methodology, and revision state.
The [latency guide](latency.md#compare-framework-owned-scheduling) documents
the harness and command:

```bash
uv run python perf/bench_framework_latency.py \
  --iterations 30 \
  --warmups 5 \
  --output framework-latency.json
```

This is a normalized framework-pipeline benchmark, not a provider leaderboard
or a claim about full microphone-to-speaker latency. It excludes real network
and provider variance, STT and endpointing, media-server behavior, client
network quality, acoustic playback, and worker startup. A framework can make
different tradeoffs outside this narrow boundary. The scheduled workflow
reruns the same pinned method weekly and uploads an artifact named with its
commit SHA; changing public numbers still requires a reviewed docs update.

## EasyCat's explicit non-goals

EasyCat does not try to be:

- a managed voice-agent cloud, global media network, or autoscaling control
  plane;
- the broadest provider, video/avatar, or native client SDK ecosystem;
- a replacement for the agent framework or workflow code an application
  already uses;
- proof that one framework is universally fastest based on a synthetic,
  sub-millisecond scheduling difference.

Those are deliberate fit boundaries. If managed global WebRTC infrastructure
or a broad multimodal ecosystem is the primary requirement, LiveKit Agents or
Pipecat is likely the stronger starting point. If retaining application-owned
agent code and self-hosted, replayable voice-session evidence is the primary
requirement, EasyCat is the narrower tool designed for that job.
