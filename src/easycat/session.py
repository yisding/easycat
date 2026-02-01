"""Session: the core runtime for a single voice conversation.

Manages the voice pipeline lifecycle, wires provider stages together,
and handles turn state and cancellation. Supports both basic and
streaming agent interfaces with incremental TTS synthesis.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from easycat.agent_runner import AgentStreamEventType
from easycat.cancel import CancelToken
from easycat.events import (
    AgentDelta,
    AgentFinal,
    AudioIn,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    Error,
    EventBus,
    Interruption,
    STTEventType,
    STTFinal,
    STTPartial,
    ToolCallDelta,
    ToolCallResult,
    ToolCallStarted,
    TTSAudio,
    TTSEventType,
    TTSMarkers,
    TurnEnded,
    TurnStarted,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.stubs import (
    NoopAgent,
    NoopNoiseReducer,
    NoopSTT,
    NoopTransport,
    NoopTTS,
    NoopVAD,
)

logger = logging.getLogger(__name__)

# Sentence boundary regex: matches whitespace after sentence-ending punctuation
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


# ── Agent protocol (lightweight — WS7 provides real implementations) ──


@runtime_checkable
class Agent(Protocol):
    """Minimal agent interface: receive text, produce text."""

    async def run(self, text: str) -> str: ...


# ── Turn state ─────────────────────────────────────────────────────


class TurnState(enum.Enum):
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    BOT_SPEAKING = "bot_speaking"


# ── Session configuration ─────────────────────────────────────────


@dataclass
class SessionConfig:
    """Configuration for a Session."""

    stt: Any = None
    tts: Any = None
    vad: Any = None
    noise_reducer: Any = None
    transport: Any = None
    agent: Any = None

    # Pipeline flags
    enable_noise_reduction: bool = True
    enable_vad: bool = True


# ── Helpers ────────────────────────────────────────────────────────


def _split_at_sentence_boundaries(text: str) -> tuple[str, str]:
    """Split text at the last sentence boundary.

    Returns (ready_text, remaining_buffer). ``ready_text`` contains complete
    sentences to send to TTS; ``remaining_buffer`` holds any trailing text
    that hasn't reached a sentence boundary yet.
    """
    matches = list(_SENTENCE_END_RE.finditer(text))
    if not matches:
        return "", text
    last_match = matches[-1]
    return text[: last_match.start()], text[last_match.end() :]


# ── Session ────────────────────────────────────────────────────────


class Session:
    """One voice session (per call / per websocket client).

    Manages the full pipeline: Audio In -> Noise Reduction -> VAD -> STT ->
    Agent -> TTS -> Audio Out. Each stage is a pluggable provider.

    When the configured agent supports streaming (has a ``run_streaming``
    method), the session consumes text deltas incrementally and begins
    TTS synthesis on sentence boundaries for lower latency.
    """

    def __init__(self, config: SessionConfig | None = None) -> None:
        cfg = config or SessionConfig()

        # Providers (fall back to no-op stubs)
        self.stt = cfg.stt or NoopSTT()
        self.tts = cfg.tts or NoopTTS()
        self.vad = cfg.vad or NoopVAD()
        self.noise_reducer = cfg.noise_reducer or NoopNoiseReducer()
        self.transport = cfg.transport or NoopTransport()
        self.agent: Agent = cfg.agent or NoopAgent()

        # Pipeline flags
        self._enable_noise_reduction = cfg.enable_noise_reduction
        self._enable_vad = cfg.enable_vad

        # Event system
        self.event_bus = EventBus()

        # State
        self._turn_state = TurnState.IDLE
        self._is_running = False
        self._pipeline_task: asyncio.Task[None] | None = None
        self._stt_task: asyncio.Task[None] | None = None
        self._current_tts_task: asyncio.Task[None] | None = None

        # Cooperative cancellation: one token per turn
        self._cancel_token: CancelToken | None = None

    # ── Properties ─────────────────────────────────────────────

    @property
    def turn_state(self) -> TurnState:
        return self._turn_state

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def is_speaking(self) -> bool:
        return self._turn_state == TurnState.LISTENING

    @property
    def is_bot_speaking(self) -> bool:
        return self._turn_state == TurnState.BOT_SPEAKING

    @property
    def cancel_token(self) -> CancelToken | None:
        return self._cancel_token

    # ── Lifecycle ──────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize providers and begin the audio receive loop."""
        if self._is_running:
            return
        self._is_running = True
        self._turn_state = TurnState.IDLE

        await self.transport.connect()
        self._pipeline_task = asyncio.create_task(self._run_pipeline())

    async def stop(self) -> None:
        """Gracefully stop the session: finish current turn, close providers."""
        if not self._is_running:
            return
        self._is_running = False

        if self._cancel_token:
            self._cancel_token.cancel()

        if self._pipeline_task and not self._pipeline_task.done():
            self._pipeline_task.cancel()
            try:
                await self._pipeline_task
            except asyncio.CancelledError:
                logger.debug(
                    "TTS processing task was cancelled; ensuring BotStoppedSpeaking is emitted if needed."
                )

        await self._cancel_stt()
        await self._cancel_tts()
        await self.transport.disconnect()
        self._turn_state = TurnState.IDLE

    async def shutdown(self) -> None:
        """Force-close everything and release resources."""
        self._is_running = False

        if self._cancel_token:
            self._cancel_token.cancel()

        tasks: list[asyncio.Task[Any]] = []
        if self._pipeline_task and not self._pipeline_task.done():
            self._pipeline_task.cancel()
            tasks.append(self._pipeline_task)
        if self._stt_task and not self._stt_task.done():
            self._stt_task.cancel()
            tasks.append(self._stt_task)
        if self._current_tts_task and not self._current_tts_task.done():
            self._current_tts_task.cancel()
            tasks.append(self._current_tts_task)

        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        await self.transport.disconnect()
        self._turn_state = TurnState.IDLE

    # ── Cancellation ───────────────────────────────────────────

    async def cancel_turn(self, *, barge_in: bool = False) -> None:
        """Trigger cancel token, abort STT/agent/TTS, reset turn state.

        If barge_in is True, emits an Interruption event.
        """
        if self._cancel_token:
            self._cancel_token.cancel()

        if barge_in:
            await self.event_bus.emit(Interruption())

        await self._cancel_stt()
        await self._cancel_tts()
        self._turn_state = TurnState.IDLE

    async def cancel_tts_playback(self) -> None:
        """Stop TTS provider and flush outbound audio."""
        if self._cancel_token:
            self._cancel_token.cancel()

        await self._cancel_tts()
        if self._turn_state == TurnState.BOT_SPEAKING:
            self._turn_state = TurnState.IDLE

    async def reset_state(self) -> None:
        """Cancel everything and return to idle/listening state.

        Also clears agent conversation history if the agent supports it.
        """
        if self._cancel_token:
            self._cancel_token.cancel()

        await self._cancel_stt()
        await self._cancel_tts()

        # Clear agent history if supported (e.g., AgentRunner)
        if hasattr(self.agent, "clear_history"):
            self.agent.clear_history()

        self._turn_state = TurnState.IDLE

    # ── Pipeline ───────────────────────────────────────────────

    async def _run_pipeline(self) -> None:
        """Main audio receive loop: Transport -> Noise Reduction -> VAD -> STT.

        On STT final -> Agent -> TTS -> Transport audio out.
        """
        try:
            async for chunk in self.transport.receive_audio():
                if not self._is_running:
                    break

                await self.event_bus.emit(AudioIn(chunk=chunk))

                # Stage 1: Noise reduction (optional)
                if self._enable_noise_reduction:
                    chunk = await self.noise_reducer.process(chunk)

                # Stage 2: VAD (optional)
                if self._enable_vad:
                    async for vad_event in self.vad.process(chunk):
                        await self.event_bus.emit(vad_event)

                        if isinstance(vad_event, VADStartSpeaking):
                            # If bot is speaking, this is a barge-in
                            if self._turn_state == TurnState.BOT_SPEAKING:
                                await self.cancel_turn(barge_in=True)

                            self._cancel_token = CancelToken()
                            self._turn_state = TurnState.LISTENING
                            await self.stt.start_stream()
                            await self.event_bus.emit(TurnStarted())
                        elif isinstance(vad_event, VADStopSpeaking):
                            await self._handle_end_of_speech()

                # Stage 3: Feed audio to STT (if listening)
                if self._turn_state == TurnState.LISTENING:
                    await self.stt.send_audio(chunk)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.exception("Pipeline error")
            await self.event_bus.emit(Error(exception=exc, context="pipeline"))

    async def _handle_end_of_speech(self) -> None:
        """Called when VAD signals end of speech: finalize STT, run agent, synthesize TTS."""
        self._turn_state = TurnState.PROCESSING
        await self.event_bus.emit(TurnEnded())
        await self.stt.end_stream()

        token = self._cancel_token

        # Consume provider-scoped STT events and map to EasyCat events
        transcript = ""
        async for stt_event in self.stt.events():
            if token and token.is_cancelled:
                break
            if stt_event.type == STTEventType.PARTIAL:
                await self.event_bus.emit(STTPartial(text=stt_event.text))
            elif stt_event.type == STTEventType.FINAL:
                await self.event_bus.emit(STTFinal(text=stt_event.text))
                transcript = stt_event.text
                break

        if not transcript or (token and token.is_cancelled):
            self._turn_state = TurnState.IDLE
            return

        # Route to streaming or basic agent path
        if hasattr(self.agent, "run_streaming"):
            await self._run_streaming_agent(transcript, token)
        else:
            await self._run_basic_agent(transcript, token)

    # ── Basic agent path ───────────────────────────────────────

    async def _run_basic_agent(self, transcript: str, token: CancelToken | None) -> None:
        """Non-streaming agent path: invoke run(), emit events, synthesize TTS."""
        try:
            agent_response = await self.agent.run(transcript)
        except Exception as exc:
            logger.exception("Agent error")
            await self.event_bus.emit(Error(exception=exc, context="agent"))
            self._turn_state = TurnState.IDLE
            return

        if token and token.is_cancelled:
            self._turn_state = TurnState.IDLE
            return

        await self.event_bus.emit(AgentDelta(text=agent_response))
        await self.event_bus.emit(AgentFinal(text=agent_response))

        await self._synthesize_tts(agent_response, token)

    # ── Streaming agent path ───────────────────────────────────

    async def _run_streaming_agent(self, transcript: str, token: CancelToken | None) -> None:
        """Streaming agent path with incremental TTS on sentence boundaries.

        Runs agent stream consumption and TTS synthesis concurrently:
        - Agent task: consumes stream events, emits EasyCat events, and queues
          complete sentences for TTS synthesis.
        - TTS task: dequeues text chunks and synthesizes them sequentially.
        """
        tts_queue: asyncio.Queue[str | None] = asyncio.Queue()
        accumulated_text = ""
        agent_error: BaseException | None = None

        async def _consume_agent() -> None:
            nonlocal accumulated_text, agent_error
            text_buffer = ""
            try:
                async for event in self.agent.run_streaming(transcript, cancel_token=token):
                    if token and token.is_cancelled:
                        break

                    if event.type == AgentStreamEventType.TEXT_DELTA:
                        accumulated_text += event.text
                        text_buffer += event.text
                        await self.event_bus.emit(AgentDelta(text=event.text))

                        # Flush complete sentences to TTS queue
                        ready, text_buffer = _split_at_sentence_boundaries(text_buffer)
                        if ready:
                            await tts_queue.put(ready)

                    elif event.type == AgentStreamEventType.TOOL_STARTED:
                        await self.event_bus.emit(
                            ToolCallStarted(tool_name=event.tool_name, call_id=event.call_id)
                        )
                    elif event.type == AgentStreamEventType.TOOL_DELTA:
                        await self.event_bus.emit(
                            ToolCallDelta(call_id=event.call_id, delta=event.text)
                        )
                    elif event.type == AgentStreamEventType.TOOL_RESULT:
                        await self.event_bus.emit(
                            ToolCallResult(call_id=event.call_id, result=event.result)
                        )
                    elif event.type == AgentStreamEventType.DONE and event.text:
                        accumulated_text = event.text

            except Exception as exc:
                agent_error = exc
                logger.exception("Agent streaming error")
                await self.event_bus.emit(Error(exception=exc, context="agent"))
            finally:
                # Flush any remaining buffered text
                if text_buffer.strip():
                    await tts_queue.put(text_buffer.strip())
                await tts_queue.put(None)  # sentinel to stop TTS task

        async def _process_tts() -> None:
            started = False
            try:
                while True:
                    text = await tts_queue.get()
                    if text is None:
                        break
                    if token and token.is_cancelled:
                        break

                    if not started:
                        self._turn_state = TurnState.BOT_SPEAKING
                        await self.event_bus.emit(BotStartedSpeaking())
                        started = True

                    async for tts_event in self.tts.synthesize(text):
                        if token and token.is_cancelled:
                            break
                        if self._turn_state != TurnState.BOT_SPEAKING:
                            break
                        if tts_event.type == TTSEventType.AUDIO and tts_event.audio:
                            await self.event_bus.emit(TTSAudio(chunk=tts_event.audio))
                            await self.transport.send_audio(tts_event.audio)
                        elif tts_event.type == TTSEventType.MARKERS and tts_event.markers:
                            await self.event_bus.emit(TTSMarkers(markers=tts_event.markers))
            except asyncio.CancelledError:
                pass

            if started and self._turn_state == TurnState.BOT_SPEAKING:
                await self.event_bus.emit(BotStoppedSpeaking())
                self._turn_state = TurnState.IDLE

        # Run agent consumption and TTS synthesis concurrently
        agent_task = asyncio.create_task(_consume_agent())
        tts_task = asyncio.create_task(_process_tts())

        await agent_task

        # Emit AgentFinal after agent stream is fully consumed
        if accumulated_text and agent_error is None and not (token and token.is_cancelled):
            await self.event_bus.emit(AgentFinal(text=accumulated_text))

        await tts_task

        # If agent errored or was cancelled with no TTS started, ensure IDLE
        if self._turn_state != TurnState.IDLE:
            self._turn_state = TurnState.IDLE

    # ── TTS synthesis helper ───────────────────────────────────

    async def _synthesize_tts(self, text: str, token: CancelToken | None) -> None:
        """Synthesize TTS for a complete text and emit audio events."""
        self._turn_state = TurnState.BOT_SPEAKING
        await self.event_bus.emit(BotStartedSpeaking())
        try:
            async for tts_event in self.tts.synthesize(text):
                if token and token.is_cancelled:
                    break
                if self._turn_state != TurnState.BOT_SPEAKING:
                    break
                if tts_event.type == TTSEventType.AUDIO and tts_event.audio:
                    await self.event_bus.emit(TTSAudio(chunk=tts_event.audio))
                    await self.transport.send_audio(tts_event.audio)
                elif tts_event.type == TTSEventType.MARKERS and tts_event.markers:
                    await self.event_bus.emit(TTSMarkers(markers=tts_event.markers))
        except asyncio.CancelledError:
            pass

        if self._turn_state == TurnState.BOT_SPEAKING:
            await self.event_bus.emit(BotStoppedSpeaking())
            self._turn_state = TurnState.IDLE

    # ── Internal helpers ───────────────────────────────────────

    async def _cancel_stt(self) -> None:
        try:
            await self.stt.end_stream()
        except Exception:
            pass
        if self._stt_task and not self._stt_task.done():
            self._stt_task.cancel()
            try:
                await self._stt_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _cancel_tts(self) -> None:
        try:
            await self.tts.cancel()
        except Exception:
            pass
        if self._current_tts_task and not self._current_tts_task.done():
            self._current_tts_task.cancel()
            try:
                await self._current_tts_task
            except (asyncio.CancelledError, Exception):
                pass
