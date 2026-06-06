# EasyCat Examples

Use this directory as a runnable map after reading the top-level
[README](../README.md) or the [teaching ladder](../docs/teaching/). Each row
below names what the example teaches, the command to run, the extras or
third-party packages it expects, and the environment variables that must be set.
Run `uv run easycat docs` for the maintained docs map; use
`uv run easycat docs --json` when a script or coding agent needs the same route
map with command hints and audience labels. Replace uppercase placeholders such
as `PATH` before running those hints. Use
`uv run easycat explain json-schema` for the JSON envelope and field contract.
To scaffold a fresh app instead of running an example in-place, compare
templates with `uv run easycat init --list-templates`; the list includes
copyable create/check/run commands for local mic, browser WebRTC, Twilio, and
text-chat starting points. Use `uv run easycat init --list-templates --json`
when a script or coding agent needs the same template catalog.

For the fastest local mic/speaker path:

```bash
uv sync --extra quickstart --group dev
export OPENAI_API_KEY="your-api-key"
uv run easycat doctor
uv run python examples/openai_agents_voice.py
```

If you keep keys in `.env`, load that file on both the doctor and run commands:

```bash
uv run easycat doctor --env-file .env
uv run --env-file .env python examples/openai_agents_voice.py
```

After changing an example or using one as a starting point, run
`uv run easycat validate quick` and inspect
`uv run easycat validate report .easycat/validation/latest.json`.

`quickstart` includes local audio, OpenAI providers, the OpenAI Agents SDK,
RNNoise, NumPy, and ONNX Runtime. It does not install every framework/provider
variant. Install cells are repo-local commands that start with EasyCat extras,
such as `uv sync --extra quickstart --group dev`; anything after a semicolon is
an additional third-party package to install in the same environment with
`uv pip install`.

## Core Voice Loops

| Example | Use When | Run | Install | Env |
| --- | --- | --- | --- | --- |
| [openai_agents_voice.py](openai_agents_voice.py) | First local mic/speaker bot with OpenAI Agents SDK. | `uv run python examples/openai_agents_voice.py` | `uv sync --extra quickstart --group dev` | `OPENAI_API_KEY` |
| [pydantic_ai_voice.py](pydantic_ai_voice.py) | Single-agent PydanticAI voice bot. | `uv run python examples/pydantic_ai_voice.py` | `uv sync --extra quickstart --extra pydantic-ai --group dev` | `OPENAI_API_KEY` |
| [pydantic_ai_workflow_voice.py](pydantic_ai_workflow_voice.py) | Workflow-level PydanticAI hand-off across turns. | `uv run python examples/pydantic_ai_workflow_voice.py` | `uv sync --extra quickstart --extra pydantic-ai --group dev` | `OPENAI_API_KEY` |
| [langchain_voice.py](langchain_voice.py) | LangChain LCEL runnable bridged into voice. | `uv run python examples/langchain_voice.py` | `uv sync --extra quickstart --extra langchain --group dev`; `langchain-openai` | `OPENAI_API_KEY` |
| [langgraph_voice.py](langgraph_voice.py) | LangGraph state graph bridged into voice. | `uv run python examples/langgraph_voice.py` | `uv sync --extra quickstart --extra langgraph --group dev`; `langchain-openai` | `OPENAI_API_KEY` |

## Agent Tools And Session Actions

| Example | Use When | Run | Install | Env |
| --- | --- | --- | --- | --- |
| [function_tools_openai.py](function_tools_openai.py) | OpenAI Agents SDK function tools. | `uv run python examples/function_tools_openai.py` | `uv sync --extra quickstart --group dev` | `OPENAI_API_KEY` |
| [function_tools_pydantic.py](function_tools_pydantic.py) | PydanticAI function tools. | `uv run python examples/function_tools_pydantic.py` | `uv sync --extra quickstart --extra pydantic-ai --group dev` | `OPENAI_API_KEY` |
| [function_tools_langchain.py](function_tools_langchain.py) | LangChain `AgentExecutor` tools. | `uv run python examples/function_tools_langchain.py` | `uv sync --extra quickstart --extra langchain --group dev`; `langchain<1`, `langchain-openai` | `OPENAI_API_KEY` |
| [function_tools_langgraph.py](function_tools_langgraph.py) | LangGraph ReAct tools. | `uv run python examples/function_tools_langgraph.py` | `uv sync --extra quickstart --extra langgraph --group dev`; `langchain-openai` | `OPENAI_API_KEY` |
| [session_actions_openai.py](session_actions_openai.py) | OpenAI tool that queues EasyCat session actions. | `uv run python examples/session_actions_openai.py` | `uv sync --extra quickstart --group dev` | `OPENAI_API_KEY` |
| [session_actions_pydantic.py](session_actions_pydantic.py) | PydanticAI tool that queues session actions through deps. | `uv run python examples/session_actions_pydantic.py` | `uv sync --extra quickstart --extra pydantic-ai --group dev` | `OPENAI_API_KEY` |
| [session_actions_langchain.py](session_actions_langchain.py) | LangChain tool that can end the current session. | `uv run python examples/session_actions_langchain.py` | `uv sync --extra quickstart --extra langchain --group dev`; `langchain<1`, `langchain-openai` | `OPENAI_API_KEY` |
| [session_actions_langgraph.py](session_actions_langgraph.py) | LangGraph tool that can end the current session. | `uv run python examples/session_actions_langgraph.py` | `uv sync --extra quickstart --extra langgraph --group dev`; `langchain-openai` | `OPENAI_API_KEY` |
| [agent_event_subscription.py](agent_event_subscription.py) | Subscribe to agent deltas and tool-call events from the session. | `uv run python examples/agent_event_subscription.py` | `uv sync --extra quickstart --group dev` | `OPENAI_API_KEY` |

