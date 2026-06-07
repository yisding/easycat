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

from easycat import EasyConfig, require_env
from easycat.transports import run_websocket_config_server


def main() -> None:
    require_env("OPENAI_API_KEY")

    def config(transport):
        from agents import Agent  # type: ignore[import-untyped]

        agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")
        return EasyConfig(transport=transport, agent=agent)

    run_websocket_config_server(config)


if __name__ == "__main__":
    main()
