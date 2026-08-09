"""Pin a specific noise-reduction backend instead of auto-selection.

``NoiseReducerConfig.backend`` accepts ``"krisp"``, ``"rnnoise"``, or
``"auto"`` (default; tries Krisp → RNNoise → passthrough).

Setup: export OPENAI_API_KEY=...;
       uv sync --extra quickstart --extra rnnoise --group dev
       uv run easycat doctor
       uv run easycat doctor --env-file .env  # if keys live in .env
       uv run easycat doctor --env-file .env --json  # for parseable checks
Run:   uv run python examples/noise_reduction_backends.py --backend rnnoise
       uv run --env-file .env python examples/noise_reduction_backends.py --backend rnnoise
       # Other choices: --backend krisp or --backend auto
"""

from typing import Literal

try:
    from agents import Agent  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "openai-agents is required. For an app, run: "
        "uv add 'easycat[quickstart,rnnoise]'. In this repo, run: "
        "uv sync --extra quickstart --extra rnnoise --group dev"
    ) from exc

from easycat import EasyConfig, run
from easycat.noise_reduction import NoiseReducerConfig, create_noise_reducer

NoiseReductionBackend = Literal["auto", "krisp", "rnnoise"]


def main(backend: NoiseReductionBackend) -> None:
    # Probe so you can see which class actually resolved before the session starts.
    # Close the probe afterwards: Krisp/RNNoise reducers hold native
    # resources (and Krisp may allow only one session at a time), so
    # leaving the probe alive would change ``auto`` fallback behavior
    # when ``run()`` builds its own reducer from the same config.
    probe = create_noise_reducer(NoiseReducerConfig(backend=backend))
    print(f"[noise_reduction_backends] requested={backend!r} built={type(probe).__name__}")
    close = getattr(probe, "close", None)
    if callable(close):
        close()

    run(
        EasyConfig.mic(
            agent=Agent(name="assistant", instructions="You are a helpful voice assistant."),
            noise_reduction=NoiseReducerConfig(backend=backend),
        )
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="auto", choices=["auto", "krisp", "rnnoise"])
    main(parser.parse_args().backend)
