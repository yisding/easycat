"""Tests for ``easycat.quick`` API-key resolution.

These lock in that the teaching helpers resolve API keys from the factory
provider catalogs (a single source of truth) rather than a hand-maintained
copy that can drift — in particular that ``cartesia`` resolves
``CARTESIA_API_KEY`` and unknown providers raise instead of silently
defaulting to ``OPENAI_API_KEY``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

import easycat.quick as quick
from easycat.audio_format import PCM16_MONO_24K, AudioChunk
from easycat.errors import EasyCatError
from easycat.events import TTSEvent, TTSEventType
from easycat.quick import _resolve_api_key, speak
from easycat.stt.factory import _CATALOG as _STT_CATALOG
from easycat.tts.factory import _CATALOG as _TTS_CATALOG
from easycat.tts.input import TTSInput


class TestResolveApiKey:
    def test_explicit_key_short_circuits(self):
        assert _resolve_api_key("cartesia", "explicit-key", catalog=_TTS_CATALOG) == "explicit-key"

    def test_cartesia_resolves_cartesia_env_var(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("CARTESIA_API_KEY", "ct-key")
        assert _resolve_api_key("cartesia", None, catalog=_TTS_CATALOG) == "ct-key"
        assert _resolve_api_key("cartesia", None, catalog=_STT_CATALOG) == "ct-key"

    def test_missing_key_names_the_correct_env_var(self, monkeypatch):
        monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="CARTESIA_API_KEY"):
            _resolve_api_key("cartesia", None, catalog=_TTS_CATALOG)

    def test_unknown_provider_raises_instead_of_defaulting_to_openai(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "oa-key")
        with pytest.raises(EasyCatError):
            _resolve_api_key("not-a-provider", None, catalog=_TTS_CATALOG)

    def test_deepgram_resolves_deepgram_env_var(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-key")
        assert _resolve_api_key("deepgram", None, catalog=_STT_CATALOG) == "dg-key"


class _FakeTTS:
    """TTS provider stub recording whether its persistent client was closed."""

    def __init__(self) -> None:
        self.closed = False

    async def synthesize(self, _input: TTSInput) -> AsyncIterator[TTSEvent]:
        yield TTSEvent(
            type=TTSEventType.AUDIO,
            audio=AudioChunk(data=b"\x00\x00", format=PCM16_MONO_24K),
        )

    async def close(self) -> None:
        self.closed = True


class _FakeTransport:
    def __init__(self) -> None:
        self.received: list[AudioChunk] = []

    async def send_audio(self, audio: AudioChunk) -> None:
        self.received.append(audio)


class TestSpeakResourceOwnership:
    @pytest.mark.asyncio
    async def test_helper_constructed_tts_is_closed(self, monkeypatch):
        # When speak() builds the provider itself, it owns the persistent
        # httpx client and must close it so callers do not leak connections.
        monkeypatch.setenv("OPENAI_API_KEY", "oa-key")
        fake = _FakeTTS()
        monkeypatch.setattr(quick, "create_tts_provider", lambda _config: fake)
        transport = _FakeTransport()

        await speak(transport, "hello")

        assert fake.closed is True
        assert transport.received  # audio was forwarded

    @pytest.mark.asyncio
    async def test_caller_supplied_tts_is_not_closed(self):
        # A caller-supplied provider's lifecycle belongs to the caller.
        fake = _FakeTTS()
        transport = _FakeTransport()

        await speak(transport, "hello", tts=fake)

        assert fake.closed is False
        assert transport.received

    @pytest.mark.asyncio
    async def test_helper_constructed_tts_closed_even_on_error(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "oa-key")
        fake = _FakeTTS()
        monkeypatch.setattr(quick, "create_tts_provider", lambda _config: fake)

        class _BoomTransport:
            async def send_audio(self, _audio: AudioChunk) -> None:
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await speak(_BoomTransport(), "hello")

        assert fake.closed is True