## Provider Swaps

| Example | Use When | Run | Install | Env |
| --- | --- | --- | --- | --- |
| [deepgram_voice.py](deepgram_voice.py) | Deepgram STT and TTS. | `uv run python examples/deepgram_voice.py` | `uv sync --extra quickstart --extra deepgram --group dev` | `OPENAI_API_KEY`, `DEEPGRAM_API_KEY` |
| [elevenlabs_voice.py](elevenlabs_voice.py) | ElevenLabs STT and TTS. | `uv run python examples/elevenlabs_voice.py` | `uv sync --extra quickstart --extra elevenlabs --group dev` | `OPENAI_API_KEY`, `ELEVENLABS_API_KEY` |
| [cartesia_voice.py](cartesia_voice.py) | Cartesia STT and TTS. | `uv run python examples/cartesia_voice.py` | `uv sync --extra quickstart --extra cartesia --group dev` | `OPENAI_API_KEY`, `CARTESIA_API_KEY` |
| [combined_providers.py](combined_providers.py) | Mix Deepgram STT with ElevenLabs TTS. | `uv run python examples/combined_providers.py` | `uv sync --extra quickstart --extra deepgram --extra elevenlabs --group dev` | `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY` |

## Transports And Browser Clients

| Example | Use When | Run | Install | Env |
| --- | --- | --- | --- | --- |
| [ws_server.py](ws_server.py) | Multi-session WebSocket server for raw PCM clients. | `uv run python examples/ws_server.py` | `uv sync --extra openai --extra openai-agents --group dev` | `OPENAI_API_KEY`; optional `EASYCAT_WS_TOKEN` |
| [ws_browser_example.py](ws_browser_example.py) | Browser mic/speaker over WebSocket with local static serving. | `uv run python examples/ws_browser_example.py` | `uv sync --extra openai --extra openai-agents --group dev` | `OPENAI_API_KEY` |
| [ws_supervisor_server.py](ws_supervisor_server.py) | Browser caller plus passive supervisor listen-in. | `uv run python examples/ws_supervisor_server.py` | `uv sync --extra openai --extra openai-agents --group dev` | `OPENAI_API_KEY` |
| [reconnecting_ws_client.py](reconnecting_ws_client.py) | Reconnecting client against `ws_server.py`. | `uv run python examples/reconnecting_ws_client.py` | `uv sync --extra openai --extra openai-agents --group dev`; run `ws_server.py` separately | None for client; `OPENAI_API_KEY` for paired `ws_server.py` |
| [webrtc_server.py](webrtc_server.py) | WebRTC voice chat with bundled browser client. | `uv run python examples/webrtc_server.py` | `uv sync --extra openai --extra openai-agents --extra webrtc --group dev` | `OPENAI_API_KEY`; optional `TURN_SERVER_URL`, `TURN_USERNAME`, `TURN_CREDENTIAL`, `WEBRTC_EXPOSE_ICE_CREDENTIALS` |
| [webrtc_observability_server.py](webrtc_observability_server.py) | WebRTC plus debugger UI in one browser page. | `uv run python examples/webrtc_observability_server.py` | `uv sync --extra openai --extra openai-agents --extra webrtc --extra debugger --group dev` | `OPENAI_API_KEY`; optional TURN vars and `WEBRTC_EXPOSE_ICE_CREDENTIALS` |
| [webtransport_server.py](webtransport_server.py) | Multi-client WebTransport server. | `uv run python examples/webtransport_server.py --cert cert.pem --key key.pem` | `uv sync --extra openai --extra openai-agents --extra webtransport --group dev` | `OPENAI_API_KEY`; local TLS cert/key files |
| [twilio_app.py](twilio_app.py) | Twilio Media Streams with per-call EasyCat sessions. | `uv run uvicorn examples.twilio_app:create_app --factory --host 0.0.0.0 --port 8000` | `uv sync --extra openai --extra telephony --extra openai-agents --group dev` | `OPENAI_API_KEY`, `TWILIO_STREAM_URL`; optional Twilio call/SMS vars |

Support files:

- [ws_browser_client.html](ws_browser_client.html): static browser client for
  `ws_browser_example.py`.
- [ws_supervisor_client.html](ws_supervisor_client.html): browser caller and
  supervisor client for `ws_supervisor_server.py`.
