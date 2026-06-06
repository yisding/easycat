"""WebSocket server example for EasyCat.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra openai --extra openai-agents --group dev
  uv run easycat doctor
  uv run python examples/ws_server.py

Connect clients streaming raw PCM16 audio to ws://localhost:8765.
For non-local deployments, set EASYCAT_WS_TOKEN and send it as:
  Authorization: Bearer <token>
"""

import asyncio

import easycat.transports as t
from easycat import EasyConfig as C
from easycat import create_session as S
from easycat import require_env


def main() -> None:
    api_key = require_env("OPENAI_API_KEY")

    def session(ws):
        from agents import Agent  # type: ignore[import-untyped]

        agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")
        wst = t.WebSocketConnectionTransport(ws)
        return S(C(openai_api_key=api_key, transport=wst, agent=agent))

    asyncio.run(t.serve_websocket_sessions(session, t.websocket_session_server_config_from_env()))


if __name__ == "__main__":
    main()
