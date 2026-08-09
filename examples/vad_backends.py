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
  uv sync --extra funasr-vad --group dev                  # bundled FunASR ONNX
  uv sync --extra quickstart --extra ten-vad --group dev  # separate TEN VAD license
  uv sync --extra silero-vad --group dev                  # bundled Silero ONNX
  uv pip install krisp_audio                              # Krisp SDK
  uv run easycat doctor
  uv run easycat doctor --env-file .env  # if keys live in .env
  uv run easycat doctor --env-file .env --json  # for parseable checks
  uv run python examples/vad_backends.py --backend silero
  uv run --env-file .env python examples/vad_backends.py --backend silero  # if keys live in .env
  uv run python examples/vad_backends.py --backend funasr
  uv run python examples/vad_backends.py --backend ten
"""

from __future__ import annotations

import argparse
from typing import Literal

try:
    from agents import Agent  # type: ignore[import-untyped]
except ImportError:
    Agent = None  # type: ignore[assignment]

from easycat import EasyConfig, run
from easycat.vad import VADConfig, create_vad

BACKENDS = ("auto", "silero", "funasr", "ten", "krisp")


def main(backend: Literal["auto", "silero", "funasr", "ten", "krisp"]) -> None:
    if Agent is None:
        raise SystemExit(
            "openai-agents is required. For an app, run: "
            "uv add 'easycat[quickstart]'. In this repo, run: "
            "uv sync --extra quickstart --group dev"
        )

    vad_config = VADConfig(backend=backend)
    config = EasyConfig.mic(
        vad=vad_config,
        agent=Agent(name="assistant", instructions="You are a helpful voice assistant."),
    )
    probe = create_vad(vad_config)
    print(f"[vad_backends] requested={backend!r} built={type(probe).__name__}")
    close = getattr(probe, "close", None)
    if callable(close):
        close()

    run(config)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="auto", choices=BACKENDS)
    main(parser.parse_args().backend)
