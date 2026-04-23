"""Use a remote agent over the OpenAI Responses API (HTTP + SSE).

``RemoteResponsesAPIBridge`` makes the session call an agent that runs on
another service speaking the ``/v1/responses`` protocol — anywhere from
``api.openai.com`` to a self-hosted gateway. It implements the full
``ExternalAgentBridge`` contract, so the session treats it like any
local bridge.

Setup: export OPENAI_API_KEY=...                       # for STT/TTS
       export EASYCAT_REMOTE_AGENT_BASE_URL=https://api.openai.com
       export EASYCAT_REMOTE_AGENT_API_KEY=...         # bearer token
       export EASYCAT_REMOTE_AGENT_MODEL=gpt-4o-mini
       uv sync --extra quickstart
Run:   uv run python examples/responses_api_bridge.py
"""

from easycat import EasyCatConfig, require_env, run
from easycat.integrations.agents import RemoteResponsesAPIBridge

require_env("OPENAI_API_KEY")
base_url = require_env("EASYCAT_REMOTE_AGENT_BASE_URL")
remote_key = require_env("EASYCAT_REMOTE_AGENT_API_KEY")
model = require_env("EASYCAT_REMOTE_AGENT_MODEL")

run(
    EasyCatConfig.mic(
        agent=RemoteResponsesAPIBridge(base_url=base_url, model=model, api_key=remote_key),
        remote_agent_api_key=remote_key,
        agent_model=model,
    )
)
