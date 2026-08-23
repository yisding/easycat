# EasyCat Examples

Use this directory as a runnable map after reading the top-level
[README](../README.md) or the [teaching ladder](../docs/teaching/). Each row
below names what the example teaches, the command to run, the extras or
third-party packages it expects, and the environment variables that must be set.
Run `uv run easycat docs` for the maintained docs map and
`uv run easycat doctor` for first-run environment checks.
Coding agent? Use the root [AGENTS.md](../AGENTS.md) for repository coding
rules; use [llms.txt](../llms.txt) for machine-readable docs route discovery or
run `uv run easycat explain json-schema` (`uv run easycat doctor --json` and
`uv run easycat doctor --env-file .env --json` emit the same checks as
parseable rows).
To scaffold a fresh app instead of running an example in-place, compare
templates with `uv run easycat init --list-templates`; the list includes
copyable create/preflight/check/fix/docs/json-schema/run commands for local mic, browser
WebRTC, Twilio, Telnyx, and text-chat starting points. Then create the default local
voice starter with `uv run easycat init my-agent`. Use
`uv run easycat init --list-templates --json` when automation needs the same
template catalog.

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
`uv run easycat validate report .easycat/validation/latest.json`. Each also
takes `--json` (`uv run easycat validate quick --json`,
`uv run easycat validate report .easycat/validation/latest.json --json`) to
emit the run or saved report inside the standard CLI envelope.

`quickstart` includes local audio, OpenAI providers, the OpenAI Agents SDK,
NumPy, ONNX Runtime, and LiveKit AEC3 echo cancellation. RNNoise is opt-in
through the `rnnoise` extra. It does not install every framework/provider
variant. Install cells start with a complete repo-local `uv sync` command.
After a semicolon, `--extra <name>` snippets extend that sync command; packages
such as `krisp_audio` are installed in the same environment with `uv pip install`.

## Choose An Example

| You need | Start with | Why |
| --- | --- | --- |
| No API keys | [journal_demo.py](journal_demo.py) or [telephony_helpers.py](telephony_helpers.py) | Both run offline; one teaches journal records, the other teaches telephony helper logic. |
| First local voice bot | [openai_agents_voice.py](openai_agents_voice.py) | It matches the README quickstart and `quickstart` extra. |
| Your preferred agent framework | [pydantic_ai_voice.py](pydantic_ai_voice.py), [langchain_voice.py](langchain_voice.py), or [langgraph_voice.py](langgraph_voice.py) | These show framework-specific bridge wiring with the same voice loop shape. |
| Browser or server transport | [ws_browser_example.py](ws_browser_example.py) or [webrtc_server.py](webrtc_server.py) | These move the same voice loop into browser/WebSocket/WebRTC surfaces. |
| Provider comparison | [deepgram_voice.py](deepgram_voice.py), [elevenlabs_voice.py](elevenlabs_voice.py), or [combined_providers.py](combined_providers.py) | These make provider extras and required API keys explicit. |
| Debugging and replay | [debug_bundle.py](debug_bundle.py) or [journal_ui.py](journal_ui.py) | These show `RunBundle` export, journal inspection, and the debugger UI. |

## Core Voice Loops

