"""Smart-turn endpoint detection — finish turns early via ONNX classifier.

By default ``TurnManager`` waits ``end_of_turn_silence_ms`` (1 s) of silence
before ending the turn. Smart-turn classifies captured audio with a ~8 MB
Whisper-Tiny ONNX model and ends early when confident.

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart --extra smart-turn
Run:   uv run python examples/smart_turn_demo.py
"""

try:
    from agents import Agent  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "openai-agents is required. Install with: uv sync --extra quickstart"
    ) from exc

from easycat import EasyConfig, SmartTurnConfig, run

run(
    EasyConfig.mic(
        agent=Agent(name="assistant", instructions="You are a helpful voice assistant."),
        smart_turn=SmartTurnConfig(enabled=True, threshold=0.5),
    )
)
