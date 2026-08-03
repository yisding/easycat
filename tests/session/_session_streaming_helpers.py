"""Shared helpers for session streaming tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.cancel import CancelToken
from easycat.events import TTSEvent, TTSEventType
from easycat.integrations.agents.base import AgentBridgeEvent, AgentRecorder, AgentTurnInput
from easycat.tts.input import TTSInput
from easycat.turn_manager import TurnManagerConfig
from tests._bridge_helpers import _TestBridgeBase
from tests._fakes import FakeSTT, FakeTransport, FakeTTS, FakeVAD

_FAST_TURN = TurnManagerConfig(end_of_turn_silence_ms=1)
__all__ = ["FakeSTT", "FakeTTS", "FakeTransport", "FakeVAD"]


def _chunk(n: int = 320) -> AudioChunk:
    return AudioChunk(data=bytes(n), format=PCM16_MONO_16K)


class FakeNoiseReducer:
    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk


class ContextCapturingBridge(_TestBridgeBase):
    def __init__(self, response_prefix: str = "reply") -> None:
        super().__init__()
        self.response_prefix = response_prefix
        self.contexts: list[list[dict[str, str]]] = []
        self.inputs: list[AgentTurnInput] = []

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        _ = recorder, cancel_token
        self.inputs.append(turn_input)
        self.contexts.append(list(turn_input.context))
        yield AgentBridgeEvent(kind="done", text=f"{self.response_prefix}:{turn_input.text}")


class StreamingUpperAgent(_TestBridgeBase):
    """Streaming agent that uppercases and streams word by word."""

    async def run(self, text: str) -> str:
        return text.upper()

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        text = turn_input.text
        _ = recorder, text
        words = text.upper().split()
        for i, word in enumerate(words):
            if cancel_token and cancel_token.is_cancelled:
                break
            delta = word if i == 0 else f" {word}"
            yield AgentBridgeEvent(kind="text_delta", text=delta)
        full = " ".join(words)
        yield AgentBridgeEvent(kind="done", text=full)


class StreamingSentenceAgent(_TestBridgeBase):
    """Streams text with sentence boundaries for incremental TTS testing."""

    async def run(self, text: str) -> str:
        return "Hello world. How are you? I am fine."

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        text = turn_input.text
        _ = recorder, text
        chunks = ["Hello world. ", "How are you? ", "I am fine."]
        for chunk in chunks:
            if cancel_token and cancel_token.is_cancelled:
                break
            yield AgentBridgeEvent(kind="text_delta", text=chunk)
        yield AgentBridgeEvent(
            kind="done",
            text="Hello world. How are you? I am fine.",
        )


class StreamingToolCallingAgent(_TestBridgeBase):
    """Streaming agent that calls a tool during response."""

    async def run(self, text: str) -> str:
        return "The result is 42."

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        text = turn_input.text
        _ = recorder, text
        yield AgentBridgeEvent(
            kind="tool_started",
            tool_name="calculator",
            call_id="call_abc",
        )
        yield AgentBridgeEvent(
            kind="tool_delta",
            call_id="call_abc",
            text="computing...",
        )
        yield AgentBridgeEvent(
            kind="tool_result",
            call_id="call_abc",
            result="42",
        )
        yield AgentBridgeEvent(
            kind="text_delta",
            text="The result is 42.",
        )
        yield AgentBridgeEvent(
            kind="done",
            text="The result is 42.",
        )


class SlowStreamingAgent(_TestBridgeBase):
    """Agent that streams slowly — useful for barge-in testing."""

    _WORDS = ("slow ", "streaming ", "response ", "that ", "keeps ", "going.")

    def __init__(self) -> None:
        super().__init__()
        self.cancel_observed = asyncio.Event()

    async def run(self, text: str) -> str:
        return "".join(self._WORDS)

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        text = turn_input.text
        _ = recorder, text
        for word in self._WORDS:
            if cancel_token and cancel_token.is_cancelled:
                self.cancel_observed.set()
                break
            yield AgentBridgeEvent(kind="text_delta", text=word)
            await asyncio.sleep(0.05)
        yield AgentBridgeEvent(kind="done", text="".join(self._WORDS))


class FailingStreamingAgent(_TestBridgeBase):
    """Agent that raises during streaming."""

    async def run(self, text: str) -> str:
        return "won't get here"

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        text = turn_input.text
        _ = recorder, text
        yield AgentBridgeEvent(kind="text_delta", text="start ")
        raise RuntimeError("agent failed mid-stream")


class PostDoneStreamingAgent(_TestBridgeBase):
    """Agent that incorrectly keeps emitting events after DONE."""

    async def run(self, text: str) -> str:
        return "Alpha."

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        text = turn_input.text
        _ = recorder, text
        yield AgentBridgeEvent(kind="text_delta", text="Alpha.")
        yield AgentBridgeEvent(kind="done", text="Alpha.")
        yield AgentBridgeEvent(
            kind="tool_started",
            tool_name="late_tool",
            call_id="call_late",
        )
        yield AgentBridgeEvent(kind="text_delta", text=" Beta.")


class StructuredOnlyStreamingAgent(_TestBridgeBase):
    """Agent that completes with structured output and no text."""

    async def run(self, text: str) -> str:
        return ""

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        text = turn_input.text
        _ = recorder, text
        yield AgentBridgeEvent(
            kind="done",
            text="",
            structured_output={"answer": 42},
        )


class DoneOnlyStreamingAgent(_TestBridgeBase):
    """Agent that emits only a DONE event with full text (no TEXT_DELTA events)."""

    async def run(self, text: str) -> str:
        return "Hello from done-only bridge."

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        text = turn_input.text
        _ = recorder, text
        yield AgentBridgeEvent(
            kind="done",
            text="Hello from done-only bridge.",
        )


class TimeoutThenRecoverStreamingAgent(_TestBridgeBase):
    """Agent that times out once, then succeeds on the next turn."""

    def __init__(self) -> None:

        super().__init__()
        self.calls = 0

    async def run(self, text: str) -> str:
        return "Recovered."

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        text = turn_input.text
        _ = recorder, text
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(0.05)
            return

        yield AgentBridgeEvent(kind="text_delta", text="Recovered.")
        yield AgentBridgeEvent(kind="done", text="Recovered.")


class TimeoutThenRecoverTTS(FakeTTS):
    """TTS that times out once before producing any audio, then recovers."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        self.calls += 1
        self.synthesized_texts.append(payload.text)
        if self.calls == 1:
            await asyncio.sleep(0.05)
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())