| Example | Use When | Run | Install | Env |
| --- | --- | --- | --- | --- |
| [voice_app.py](voice_app.py) | App-first VoiceApp with one-word mode switching (browser/local/websocket/twilio). | `uv run python examples/voice_app.py` | `uv sync --extra quickstart --extra webrtc --group dev` | `OPENAI_API_KEY` |
| [voice_app_twilio.py](voice_app_twilio.py) | App-first Twilio phone bot via `VoiceApp.run("twilio")` and the reusable `easycat.telephony.server` helper. | `uv run python examples/voice_app_twilio.py` | `uv sync --extra openai --extra telephony --extra openai-agents --group dev` | `OPENAI_API_KEY`, `TWILIO_STREAM_URL`, `TWILIO_AUTH_TOKEN`; optional `TWILIO_STREAM_TOKEN_SECRET`, `TRUST_PROXY_HEADERS`, `TWILIO_PUBLIC_TWIML_URL` |
| [voice_app_telnyx.py](voice_app_telnyx.py) | App-first Telnyx phone bot via `VoiceApp.run("telnyx")` and the reusable `easycat.telephony.telnyx_server` helper. | `uv run python examples/voice_app_telnyx.py` | `uv sync --extra openai --extra telnyx --extra openai-agents --group dev` | `OPENAI_API_KEY`, `TELNYX_STREAM_URL`, `TELNYX_API_KEY`, `TELNYX_PUBLIC_KEY` |
| [openai_agents_voice.py](openai_agents_voice.py) | First local mic/speaker bot with OpenAI Agents SDK. | `uv run python examples/openai_agents_voice.py` | `uv sync --extra quickstart --group dev` | `OPENAI_API_KEY` |
| [pydantic_ai_voice.py](pydantic_ai_voice.py) | Single-agent PydanticAI voice bot. | `uv run python examples/pydantic_ai_voice.py` | `uv sync --extra quickstart --extra pydantic-ai --group dev` | `OPENAI_API_KEY` |
| [pydantic_ai_workflow_voice.py](pydantic_ai_workflow_voice.py) | Workflow-level PydanticAI hand-off across turns. | `uv run python examples/pydantic_ai_workflow_voice.py` | `uv sync --extra quickstart --extra pydantic-ai --group dev` | `OPENAI_API_KEY` |
| [langchain_voice.py](langchain_voice.py) | LangChain 1.x LCEL runnable bridged into voice. | `uv run python examples/langchain_voice.py` | `uv sync --extra quickstart --extra langchain --group dev` | `OPENAI_API_KEY` |
| [langgraph_voice.py](langgraph_voice.py) | LangGraph state graph bridged into voice. | `uv run python examples/langgraph_voice.py` | `uv sync --extra quickstart --extra langgraph --group dev`; `langchain-openai` | `OPENAI_API_KEY` |

## Agent Tools And Session Actions

