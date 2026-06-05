"""Pin a specific VAD backend instead of letting ``create_vad`` auto-select.

``VADConfig.backend`` accepts ``"silero"``, ``"funasr"``, ``"ten"``,
``"krisp"``, or ``"auto"`` (default).  The auto chain tries Silero →
FunASR → TEN → Krisp in order.  Pin a backend when you want deterministic
behavior across machines (e.g. forcing a single backend in CI), or when a
specific backend is known to work better for your audio.

Pass ``--backend`` to select; the script prints which class was actually
built and then runs a normal local mic/speaker loop with the chosen VAD.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart --group dev                  # bundled Silero ONNX
  uv sync --extra funasr-vad --group dev                  # Python 3.11-3.12
  uv sync --extra quickstart --extra ten-vad --group dev  # separate TEN VAD license
  uv sync --extra silero-vad --group dev                  # bundled Silero ONNX
  uv pip install krisp_audio                              # Krisp SDK
  uv run easycat doctor
  uv run python examples/vad_backends.py --backend silero
  uv run python examples/vad_backends.py --backend funasr
  uv run python examples/vad_backends.py --backend ten
"""

from __future__ import annotations

import argparse
import asyncio

from easycat import (
    EasyConfig,
    attach_runtime_feedback,
    create_session,
    require_env,
    wait_for_shutdown_signal,
)
from easycat.vad import VADConfig, create_vad


async def main(backend: str) -> None:
    api_key = require_env("OPENAI_API_KEY")

    from agents import Agent  # type: ignore[import-untyped]

    vad = create_vad(VADConfig(backend=backend))
    print(f"[vad_backends] requested={backend!r} built={type(vad).__name__}")

    config = EasyConfig.mic(
        openai_api_key=api_key,
        vad=vad,
        agent=Agent(name="assistant", instructions="You are a helpful voice assistant."),
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--backend",
        default="auto",
        choices=["auto", "silero", "funasr", "ten", "krisp"],
        help="VAD backend to pin (default: auto)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.backend))
