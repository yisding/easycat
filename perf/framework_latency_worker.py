#!/usr/bin/env python3
"""JSON-lines worker for the cross-framework latency benchmark.

This file intentionally imports each framework only inside its adapter so the
same worker can run in three isolated Python environments.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import importlib.metadata
import json
import sys
import time
import traceback
from collections.abc import AsyncIterator
from typing import Any, Literal

Framework = Literal["easycat", "livekit", "pipecat"]
RESPONSE_TEXT = "Hello there."


def _version(framework: Framework) -> str:
    distribution = {
        "easycat": "easycat",
        "livekit": "livekit-agents",
        "pipecat": "pipecat-ai",
    }[framework]
    return importlib.metadata.version(distribution)


def _begin_critical_path() -> tuple[float, bool]:
    """Collect before the sample, then prevent artificial setup GC in the timed span."""
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    return time.perf_counter(), was_enabled


def _end_critical_path(was_enabled: bool) -> float:
    ended = time.perf_counter()
    if was_enabled:
        gc.enable()
    return ended


async def _sample_easycat(  # noqa: C901 - self-contained adapter mirrors a real Session
    llm_delay_s: float, tts_delay_s: float
) -> dict[str, Any]:
    from easycat._turn_context import TurnContext
    from easycat.audio_format import PCM16_MONO_16K, AudioChunk
    from easycat.cancel import CancelToken
    from easycat.events import STTEvent, STTEventType, TTSEvent, TTSEventType
    from easycat.integrations.agents import AgentRunner
    from easycat.session._session import Session
    from easycat.session._types import SessionConfig
    from easycat.tts.input import TTSInput
    from easycat.turn_manager import TurnManagerConfig

    class Transport:
        send_audio_is_nonblocking = True

        def __init__(self) -> None:
            self.closed = asyncio.Event()
            self.first_audio: asyncio.Future[float] = asyncio.get_running_loop().create_future()
            self.audio_bytes = 0

        async def connect(self) -> None: ...

        async def disconnect(self) -> None:
            self.closed.set()

        async def receive_audio(self) -> AsyncIterator[AudioChunk]:
            await self.closed.wait()
            if False:
                yield AudioChunk(data=b"", format=PCM16_MONO_16K)

        async def send_audio(self, chunk: AudioChunk) -> bool:
            self.audio_bytes += len(chunk.data)
            if not self.first_audio.done():
                self.first_audio.set_result(time.perf_counter())
            return True

        async def clear_audio(self) -> None: ...

    class VAD:
        async def process(self, chunk: AudioChunk) -> AsyncIterator[object]:
            _ = chunk
            if False:
                yield None

        def configure(self, **kwargs: object) -> None:
            _ = kwargs

    class STT:
        async def start_stream(self) -> None: ...
        async def send_audio(self, chunk: AudioChunk) -> None:
            _ = chunk

        async def end_stream(self) -> None: ...

        async def events(self) -> AsyncIterator[STTEvent]:
            if False:
                yield STTEvent(type=STTEventType.PARTIAL, text="")

    class NoiseReducer:
        async def process(self, chunk: AudioChunk) -> AudioChunk:
            return chunk

    class Agent:
        def __init__(self) -> None:
            self.elapsed_ms = 0.0

        async def run(self, text: str) -> str:
            _ = text
            provider_started = time.perf_counter()
            await asyncio.sleep(llm_delay_s)
            self.elapsed_ms = (time.perf_counter() - provider_started) * 1_000.0
            return RESPONSE_TEXT

    class TTS:
        def __init__(self) -> None:
            self.elapsed_ms = 0.0
            self.spoken_text = ""

        async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
            self.spoken_text += payload.text
            provider_started = time.perf_counter()
            await asyncio.sleep(tts_delay_s)
            self.elapsed_ms = (time.perf_counter() - provider_started) * 1_000.0
            yield TTSEvent(
                type=TTSEventType.AUDIO,
                audio=AudioChunk(data=bytes(320), format=PCM16_MONO_16K),
            )

        async def cancel(self) -> None: ...
        async def stop(self) -> None: ...

    transport = Transport()
    agent = Agent()
    tts = TTS()
    session = Session(
        SessionConfig(
            transport=transport,
            vad=VAD(),
            stt=STT(),
            agent=AgentRunner(agent),
            tts=tts,
            noise_reducer=NoiseReducer(),
            turn_manager_config=TurnManagerConfig(end_of_turn_silence_ms=1),
        )
    )
    await session.start()
    session._turn = TurnContext("framework-latency", CancelToken())
    started, gc_was_enabled = _begin_critical_path()
    turn_task = asyncio.create_task(session._turn_runner.run_streaming_agent("Hello", token=None))
    try:
        first_audio = await asyncio.wait_for(transport.first_audio, timeout=5.0)
    finally:
        _end_critical_path(gc_was_enabled)
    await turn_task
    await session.stop(force=True)
    return {
        "latency_ms": (first_audio - started) * 1_000.0,
        "provider_elapsed_ms": agent.elapsed_ms + tts.elapsed_ms,
        "text": tts.spoken_text,
        "audio_bytes": transport.audio_bytes,
    }


async def _sample_livekit(  # noqa: C901 - self-contained adapter mirrors AgentSession
    llm_delay_s: float, tts_delay_s: float
) -> dict[str, Any]:
    from livekit import rtc
    from livekit.agents import Agent, AgentSession, llm, tts
    from livekit.agents.voice import io

    class Output(io.AudioOutput):
        def __init__(self) -> None:
            super().__init__(
                label="framework-latency",
                capabilities=io.AudioOutputCapabilities(pause=True),
                sample_rate=24_000,
            )
            self.first_audio: asyncio.Future[float] = asyncio.get_running_loop().create_future()
            self.audio_bytes = 0
            self.played_seconds = 0.0
            self.segment_open = False

        async def capture_frame(self, frame: rtc.AudioFrame) -> None:
            await super().capture_frame(frame)
            self.segment_open = True
            self.audio_bytes += len(frame.data)
            self.played_seconds += frame.duration
            if not self.first_audio.done():
                self.first_audio.set_result(time.perf_counter())
                self.on_playback_started(created_at=time.time())

        def _finish(self, *, interrupted: bool) -> None:
            super().flush()
            if self.segment_open:
                self.segment_open = False
                self.on_playback_finished(
                    playback_position=self.played_seconds,
                    interrupted=interrupted,
                )

        def flush(self) -> None:
            self._finish(interrupted=False)

        def clear_buffer(self) -> None:
            self._finish(interrupted=True)

    class PlaceholderLLM(llm.LLM):
        def chat(self, **kwargs: Any) -> Any:
            _ = kwargs
            raise AssertionError("overridden llm_node should be used")

    class PlaceholderTTS(tts.TTS):
        def __init__(self) -> None:
            super().__init__(
                capabilities=tts.TTSCapabilities(streaming=True),
                sample_rate=24_000,
                num_channels=1,
            )

        def synthesize(self, text: str, **kwargs: Any) -> Any:
            _ = text, kwargs
            raise AssertionError("overridden tts_node should be used")

    class BenchmarkAgent(Agent):
        def __init__(self) -> None:
            super().__init__(
                instructions="Reply briefly.",
                llm=PlaceholderLLM(),
                tts=PlaceholderTTS(),
                vad=None,
                stt=None,
                turn_handling={"turn_detection": None},
            )
            self.llm_elapsed_ms = 0.0
            self.tts_elapsed_ms = 0.0
            self.spoken_text = ""

        async def llm_node(self, chat_ctx: Any, tools: Any, model_settings: Any) -> Any:
            _ = chat_ctx, tools, model_settings
            provider_started = time.perf_counter()
            await asyncio.sleep(llm_delay_s)
            self.llm_elapsed_ms = (time.perf_counter() - provider_started) * 1_000.0
            yield RESPONSE_TEXT

        async def tts_node(self, text: AsyncIterator[str], model_settings: Any) -> Any:
            _ = model_settings
            async for chunk in text:
                if chunk.strip():
                    self.spoken_text += chunk
                    provider_started = time.perf_counter()
                    await asyncio.sleep(tts_delay_s)
                    self.tts_elapsed_ms = (time.perf_counter() - provider_started) * 1_000.0
                    yield rtc.AudioFrame(
                        data=bytes(480),
                        sample_rate=24_000,
                        num_channels=1,
                        samples_per_channel=240,
                    )
                    return

    session = AgentSession(
        vad=None,
        stt=None,
        llm=None,
        tts=None,
        turn_handling={"turn_detection": None},
    )
    output = Output()
    agent = BenchmarkAgent()
    session.output.audio = output
    await session.start(agent=agent, record=False)
    started, gc_was_enabled = _begin_critical_path()
    result = session.run(user_input="Hello", input_modality="text")
    try:
        first_audio = await asyncio.wait_for(output.first_audio, timeout=5.0)
    finally:
        _end_critical_path(gc_was_enabled)
    await result
    await session.aclose()
    return {
        "latency_ms": (first_audio - started) * 1_000.0,
        "provider_elapsed_ms": agent.llm_elapsed_ms + agent.tts_elapsed_ms,
        "text": agent.spoken_text,
        "audio_bytes": output.audio_bytes,
    }


async def _sample_pipecat(  # noqa: C901 - self-contained adapter mirrors PipelineTask
    llm_delay_s: float, tts_delay_s: float
) -> dict[str, Any]:
    from loguru import logger
    from pipecat.frames.frames import (
        EndFrame,
        Frame,
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        StartFrame,
        TextFrame,
        TranscriptionFrame,
        TTSAudioRawFrame,
        TTSStartedFrame,
        TTSStoppedFrame,
    )
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    logger.remove()

    class LLM(FrameProcessor):
        def __init__(self) -> None:
            super().__init__()
            self.generated_text = ""
            self.elapsed_ms = 0.0

        async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            if isinstance(frame, TranscriptionFrame):
                provider_started = time.perf_counter()
                await asyncio.sleep(llm_delay_s)
                self.elapsed_ms = (time.perf_counter() - provider_started) * 1_000.0
                self.generated_text = RESPONSE_TEXT
                await self.push_frame(LLMFullResponseStartFrame())
                await self.push_frame(TextFrame(RESPONSE_TEXT))
                await self.push_frame(LLMFullResponseEndFrame())
            else:
                await self.push_frame(frame, direction)

    class TTS(FrameProcessor):
        def __init__(self) -> None:
            super().__init__()
            self.elapsed_ms = 0.0
            self.spoken_text = ""

        async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            if type(frame) is TextFrame:
                self.spoken_text += frame.text
                await self.push_frame(TTSStartedFrame())
                provider_started = time.perf_counter()
                await asyncio.sleep(tts_delay_s)
                self.elapsed_ms = (time.perf_counter() - provider_started) * 1_000.0
                await self.push_frame(
                    TTSAudioRawFrame(audio=bytes(480), sample_rate=24_000, num_channels=1)
                )
                await self.push_frame(TTSStoppedFrame())
            else:
                await self.push_frame(frame, direction)

    class Sink(FrameProcessor):
        def __init__(self) -> None:
            super().__init__()
            self.ready = asyncio.Event()
            self.first_audio: asyncio.Future[float] = asyncio.get_running_loop().create_future()
            self.audio_bytes = 0

        async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            if isinstance(frame, StartFrame):
                self.ready.set()
            elif isinstance(frame, TTSAudioRawFrame):
                self.audio_bytes += len(frame.audio)
                if not self.first_audio.done():
                    self.first_audio.set_result(time.perf_counter())
            await self.push_frame(frame, direction)

    llm_processor = LLM()
    tts_processor = TTS()
    sink = Sink()
    pipeline = Pipeline([llm_processor, tts_processor, sink])
    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=False),
        enable_rtvi=False,
        enable_turn_tracking=False,
        check_dangling_tasks=False,
    )
    runner = PipelineRunner(handle_sigint=False)
    runner_task = asyncio.create_task(runner.run(task))
    await asyncio.wait_for(sink.ready.wait(), timeout=5.0)
    started, gc_was_enabled = _begin_critical_path()
    await task.queue_frame(
        TranscriptionFrame(text="Hello", user_id="benchmark", timestamp="0", finalized=True)
    )
    try:
        first_audio = await asyncio.wait_for(sink.first_audio, timeout=5.0)
    finally:
        _end_critical_path(gc_was_enabled)
    await task.queue_frame(EndFrame())
    await runner_task
    return {
        "latency_ms": (first_audio - started) * 1_000.0,
        "provider_elapsed_ms": llm_processor.elapsed_ms + tts_processor.elapsed_ms,
        "text": tts_processor.spoken_text,
        "audio_bytes": sink.audio_bytes,
    }


async def _sample(framework: Framework, request: dict[str, Any]) -> dict[str, Any]:
    llm_delay_s = float(request["llm_delay_ms"]) / 1_000.0
    tts_delay_s = float(request["tts_delay_ms"]) / 1_000.0
    if framework == "easycat":
        return await _sample_easycat(llm_delay_s, tts_delay_s)
    if framework == "livekit":
        return await _sample_livekit(llm_delay_s, tts_delay_s)
    return await _sample_pipecat(llm_delay_s, tts_delay_s)


def _write(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--framework", choices=("easycat", "livekit", "pipecat"), required=True)
    args = parser.parse_args()
    framework: Framework = args.framework
    _write(
        {
            "kind": "ready",
            "framework": framework,
            "version": _version(framework),
            "python": sys.version.split()[0],
        }
    )
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get("command") == "shutdown":
                _write({"kind": "stopped", "framework": framework})
                return
            if request.get("command") != "sample":
                raise ValueError("unknown worker command")
            result = asyncio.run(_sample(framework, request))
            _write({"kind": "sample", "framework": framework, **result})
        except Exception as exc:  # noqa: BLE001 - worker must report adapter failures as JSON
            _write(
                {
                    "kind": "error",
                    "framework": framework,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                }
            )


if __name__ == "__main__":
    main()
