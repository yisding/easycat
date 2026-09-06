"""Session output processor and markdown preparation tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from easycat._turn_context import TurnContext
from easycat.cancel import CancelToken
from easycat.events import (
    TTSEvent,
    TTSEventType,
)
from easycat.llm_output_processing import PauseProcessor, PhoneticReplacementProcessor
from easycat.runtime import InMemoryRingBuffer
from easycat.session._session import Session
from easycat.tts.input import TTSInput
from tests.session._session_core_helpers import (
    FakeSTT,
    FakeTransport,
    FakeTTS,
    _full_config,
    _make_chunk,
)


class CaptureTTS(FakeTTS):
    def __init__(self) -> None:
        self.payloads: list[TTSInput] = []

    async def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        self.payloads.append(payload if isinstance(payload, TTSInput) else TTSInput(payload))
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_make_chunk())


def test_session_strip_markdown_does_not_inject_hidden_processor():
    class MarkerProcessor:
        def process(self, payload: TTSInput, *, is_final: bool, is_streaming: bool) -> TTSInput:
            return payload

    processor = MarkerProcessor()
    session = Session(_full_config(strip_markdown=True, output_processors=[processor]))

    assert session._tts_scheduler._output_processors == [processor]


@pytest.mark.asyncio
async def test_streaming_agent_strip_markdown_writes_journal_record():
    class MarkdownAgent:
        async def run(self, text: str) -> str:
            return "Go to **Settings** first."

    journal = InMemoryRingBuffer()
    session = Session(_full_config(agent=MarkdownAgent(), journal=journal, strip_markdown=True))
    session._turn = TurnContext("turn-markdown", CancelToken())

    await session._turn_runner.run_streaming_agent("help", token=None)

    records = [record for record in journal.read() if record.name == "markdown_stripped"]
    assert records, "expected a markdown_stripped record"
    final_record = next(r for r in records if r.data.get("phase") == "streaming_final")
    assert final_record.turn_id == "turn-markdown"
    assert final_record.data == {
        "phase": "streaming_final",
        "changed": True,
        "original_text": "Go to **Settings** first.",
        "stripped_text": "Go to Settings first.",
    }


@pytest.mark.asyncio
async def test_prepare_tts_payload_writes_journal_record():
    class PrefixProcessor:
        def process(self, payload: TTSInput, *, is_final: bool, is_streaming: bool) -> TTSInput:
            return TTSInput(text=f"speak: {payload.text}", format=payload.format)

    journal = InMemoryRingBuffer()
    session = Session(
        _full_config(
            journal=journal,
            output_processors=[PrefixProcessor()],
        )
    )
    session._turn = TurnContext("turn-tts-prepared", CancelToken())

    payload = session._tts_scheduler.prepare("hello", is_streaming=False, is_final=True)

    assert payload.text == "speak: hello"
    records = [record for record in journal.read() if record.name == "tts_payload_prepared"]
    assert len(records) == 1
    assert records[0].turn_id == "turn-tts-prepared"
    assert records[0].data == {
        "is_streaming": False,
        "is_final": True,
        "changed": True,
        "original_text": "hello",
        "original_format": "plain",
        "prepared_text": "speak: hello",
        "prepared_format": "plain",
        "processors": ["PrefixProcessor"],
        "ssml_downgraded": False,
    }


@pytest.mark.asyncio
async def test_session_applies_output_processors_before_tts() -> None:
    class PrefixProcessor:
        def process(self, payload: TTSInput, *, is_final: bool, is_streaming: bool) -> TTSInput:
            return TTSInput(text=f"speak: {payload.text}", format=payload.format)

    tts = CaptureTTS()
    session = Session(
        _full_config(
            tts=tts,
            output_processors=[PrefixProcessor()],
            transport=FakeTransport(chunks=[_make_chunk(), _make_chunk()]),
            stt=FakeSTT(transcript="hello"),
        )
    )

    session._turn = TurnContext("turn-output-proc", CancelToken())
    await session._turn_runner.run_streaming_agent("call me at 415-555-2671", token=None)

    assert tts.payloads
    assert tts.payloads[0].text.startswith("speak: ")
    assert tts.payloads[0].format == "plain"


@pytest.mark.asyncio
async def test_session_falls_back_to_plain_when_ssml_not_supported() -> None:
    tts = CaptureTTS()
    session = Session(
        _full_config(
            tts=tts,
            output_processors=[
                PauseProcessor(
                    pattern=r"\+?\d[\d\s().-]{5,}\d",
                    unit_pattern=r"\d",
                    minimum_units=7,
                    style="ssml",
                )
            ],
            transport=FakeTransport(chunks=[_make_chunk(), _make_chunk()]),
            stt=FakeSTT(transcript="call AT&T at 415-555-2671"),
        )
    )

    session._turn = TurnContext("turn-ssml-fallback", CancelToken())
    await session._turn_runner.run_streaming_agent("call AT&T at 415-555-2671", token=None)

    assert tts.payloads
    assert tts.payloads[0].format == "plain"
    assert "<break" not in tts.payloads[0].text
    assert "AT&T" in tts.payloads[0].text
    assert "AT&amp;T" not in tts.payloads[0].text
    assert "4 1 5" in tts.payloads[0].text


@pytest.mark.asyncio
async def test_session_falls_back_to_plain_unescapes_ssml_entities() -> None:
    tts = CaptureTTS()
    session = Session(
        _full_config(
            tts=tts,
            output_processors=[
                PauseProcessor(
                    pattern=r"\+?\d[\d\s().-]{5,}\d",
                    unit_pattern=r"\d",
                    minimum_units=7,
                    style="ssml",
                )
            ],
        )
    )

    session._turn = TurnContext("turn-ssml-unescape", CancelToken())
    await session._turn_runner.run_streaming_agent("Call AT&T at 415-555-2671", token=None)

    assert tts.payloads
    assert tts.payloads[0].format == "plain"
    assert "AT&T" in tts.payloads[0].text
    assert "AT&amp;T" not in tts.payloads[0].text


@pytest.mark.asyncio
async def test_session_composes_phonetic_and_phone_processors() -> None:
    tts = CaptureTTS()
    session = Session(
        _full_config(
            tts=tts,
            output_processors=[
                PhoneticReplacementProcessor({"Siobhan": "shi-vawn"}),
                PauseProcessor(
                    pattern=r"\+?\d[\d\s().-]{5,}\d",
                    unit_pattern=r"\d",
                    minimum_units=7,
                    pause_ms=140,
                ),
            ],
        )
    )

    session._turn = TurnContext("turn-phonetic", CancelToken())
    await session._turn_runner.run_streaming_agent("call Siobhan at 415-555-2671", token=None)

    assert tts.payloads
    # The default ellipsis style reaches a plain-text provider unchanged.
    assert tts.payloads[0].format == "plain"
    assert "shi-vawn" in tts.payloads[0].text
    assert "4 ... 1 ... 5" in tts.payloads[0].text


# ── Spoken terms are literal, not re.sub templates (gh 1101) ─────


def _spoken(replacements: dict[str, str], text: str) -> str:
    processor = PhoneticReplacementProcessor(replacements=replacements)
    return processor.process(TTSInput(text=text), is_final=True, is_streaming=False).text


def test_phonetic_replacement_treats_a_backslash_spoken_term_literally() -> None:
    """A backslash in a pronunciation must reach TTS as itself (gh 1101).

    The spoken term used to be passed straight to ``re.sub`` as a replacement
    *template*, so ``"\\alpha"`` emitted a BEL control character mid-sentence
    and ``"\\1"`` injected the matched source text.
    """
    assert _spoken({"alpha": "\\alpha"}, "use the alpha function") == "use the \\alpha function"
    assert "\x07" not in _spoken({"alpha": "\\alpha"}, "use the alpha function")
    assert _spoken({"beta": "\\beta"}, "the beta path") == "the \\beta path"


def test_phonetic_replacement_does_not_expand_group_references() -> None:
    assert _spoken({"alpha": "\\1"}, "the alpha function") == "the \\1 function"
    assert _spoken({"alpha": "\\g<0>"}, "the alpha function") == "the \\g<0> function"


def test_phonetic_replacement_survives_an_invalid_escape() -> None:
    """``"\\d"`` used to raise, and the fail-open handler then dropped the
    processor for every later payload — one bad entry disabled all
    pronunciations."""
    assert _spoken({"alpha": "\\d"}, "the alpha function") == "the \\d function"
    assert _spoken({"alpha": "back\\"}, "the alpha function") == "the back\\ function"


def test_phonetic_replacement_keeps_independent_terms_independent() -> None:
    """Each term substitutes its own spoken form (no late-binding capture)."""
    result = _spoken({"alpha": "AL-fuh", "beta": "BAY-tuh"}, "alpha then beta")

    assert result == "AL-fuh then BAY-tuh"
