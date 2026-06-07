"""WebSocket server example for EasyCat.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra openai --extra openai-agents --group dev
  uv run easycat doctor
  uv run easycat doctor --env-file .env  # if keys live in .env
  uv run easycat doctor --env-file .env --json  # for parseable checks
  uv run python examples/ws_server.py
  uv run --env-file .env python examples/ws_server.py  # if keys live in .env

Connect clients streaming raw PCM16 audio to ws://localhost:8765.
For non-local deployments, set EASYCAT_WS_TOKEN and send it as:
  Authorization: Bearer <token>
"""

import asyncio

import easycat.transports
from easycat import EasyConfig, create_session, require_env


def main() -> None:
    api_key = require_env("OPENAI_API_KEY")

    def session(ws):
        from agents import Agent  # type: ignore[import-untyped]

        agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")
        transport = easycat.transports.WebSocketConnectionTransport(ws)
        return create_session(EasyConfig(openai_api_key=api_key, transport=transport, agent=agent))

    config = easycat.transports.websocket_session_server_config_from_env()
    asyncio.run(easycat.transports.serve_websocket_sessions(session, config))


if __name__ == "__main__":
    main()