class SlowToolCallingAgent(_TestBridgeBase):
    """Streaming agent with a tool call that takes time, allowing
    cancellation to arrive mid-tool."""

    def __init__(self) -> None:

        super().__init__()
        self.interruption_notified = False
        self.interruption_text_spoken = ""
        self.interruption_mode = ""

    async def run(self, text: str) -> str:
        return "The answer is 42."

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        text = turn_input.text
        _ = recorder, text
        # Text before tool — deliberately unterminated so lookahead keeps
        # it buffered instead of flushing it to TTS before the tool runs.
        text = turn_input.text
        _ = recorder, text
        yield AgentBridgeEvent(kind="text_delta", text="Let me look")
        # Tool lifecycle
        yield AgentBridgeEvent(
            kind="tool_started",
            tool_name="database_update",
            call_id="call_xyz",
        )
        yield AgentBridgeEvent(
            kind="tool_delta",
            call_id="call_xyz",
            text="updating...",
        )
        # Simulate slow tool — cancellation arrives here
        await asyncio.sleep(0.1)
        yield AgentBridgeEvent(
            kind="tool_result",
            call_id="call_xyz",
            result="row updated",
        )
        # Text after tool (should be skipped on barge-in)
        yield AgentBridgeEvent(kind="text_delta", text="Done!")
        yield AgentBridgeEvent(kind="done", text="Done!")

    def notify_interruption(self, text_spoken: str = "", *, mode: str = "truncate") -> None:
        self.interruption_notified = True
        self.interruption_text_spoken = text_spoken
        self.interruption_mode = mode


class FastDoneAgent(_TestBridgeBase):
    """Agent that completes quickly and supports interruption notifications."""

    def __init__(self) -> None:

        super().__init__()
        self.finished = asyncio.Event()
        self.interruption_notified = False
        self.interruption_text_spoken = ""
        self.interruption_mode = ""

    async def run(self, text: str) -> str:
        return "Quick reply."

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        text = turn_input.text
        _ = recorder, text
        yield AgentBridgeEvent(kind="text_delta", text="Quick reply.")
        yield AgentBridgeEvent(kind="done", text="Quick reply.")
        self.finished.set()

    def notify_interruption(self, text_spoken: str = "", *, mode: str = "truncate") -> None:
        self.interruption_notified = True
        self.interruption_text_spoken = text_spoken
        self.interruption_mode = mode


class SlowStartTTS(FakeTTS):
    """TTS that starts playback, then waits before yielding audio."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        self.synthesized_texts.append(payload.text)
        self.started.set()
        await asyncio.sleep(0.1)
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())