| Example | Use When | Run | Install | Env |
| --- | --- | --- | --- | --- |
| [function_tools_openai.py](function_tools_openai.py) | OpenAI Agents SDK function tools. | `uv run python examples/function_tools_openai.py` | `uv sync --extra quickstart --group dev` | `OPENAI_API_KEY` |
| [function_tools_pydantic.py](function_tools_pydantic.py) | PydanticAI function tools. | `uv run python examples/function_tools_pydantic.py` | `uv sync --extra quickstart --extra pydantic-ai --group dev` | `OPENAI_API_KEY` |
| [function_tools_langchain.py](function_tools_langchain.py) | LangChain 0.3 `AgentExecutor` tools. | `uv run python examples/function_tools_langchain.py` | `uv sync --extra quickstart --extra langchain-v0 --group dev` | `OPENAI_API_KEY` |
| [function_tools_langgraph.py](function_tools_langgraph.py) | LangGraph ReAct tools. | `uv run python examples/function_tools_langgraph.py` | `uv sync --extra quickstart --extra langgraph --group dev`; `langchain-openai` | `OPENAI_API_KEY` |
| [session_actions_openai.py](session_actions_openai.py) | OpenAI tool that queues EasyCat session actions. | `uv run python examples/session_actions_openai.py` | `uv sync --extra quickstart --group dev` | `OPENAI_API_KEY` |
| [session_actions_pydantic.py](session_actions_pydantic.py) | PydanticAI tool that queues session actions through deps. | `uv run python examples/session_actions_pydantic.py` | `uv sync --extra quickstart --extra pydantic-ai --group dev` | `OPENAI_API_KEY`, optional `PYDANTIC_AI_MODEL` |
| [session_actions_langchain.py](session_actions_langchain.py) | LangChain 0.3 tool that can end the current session. | `uv run python examples/session_actions_langchain.py` | `uv sync --extra quickstart --extra langchain-v0 --group dev` | `OPENAI_API_KEY` |
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
| [ws_server.py](ws_server.py) | Multi-session WebSocket server for raw PCM clients. | `uv run python examples/ws_server.py` | `uv sync --extra openai --extra openai-agents --group dev` | `OPENAI_API_KEY`; optional `EASYCAT_WS_TOKEN`, `EASYCAT_WS_ALLOW_QUERY_TOKEN` |
| [ws_browser_example.py](ws_browser_example.py) | Browser mic/speaker over WebSocket with local static serving. | `uv run python examples/ws_browser_example.py` | `uv sync --extra openai --extra openai-agents --group dev` | `OPENAI_API_KEY` |
| [ws_supervisor_server.py](ws_supervisor_server.py) | Browser caller plus passive supervisor listen-in. | `uv run python examples/ws_supervisor_server.py` | `uv sync --extra openai --extra openai-agents --group dev` | `OPENAI_API_KEY`, `EASYCAT_SUPERVISOR_TOKEN`; optional `EASYCAT_WS_CALLER_HOST`, `EASYCAT_WS_SUPERVISOR_HOST` |
| [reconnecting_ws_client.py](reconnecting_ws_client.py) | Reconnecting client against `ws_server.py`. | `uv run python examples/reconnecting_ws_client.py` | `uv sync --extra openai --extra openai-agents --group dev`; run `ws_server.py` separately | None for client; `OPENAI_API_KEY` for paired `ws_server.py` |
| [webrtc_server.py](webrtc_server.py) | Multi-client WebRTC voice chat with bundled browser client. | `uv run python examples/webrtc_server.py` | `uv sync --extra openai --extra openai-agents --extra webrtc --group dev` | `OPENAI_API_KEY`; optional `TURN_SERVER_URL`, `TURN_USERNAME`, `TURN_CREDENTIAL`, `WEBRTC_EXPOSE_ICE_CREDENTIALS`, `WEBRTC_SIGNALING_TOKEN`, `WEBRTC_MAX_SESSIONS`, `SIGNALING_HOST`, `SIGNALING_PORT` |
| [webrtc_observability_server.py](webrtc_observability_server.py) | WebRTC plus debugger UI in one browser page. | `uv run python examples/webrtc_observability_server.py` | `uv sync --extra openai --extra openai-agents --extra webrtc --extra debugger --group dev` | `OPENAI_API_KEY`; optional `TURN_SERVER_URL`, `TURN_USERNAME`, `TURN_CREDENTIAL`, `WEBRTC_EXPOSE_ICE_CREDENTIALS`, `WEBRTC_SIGNALING_TOKEN`, `SIGNALING_HOST`, `SIGNALING_PORT`, `DASHBOARD_HOST`, `DASHBOARD_PORT` |
| [webtransport_server.py](webtransport_server.py) | Multi-client WebTransport server. | `uv run python examples/webtransport_server.py --cert cert.pem --key key.pem` | `uv sync --extra openai --extra openai-agents --extra webtransport --group dev` | `OPENAI_API_KEY`; optional `EASYCAT_SERVE_TOKEN`; local TLS cert/key files |
| [twilio_app.py](twilio_app.py) | Lower-level FastAPI Twilio Media Streams reference (outbound calls, status callbacks, SMS); the reusable two-listener helper now lives in `easycat.telephony.server`. | `uv run uvicorn examples.twilio_app:create_app --factory --host 0.0.0.0` | `uv sync --extra openai --extra telephony --extra telephony-fastapi --extra openai-agents --group dev` | `OPENAI_API_KEY`, `TWILIO_STREAM_URL`, `TWILIO_AUTH_TOKEN`; optional `TWILIO_ACCOUNT_SID`, `TWILIO_VOICE_FROM`, `TWILIO_TWIML_URL`, `TWILIO_STATUS_CALLBACK_URL`, `TWILIO_CALL_API_TOKEN`, `TWILIO_SMS_FROM`, `TWILIO_STREAM_TOKEN_SECRET`, `TWILIO_MAX_SESSIONS`, `TWILIO_DRAIN_TIMEOUT_S`, `TWILIO_FORCE_SHUTDOWN_TIMEOUT_S` |
| [telnyx_app.py](telnyx_app.py) | Lower-level FastAPI Telnyx Call Control reference (Ed25519-verified webhook, outbound calls, status callbacks); the reusable two-listener helper lives in `easycat.telephony.telnyx_server`. | `uv run uvicorn examples.telnyx_app:create_app --factory --host 0.0.0.0` | `uv sync --extra openai --extra telnyx --extra telephony-fastapi --extra openai-agents --group dev` | `OPENAI_API_KEY`, `TELNYX_STREAM_URL`, `TELNYX_API_KEY`, `TELNYX_PUBLIC_KEY`; optional `TELNYX_CONNECTION_ID`, `TELNYX_VOICE_FROM`, `TELNYX_WEBHOOK_URL`, `TELNYX_CALL_API_TOKEN`, `TELNYX_STREAM_TOKEN_SECRET`, `TELNYX_MAX_SESSIONS`, `TELNYX_WS_PORT`, `TELNYX_START_TIMEOUT_S`, `TELNYX_DRAIN_TIMEOUT_S`, `TELNYX_FORCE_SHUTDOWN_TIMEOUT_S` |