- [webrtc_static/webrtc_client.html](webrtc_static/webrtc_client.html): browser
  client served by `webrtc_server.py`.
- [webrtc_static/webrtc_observability.html](webrtc_static/webrtc_observability.html):
  combined WebRTC/debugger page served by `webrtc_observability_server.py`.
- [webtransport_browser_client.html](webtransport_browser_client.html): browser
  client for `webtransport_server.py`.
- [ec2_webrtc/deploy.sh](ec2_webrtc/deploy.sh): EC2/coturn deployment helper
  for WebRTC deployments.
- [ec2_webrtc/coturn.conf](ec2_webrtc/coturn.conf): coturn configuration
  template installed by `ec2_webrtc/deploy.sh`.
- [ec2_webrtc/easycat-webrtc.service](ec2_webrtc/easycat-webrtc.service):
  systemd unit template installed by `ec2_webrtc/deploy.sh`.

## Turn-Taking, Audio, And Output Controls

| Example | Use When | Run | Install | Env |
| --- | --- | --- | --- | --- |
| [push_to_talk.py](push_to_talk.py) | Manually call `start_turn()` / `end_turn()` instead of VAD. | `uv run python examples/push_to_talk.py` | `uv sync --extra quickstart --group dev` | `OPENAI_API_KEY` |
| [smart_turn_demo.py](smart_turn_demo.py) | ONNX endpoint classifier for faster turn completion. | `uv run python examples/smart_turn_demo.py` | `uv sync --extra quickstart --group dev` | `OPENAI_API_KEY` |
| [vad_backends.py](vad_backends.py) | Pin VAD backend (`silero`, `funasr`, `ten`, `krisp`, or `auto`). | `uv run python examples/vad_backends.py --backend silero` | `uv sync --extra quickstart --group dev`; `--extra funasr-vad` on Python 3.11-3.12, `--extra ten-vad`, or `uv pip install krisp_audio` for those backends | `OPENAI_API_KEY` |
| [noise_reduction_backends.py](noise_reduction_backends.py) | Pin noise-reduction backend (`rnnoise`, `krisp`, or `auto`). | `uv run python examples/noise_reduction_backends.py --backend rnnoise` | `uv sync --extra quickstart --group dev`; `uv pip install krisp_audio` for Krisp | `OPENAI_API_KEY` |
| [echo_cancellation.py](echo_cancellation.py) | Enable LiveKit AEC3 on local mic/speaker. | `uv run python examples/echo_cancellation.py` | `uv sync --extra quickstart --extra aec --group dev` | `OPENAI_API_KEY` |
| [output_processors.py](output_processors.py) | Pre-TTS pronunciation and pacing processors. | `uv run python examples/output_processors.py` | `uv sync --extra quickstart --group dev` | `OPENAI_API_KEY` |

## Debugging, Journals, And Custom Providers

| Example | Use When | Run | Install | Env |
| --- | --- | --- | --- | --- |
| [journal_demo.py](journal_demo.py) | Inspect one synthetic turn and its journal records. | `uv run python examples/journal_demo.py` | `uv sync --group dev` or base install | None |
| [journal_ui.py](journal_ui.py) | Tail a live mic session in the debugger UI. | `uv run python examples/journal_ui.py` | `uv sync --extra quickstart --extra debugger --group dev` | `OPENAI_API_KEY` |
| [debug_bundle.py](debug_bundle.py) | Export and inspect a `RunBundle`. | `uv run python examples/debug_bundle.py` | `uv sync --extra quickstart --group dev` | `OPENAI_API_KEY` |
| [custom_stt_provider.py](custom_stt_provider.py) | Wrap or replace the STT provider by hand. | `uv run python examples/custom_stt_provider.py` | `uv sync --extra quickstart --group dev` | `OPENAI_API_KEY` |
| [custom_tts_provider.py](custom_tts_provider.py) | Wrap or replace the TTS provider by hand. | `uv run python examples/custom_tts_provider.py` | `uv sync --extra quickstart --group dev` | `OPENAI_API_KEY` |
| [custom_vad_provider.py](custom_vad_provider.py) | Wrap or replace the VAD provider by hand. | `uv run python examples/custom_vad_provider.py` | `uv sync --extra quickstart --group dev` | `OPENAI_API_KEY` |
| [responses_api_bridge.py](responses_api_bridge.py) | Call a remote agent over the OpenAI Responses API protocol. | `uv run python examples/responses_api_bridge.py` | `uv sync --extra quickstart --group dev` | `OPENAI_API_KEY`, `EASYCAT_REMOTE_AGENT_BASE_URL`, `EASYCAT_REMOTE_AGENT_API_KEY`, `EASYCAT_REMOTE_AGENT_MODEL` |

## Telephony Helpers

| Example | Use When | Run | Install | Env |
| --- | --- | --- | --- | --- |
| [telephony_helpers.py](telephony_helpers.py) | Learn DTMF aggregation, voicemail detection, and IVR text classifiers offline. | `uv run python examples/telephony_helpers.py` | `uv sync --extra quickstart --group dev` | None |
