"""VoicePipeline — plug voice into any OpenAI Agents SDK or PydanticAI agent.

Build your agent idiomatically with the framework of your choice, then
hand it to :class:`VoicePipeline` to get a fully wired voice
conversation::

    # OpenAI Agents SDK
    from agents import Agent
    from easycat import VoicePipeline

    agent = Agent(name="Assistant", instructions="You are helpful.")
    pipeline = VoicePipeline(agent)
    await pipeline.run()

    # PydanticAI
    from pydantic_ai import Agent
    from easycat import VoicePipeline

    agent = Agent("openai:gpt-4o", system_prompt="You are helpful.")
    pipeline = VoicePipeline(agent)
    await pipeline.run()

The pipeline auto-detects the agent framework and wires up STT, TTS,
VAD, noise reduction, and transport around it.  All voice components
are optional and fall back to sensible defaults (or no-op stubs for
testing).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from easycat.events import EventBus
from easycat.metrics import MetricsCollector
from easycat.session import Session, SessionConfig, TurnState
from easycat.timeouts import TimeoutConfig
from easycat.tracing import Tracer
from easycat.turn_manager import TurnManagerConfig

logger = logging.getLogger(__name__)


# ── Framework detection ───────────────────────────────────────────────


def _is_openai_agents_sdk(agent: Any) -> bool:
    """Return True if *agent* looks like an ``agents.Agent`` instance."""
    module = type(agent).__module__ or ""
    if module.startswith("agents"):
        return True
    # Fallback heuristic: agents.Agent always has `name` + `instructions`
    # but doesn't have PydanticAI's `iter` method.
    return (
        hasattr(agent, "name")
        and hasattr(agent, "instructions")
        and not hasattr(agent, "iter")
    )


def _is_pydantic_ai(agent: Any) -> bool:
    """Return True if *agent* looks like a ``pydantic_ai.Agent`` instance."""
    module = type(agent).__module__ or ""
    if module.startswith("pydantic_ai"):
        return True
    # Fallback: PydanticAI agents expose iter() or run_stream()
    return hasattr(agent, "iter") or hasattr(agent, "run_stream")


def _wrap_agent(
    agent: Any,
    *,
    # OpenAI Agents SDK
    run_config: Any = None,
    context: Any = None,
    # PydanticAI
    deps: Any = None,
    model_settings: Any = None,
) -> Any:
    """Auto-detect the agent framework and return a wrapped adapter.

    If the agent is already an EasyCat adapter or satisfies the basic
    ``Agent`` protocol (has a ``run`` method), it is returned as-is.
    """
    # Already wrapped
    from easycat.agents.base import BaseAgentAdapter

    if isinstance(agent, BaseAgentAdapter):
        return agent

    # OpenAI Agents SDK
    if _is_openai_agents_sdk(agent):
        from easycat.agents.openai_agents import OpenAIAgentsAdapter

        return OpenAIAgentsAdapter(
            agent,
            run_config=run_config,
            context=context,
        )

    # PydanticAI
    if _is_pydantic_ai(agent):
        from easycat.agents.pydantic_ai import PydanticAIAdapter

        return PydanticAIAdapter(
            agent,
            deps=deps,
            model_settings=model_settings,
        )

    # Plain callable / duck-typed agent — pass through
    if hasattr(agent, "run"):
        return agent

    raise TypeError(
        f"Cannot detect agent framework for {type(agent).__qualname__}. "
        "Expected an agents.Agent (OpenAI Agents SDK), pydantic_ai.Agent, "
        "or any object with a run(text) -> str method."
    )


# ── VoicePipeline ────────────────────────────────────────────────────


class VoicePipeline:
    """Add real-time voice to any agent.

    Accepts a raw ``agents.Agent`` (OpenAI Agents SDK) or
    ``pydantic_ai.Agent`` and wires it into the full voice pipeline:
    Audio In → Noise Reduction → VAD → STT → **Agent** → TTS → Audio Out.

    Parameters
    ----------
    agent:
        An agent instance from OpenAI Agents SDK, PydanticAI, or any
        object satisfying the ``run(text) -> str`` protocol.
    stt:
        Speech-to-text provider.  ``None`` for no-op stub.
    tts:
        Text-to-speech provider.  ``None`` for no-op stub.
    vad:
        Voice activity detection provider.  ``None`` for no-op stub.
    transport:
        Audio transport (local mic, WebSocket, Twilio, …).  ``None``
        for no-op stub.
    noise_reducer:
        Noise reduction provider.  ``None`` for no-op stub.
    event_bus:
        Shared event bus.  A new one is created if not provided.
    turn_manager_config:
        Turn-taking configuration (silence thresholds, pre-roll, etc.).
    timeout_config:
        Timeout configuration for STT, agent, and TTS stages.
    metrics:
        Metrics collector for latency tracking.
    tracer:
        Distributed tracing exporter.
    enable_noise_reduction:
        Whether the noise-reduction stage is active.
    enable_vad:
        Whether the VAD stage is active.
    run_config:
        *(OpenAI Agents SDK only)* ``RunConfig`` forwarded to every
        ``Runner.run`` / ``Runner.run_streamed`` call.
    context:
        *(OpenAI Agents SDK only)* Run context forwarded to every call.
    deps:
        *(PydanticAI only)* Dependencies forwarded to every
        ``agent.run`` call.
    model_settings:
        *(PydanticAI only)* ``ModelSettings`` override for every call.
    """

    def __init__(
        self,
        agent: Any,
        *,
        # Voice pipeline components
        stt: Any = None,
        tts: Any = None,
        vad: Any = None,
        transport: Any = None,
        noise_reducer: Any = None,
        # Session / pipeline configuration
        event_bus: EventBus | None = None,
        turn_manager_config: TurnManagerConfig | None = None,
        timeout_config: TimeoutConfig | None = None,
        metrics: MetricsCollector | None = None,
        tracer: Tracer | None = None,
        enable_noise_reduction: bool = True,
        enable_vad: bool = True,
        # OpenAI Agents SDK specific
        run_config: Any = None,
        context: Any = None,
        # PydanticAI specific
        deps: Any = None,
        model_settings: Any = None,
    ) -> None:
        wrapped = _wrap_agent(
            agent,
            run_config=run_config,
            context=context,
            deps=deps,
            model_settings=model_settings,
        )

        self._session = Session(
            SessionConfig(
                agent=wrapped,
                stt=stt,
                tts=tts,
                vad=vad,
                transport=transport,
                noise_reducer=noise_reducer,
                event_bus=event_bus,
                turn_manager_config=turn_manager_config,
                timeout_config=timeout_config,
                metrics=metrics,
                tracer=tracer,
                enable_noise_reduction=enable_noise_reduction,
                enable_vad=enable_vad,
            )
        )

    # ── Public API ────────────────────────────────────────────────

    @property
    def session(self) -> Session:
        """The underlying :class:`~easycat.Session` for advanced access.

        Use this to subscribe to events, inspect turn state, or call
        session-level methods like ``cancel_turn()``::

            pipeline.session.event_bus.subscribe(STTFinal, on_transcript)
        """
        return self._session

    @property
    def event_bus(self) -> EventBus:
        """Shortcut to the session's event bus."""
        return self._session.event_bus

    @property
    def turn_state(self) -> TurnState:
        """Current turn state of the pipeline."""
        return self._session.turn_state

    @property
    def is_running(self) -> bool:
        """Whether the pipeline is currently running."""
        return self._session.is_running

    async def start(self) -> None:
        """Start the voice pipeline (non-blocking).

        Begins listening for audio from the transport.  Use :meth:`stop`
        to shut down, or use :meth:`run` for a blocking alternative.
        """
        await self._session.start()

    async def stop(self) -> None:
        """Gracefully stop the voice pipeline."""
        await self._session.stop()

    async def run(self) -> None:
        """Start the pipeline and block until the transport closes or
        :meth:`stop` is called.

        This is the simplest way to run a voice agent::

            pipeline = VoicePipeline(agent, transport=LocalTransport())
            await pipeline.run()
        """
        await self._session.start()
        try:
            # Wait for the pipeline task to finish (transport closes)
            # or for stop() to be called from another coroutine.
            while self._session.is_running:
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass
        finally:
            await self._session.stop()
