"""Session streaming markdown stripping tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from easycat._turn_context import TurnContext
from easycat.cancel import CancelToken
from easycat.events import (
    AgentFinal,
    BotStoppedSpeaking,
)
from easycat.integrations.agents._agent_runner import AgentRunner
from easycat.integrations.agents.base import AgentBridgeEvent, AgentRecorder, AgentTurnInput
from easycat.runtime import InMemoryRingBuffer
from easycat.session._session import Session
from easycat.session._types import SessionConfig
from easycat.session.text import (
    has_unclosed_markdown_delimiters,
)
from tests._bridge_helpers import _TestBridgeBase
from tests.session._session_streaming_helpers import (
    _FAST_TURN,
    FakeNoiseReducer,
    FakeSTT,
    FakeTransport,
    FakeTTS,
    FakeVAD,
    _chunk,
)


def test_markdown_unclosed_single_italic_asterisk():
    assert has_unclosed_markdown_delimiters("*First sentence. Second sentence")
    assert not has_unclosed_markdown_delimiters("*First sentence. Second sentence*")


def test_markdown_unclosed_single_italic_underscore():
    assert has_unclosed_markdown_delimiters("_First sentence. Second sentence")
    assert not has_unclosed_markdown_delimiters("_First sentence. Second sentence_")
    assert not has_unclosed_markdown_delimiters("Use my_variable_name here.")


def test_markdown_unclosed_link_or_image_delimiters():
    assert has_unclosed_markdown_delimiters("See [OpenAI")
    assert has_unclosed_markdown_delimiters("See [OpenAI]")
    assert has_unclosed_markdown_delimiters("See [OpenAI](https://openai.com/docs")
    assert has_unclosed_markdown_delimiters("See ![diagram](https://img.example.com/plot")
    assert not has_unclosed_markdown_delimiters("See [OpenAI](https://openai.com/docs).")
    assert not has_unclosed_markdown_delimiters(
        "See [Function](https://en.wikipedia.org/wiki/Function_(mathematics))."
    )


def test_markdown_delimiters_inside_inline_code_do_not_block_streaming():
    assert not has_unclosed_markdown_delimiters("Literal `**` should not block.")
    assert not has_unclosed_markdown_delimiters("Literal `__` should not block.")
    assert not has_unclosed_markdown_delimiters("Literal `~~` should not block.")
    assert has_unclosed_markdown_delimiters("Literal `**` and **still open")


@pytest.mark.asyncio
async def test_streaming_strip_markdown_tts_receives_clean_text():
    """Markdown should be stripped from TTS chunks and AgentFinal when enabled."""

    class MarkdownStreamingAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "Go to **Settings** first. Then click *Security* next."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            # Stream the markdown response in realistic token-size deltas
            chunks = [
                "Go to **Settings",
                "** first. ",
                "Then click *Security",
                "* next.",
            ]
            for chunk in chunks:
                if cancel_token and cancel_token.is_cancelled:
                    break
                yield AgentBridgeEvent(kind="text_delta", text=chunk)
            full = "Go to **Settings** first. Then click *Security* next."
            yield AgentBridgeEvent(kind="done", text=full)

    tts = FakeTTS()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="help"),
        agent=MarkdownStreamingAgent(),
        tts=tts,
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
        strip_markdown=True,
    )
    session = Session(config)

    finals: list[AgentFinal] = []
    turn_finished = asyncio.Event()
    session.event_bus.subscribe(AgentFinal, lambda e: finals.append(e))
    session.event_bus.subscribe(BotStoppedSpeaking, lambda _e: turn_finished.set())

    await session.start()
    await asyncio.wait_for(turn_finished.wait(), timeout=1.0)
    await session.stop()

    # TTS should have received text with no markdown artefacts
    joined_tts = " ".join(tts.synthesized_texts)
    assert "**" not in joined_tts
    assert "*Security*" not in joined_tts
    assert "Settings" in joined_tts
    assert "Security" in joined_tts

    # AgentFinal event should also carry stripped text
    assert len(finals) == 1
    assert "**" not in finals[0].text
    assert "Settings" in finals[0].text


@pytest.mark.asyncio
async def test_streaming_strip_markdown_writes_journal_record():
    class MarkdownStreamingAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "Go to **Settings** first."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            yield AgentBridgeEvent(kind="text_delta", text="Go to **Settings")
            yield AgentBridgeEvent(kind="text_delta", text="** first.")
            yield AgentBridgeEvent(
                kind="done",
                text="Go to **Settings** first.",
            )

    journal = InMemoryRingBuffer()
    session = Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="help"),
            agent=MarkdownStreamingAgent(),
            tts=FakeTTS(),
            noise_reducer=FakeNoiseReducer(),
            enable_noise_reduction=False,
            turn_manager_config=_FAST_TURN,
            strip_markdown=True,
            journal=journal,
        )
    )
    session._turn = TurnContext("turn-stream-markdown", CancelToken())
    session._drain_session_actions = AsyncMock(return_value=False)

    await session._turn_runner.run_streaming_agent("help", token=None)

    records = [record for record in journal.read() if record.name == "markdown_stripped"]
    assert len(records) == 1
    assert records[0].turn_id == "turn-stream-markdown"
    assert records[0].data == {
        "phase": "streaming_final",
        "changed": True,
        "original_text": "Go to **Settings** first.",
        "stripped_text": "Go to Settings first.",
    }
    # The last-assistant rewrite is routed through AgentStage so the
    # framework-state mutation lands on the journal recording boundary
    # alongside the streamed text (xc-architecture consistency fix).
    rewrites = [
        record for record in journal.read() if record.name == "replace_last_assistant_text"
    ]
    assert len(rewrites) == 1
    assert rewrites[0].turn_id == "turn-stream-markdown"
    assert rewrites[0].data["stage"] == "agent"
    assert rewrites[0].data["text"] == "Go to Settings first."


@pytest.mark.asyncio
async def test_streaming_strip_markdown_normalizes_short_code_spans_for_tts():
    class CodeStreamingAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "Call `__init__`. Then run `print()`."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            chunks = ["Call `__init__`. ", "Then run `print()`."]
            for chunk in chunks:
                if cancel_token and cancel_token.is_cancelled:
                    break
                yield AgentBridgeEvent(kind="text_delta", text=chunk)
            yield AgentBridgeEvent(
                kind="done",
                text="Call `__init__`. Then run `print()`.",
            )

    tts = FakeTTS()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="help"),
        agent=CodeStreamingAgent(),
        tts=tts,
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
        strip_markdown=True,
    )
    session = Session(config)

    finals: list[AgentFinal] = []
    session.event_bus.subscribe(AgentFinal, lambda e: finals.append(e))

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    joined_tts = " ".join(tts.synthesized_texts)
    assert "dunder init" in joined_tts
    assert "print open paren close paren" in joined_tts
    assert "`" not in joined_tts

    assert len(finals) == 1
    assert "dunder init" in finals[0].text
    assert "print open paren close paren" in finals[0].text


@pytest.mark.asyncio
async def test_streaming_strip_markdown_preserves_chunk_boundary_spaces():
    """Chunk-boundary spaces should not be removed by incremental stripping."""

    class BoundarySpaceAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "Then click Security."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            for chunk in ["Then ", "click Security."]:
                if cancel_token and cancel_token.is_cancelled:
                    break
                yield AgentBridgeEvent(kind="text_delta", text=chunk)
            yield AgentBridgeEvent(kind="done", text="Then click Security.")

    tts = FakeTTS()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="test"),
        agent=BoundarySpaceAgent(),
        tts=tts,
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
        strip_markdown=True,
    )
    session = Session(config)

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    assert tts.synthesized_texts == ["Then click Security."]


@pytest.mark.asyncio
async def test_streaming_strip_markdown_cross_sentence_bold():
    """Bold spanning two sentences should still be stripped correctly."""

    class CrossSentenceBoldAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "**This is important. Very important.** Got it?"

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            chunks = ["**This is important. ", "Very important.** Got it?"]
            for chunk in chunks:
                if cancel_token and cancel_token.is_cancelled:
                    break
                yield AgentBridgeEvent(kind="text_delta", text=chunk)
            full = "**This is important. Very important.** Got it?"
            yield AgentBridgeEvent(kind="done", text=full)

    tts = FakeTTS()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="test"),
        agent=CrossSentenceBoldAgent(),
        tts=tts,
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
        strip_markdown=True,
    )
    session = Session(config)

    finals: list[AgentFinal] = []
    session.event_bus.subscribe(AgentFinal, lambda e: finals.append(e))

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    # TTS must not contain any stray ** markers
    joined_tts = " ".join(tts.synthesized_texts)
    assert "**" not in joined_tts
    assert "important" in joined_tts.lower()

    # AgentFinal should be fully stripped
    assert len(finals) == 1
    assert "**" not in finals[0].text


@pytest.mark.asyncio
async def test_streaming_strip_markdown_unclosed_bold_multiple_sentences():
    """Unclosed markdown across chunks should not drop earlier stripped text."""

    class UnclosedBoldAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "**First sentence. Second sentence.** Third sentence."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            chunks = ["**First sentence. Second sentence.", "** Third sentence."]
            for chunk in chunks:
                if cancel_token and cancel_token.is_cancelled:
                    break
                yield AgentBridgeEvent(kind="text_delta", text=chunk)
            yield AgentBridgeEvent(
                kind="done",
                text="**First sentence. Second sentence.** Third sentence.",
            )

    tts = FakeTTS()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="test"),
        agent=UnclosedBoldAgent(),
        tts=tts,
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
        strip_markdown=True,
    )
    session = Session(config)

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    joined_tts = " ".join(tts.synthesized_texts)
    assert "**" not in joined_tts
    assert "First sentence." in joined_tts
    assert "Second sentence." in joined_tts
    assert "Third sentence." in joined_tts


@pytest.mark.asyncio
async def test_streaming_strip_markdown_unclosed_italic_multiple_sentences():
    """Unclosed single-italic markdown should defer flushing until closed."""

    class UnclosedItalicAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "*First sentence. Second sentence.* Third sentence."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            chunks = ["*First sentence. Second sentence.", "* Third sentence."]
            for chunk in chunks:
                if cancel_token and cancel_token.is_cancelled:
                    break
                yield AgentBridgeEvent(kind="text_delta", text=chunk)
            yield AgentBridgeEvent(
                kind="done",
                text="*First sentence. Second sentence.* Third sentence.",
            )

    tts = FakeTTS()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="test"),
        agent=UnclosedItalicAgent(),
        tts=tts,
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
        strip_markdown=True,
    )
    session = Session(config)

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    joined_tts = " ".join(tts.synthesized_texts)
    assert "*" not in joined_tts
    assert "First sentence." in joined_tts
    assert "Second sentence." in joined_tts
    assert "Third sentence." in joined_tts


@pytest.mark.asyncio
async def test_streaming_strip_markdown_unclosed_link_multiple_sentences():
    """Unclosed markdown links across chunks should not leak link/url artefacts."""

    class UnclosedLinkAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "See [OpenAI. Next sentence.](https://openai.com/docs) Last sentence."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            chunks = [
                "See [OpenAI. Next sentence.",
                "](https://openai.com/docs) Last sentence.",
            ]
            for chunk in chunks:
                if cancel_token and cancel_token.is_cancelled:
                    break
                yield AgentBridgeEvent(kind="text_delta", text=chunk)
            yield AgentBridgeEvent(
                kind="done",
                text="See [OpenAI. Next sentence.](https://openai.com/docs) Last sentence.",
            )

    tts = FakeTTS()
    transport = FakeTransport(chunks=[_chunk(), _chunk()])
    config = SessionConfig(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="test"),
        agent=UnclosedLinkAgent(),
        tts=tts,
        noise_reducer=FakeNoiseReducer(),
        turn_manager_config=_FAST_TURN,
        strip_markdown=True,
    )
    session = Session(config)

    finals: list[AgentFinal] = []
    session.event_bus.subscribe(AgentFinal, lambda e: finals.append(e))

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    joined_tts = " ".join(tts.synthesized_texts)
    assert "https://openai.com/docs" in joined_tts
    assert "](" not in joined_tts
    assert "OpenAI." in joined_tts
    assert "Next sentence." in joined_tts
    assert "Last sentence." in joined_tts

    assert len(finals) == 1
    assert "https://openai.com/docs" in finals[0].text
    assert "](" not in finals[0].text


@pytest.mark.asyncio
async def test_streaming_strip_markdown_flushes_tail_without_sentence_boundary():
    class TailOnlyMarkdownAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "**Final sentence without punctuation**"

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            text = turn_input.text
            _ = recorder, text
            full = "**Final sentence without punctuation**"
            yield AgentBridgeEvent(kind="text_delta", text=full)
            yield AgentBridgeEvent(kind="done", text=full)

    tts = FakeTTS()
    session = Session(
        SessionConfig(
            transport=FakeTransport(chunks=[_chunk(), _chunk()]),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="test"),
            agent=TailOnlyMarkdownAgent(),
            tts=tts,
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
            strip_markdown=True,
        )
    )

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    assert tts.synthesized_texts == ["Final sentence without punctuation"]


@pytest.mark.asyncio
async def test_streaming_strip_markdown_failed_turn_does_not_rewrite_prior_history():
    """Failed streaming turns must not patch prior assistant history entries."""

    class FlakyMarkdownAgent(_TestBridgeBase):
        def __init__(self) -> None:

            super().__init__()
            self.calls = 0

        async def run(self, text: str) -> str:
            return "unused"

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
                full = "First **answer**."
                yield AgentBridgeEvent(kind="text_delta", text=full)
                yield AgentBridgeEvent(kind="done", text=full)
                return

            yield AgentBridgeEvent(
                kind="text_delta",
                text="[overwritten](https://example.com)",
            )
            raise RuntimeError("agent failed mid-stream")

    runner = AgentRunner(FlakyMarkdownAgent())
    session = Session(
        SessionConfig(
            transport=FakeTransport(),
            vad=FakeVAD(),
            stt=FakeSTT(transcript="ignored"),
            agent=runner,
            tts=FakeTTS(),
            noise_reducer=FakeNoiseReducer(),
            turn_manager_config=_FAST_TURN,
            strip_markdown=True,
        )
    )

    session._turn = TurnContext("turn-1", CancelToken())
    await session._turn_runner.run_streaming_agent("first", token=None)
    assert runner.history[-1]["role"] == "assistant"
    assert runner.history[-1]["content"] == "First answer."
    history_after_success = [entry.copy() for entry in runner.history]

    session._turn = TurnContext("turn-2", CancelToken())
    await session._turn_runner.run_streaming_agent("second", token=None)
    assert runner.history == history_after_success
