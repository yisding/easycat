"""Pin a specific VAD backend instead of letting ``create_vad`` auto-select.

``VADConfig.backend`` accepts ``"silero"``, ``"funasr"``, ``"ten"``,
``"krisp"``, or ``"auto"`` (default).  The auto chain tries Silero →
FunASR → TEN → Krisp in order.  Pin a backend when you want deterministic
behavior across machines (e.g. CI without torch installed), or when a
specific backend is known to work better for your audio.

Pass ``--backend`` to select; the script prints which class was actually
built and then runs a normal local mic/speaker loop with the chosen VAD.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart                        # silero + funasr + krisp
  uv sync --extra quickstart --extra ten-vad        # adds TEN VAD (separate license)
  uv pip install torch                              # required for Silero
  uv run python examples/vad_backends.py --backend silero
  uv run python examples/vad_backends.py --backend ten
"""

from __future__ import annotations

import argparse
import asyncio

from easycat import (
    AgentRunner,
    AgentRunnerConfig,
    Session,
    SessionConfig,
    attach_runtime_feedback,
    require_env,
    wait_for_shutdown_signal,
)
from easycat.events import EventBus
from easycat.integrations.agents import auto_adapt_agent
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTT, OpenAIRealtimeSTTConfig
from easycat.transports.local import LocalTransport, LocalTransportConfig
from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig
from easycat.vad import VADConfig, create_vad


async def main(backend: str) -> None:
    api_key = require_env("OPENAI_API_KEY")

    from agents import Agent  # type: ignore[import-untyped]

    vad = create_vad(VADConfig(backend=backend))
    print(f"[vad_backends] requested={backend!r} built={type(vad).__name__}")

    event_bus = EventBus()
    stt = OpenAIRealtimeSTT(OpenAIRealtimeSTTConfig(api_key=api_key, event_bus=event_bus))
    tts = OpenAITTS(OpenAITTSConfig(api_key=api_key))
    transport = LocalTransport(LocalTransportConfig())

    base_agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")
    agent = AgentRunner(auto_adapt_agent(base_agent), AgentRunnerConfig())

    config = SessionConfig(
        transport=transport,
        vad=vad,
        stt=stt,
        tts=tts,
        agent=agent,
        event_bus=event_bus,
    )
    session = Session(config)
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
