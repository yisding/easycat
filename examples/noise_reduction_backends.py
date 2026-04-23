"""Pin a specific noise-reduction backend instead of auto-selection.

``NoiseReducerConfig.backend`` accepts ``"krisp"``, ``"rnnoise"``, or
``"auto"`` (default; tries Krisp → RNNoise → passthrough).  ``EasyCatConfig``
takes the config object directly; ``create_noise_reducer`` is shown up front
so you can see which class actually got built.

Pass ``--backend`` to select:

  uv run python examples/noise_reduction_backends.py --backend rnnoise
  uv run python examples/noise_reduction_backends.py --backend krisp

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart --extra rnnoise   # or set up Krisp SDK separately
"""

from __future__ import annotations

import argparse
import asyncio

from easycat import (
    EasyCatConfig,
    LocalTransportConfig,
    attach_runtime_feedback,
    create_session,
    require_env,
    wait_for_shutdown_signal,
)
from easycat.noise_reduction import NoiseReducerConfig, create_noise_reducer


async def main(backend: str) -> None:
    api_key = require_env("OPENAI_API_KEY")

    # Build the reducer once up front so we can print which backend actually
    # resolved. The session will build a second instance internally from
    # ``noise_reduction=``; this print is purely informational.
    probe = create_noise_reducer(NoiseReducerConfig(backend=backend))
    print(f"[noise_reduction_backends] requested={backend!r} built={type(probe).__name__}")

    from agents import Agent  # type: ignore[import-untyped]

    agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=LocalTransportConfig(),
        agent=agent,
        noise_reduction=NoiseReducerConfig(backend=backend),
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
        choices=["auto", "krisp", "rnnoise"],
        help="Noise-reduction backend to pin (default: auto)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.backend))