Support files:

- [ws_browser_client.html](ws_browser_client.html): static browser client for
  `ws_browser_example.py`.
- [ws_supervisor_client.html](ws_supervisor_client.html): browser caller and
  supervisor client for `ws_supervisor_server.py`.
- [webrtc_static/webrtc_client.html](webrtc_static/webrtc_client.html): compact
  browser client served by `webrtc_observability_server.py`; `webrtc_server.py`
  uses the richer client bundled with the EasyCat package.
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
| [vad_backends.py](vad_backends.py) | Pin VAD backend (`silero`, `funasr`, `ten`, `krisp`, or `auto`). | `uv run python examples/vad_backends.py --backend silero` | `uv sync --extra quickstart --group dev`; `--extra funasr-vad`, `--extra ten-vad`, or `uv pip install krisp_audio` for those backends | `OPENAI_API_KEY` |
| [noise_reduction_backends.py](noise_reduction_backends.py) | Pin noise-reduction backend (`rnnoise`, `krisp`, or `auto`). | `uv run python examples/noise_reduction_backends.py --backend rnnoise` | `uv sync --extra quickstart --extra rnnoise --group dev`; `uv pip install krisp_audio` for Krisp | `OPENAI_API_KEY` |
| [echo_cancellation.py](echo_cancellation.py) | Enable LiveKit AEC3 on local mic/speaker. | `uv run python examples/echo_cancellation.py` | `uv sync --extra quickstart --group dev` | `OPENAI_API_KEY` |
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
| [custom_transport.py](custom_transport.py) | Wrap or replace the audio transport by hand. | `uv run python examples/custom_transport.py` | `uv sync --extra quickstart --group dev` | `OPENAI_API_KEY` |
| [responses_api_bridge.py](responses_api_bridge.py) | Call a remote agent over the OpenAI Responses API protocol. | `uv run python examples/responses_api_bridge.py` | `uv sync --extra quickstart --group dev` | `OPENAI_API_KEY`, `EASYCAT_REMOTE_AGENT_BASE_URL`, `EASYCAT_REMOTE_AGENT_API_KEY`, `EASYCAT_REMOTE_AGENT_MODEL` |

## Telephony Helpers

| Example | Use When | Run | Install | Env |
| --- | --- | --- | --- | --- |
| [telephony_helpers.py](telephony_helpers.py) | Learn DTMF aggregation, voicemail detection, and IVR text classifiers offline. | `uv run python examples/telephony_helpers.py` | `uv sync --group dev` or base install | None |
