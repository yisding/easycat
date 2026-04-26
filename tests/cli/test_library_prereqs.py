"""Plans 12 & 13 — library prereqs (run, string-keyed providers).

The CLI's scaffolded templates assume that:

* ``easycat.run(config)`` exists, calls ``create_session``,
  ``session.start``, ``session.shutdown``, and wires signal handlers.
* ``EasyConfig(stt="<provider>/<model>", tts="...")`` resolves
  strings to typed configs using env-var API keys and raises
  ``EASYCAT_E104``/``EASYCAT_E203`` on unknowns / missing keys.

See ``TEST_PLANS.md`` §12 and §13.
"""

from __future__ import annotations

import asyncio
import signal
from unittest.mock import patch

import pytest

import easycat
from easycat import EasyConfig
from easycat.errors import EasyCatError
from easycat.stt.deepgram_provider import DeepgramSTTConfig
from easycat.stt.factory import parse_stt_string
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTTConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTSConfig
from easycat.tts.factory import parse_tts_string
from easycat.tts.openai_tts import OpenAITTSConfig

# ── Plan 12: easycat.run() lifecycle ─────────────────────────────


def test_run_is_exposed_publicly() -> None:
    assert callable(easycat.run)
    # Library consumers should be able to import it as a top-level name.
    from easycat import run as _

    assert _ is easycat.run


class _StubSession:
    """Session double that records lifecycle calls."""

    def __init__(self) -> None:
        self.events: list[str] = []

    async def start(self) -> None:
        self.events.append("start")

    async def shutdown(self) -> None:
        self.events.append("shutdown")

    def subscribe_event(self, *a, **kw) -> None:  # noqa: ANN002,ANN003
        self.events.append("subscribe")


def test_run_calls_start_and_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy-path lifecycle: create → start → wait → shutdown."""
    session = _StubSession()

    # Replace create_session with a stub.
    monkeypatch.setattr("easycat.config.create_session", lambda cfg: session)

    # Suppress the feedback hook; it's tested separately.
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "yes")

    # Fire SIGTERM a tick after start() completes so _run exits cleanly.
    async def trigger_shutdown() -> None:
        await asyncio.sleep(0.05)
        import os

        os.kill(os.getpid(), signal.SIGTERM)

    # Patch asyncio.run so we can drive the coroutine inside a test loop
    # that also schedules our shutdown trigger.
    real_run = asyncio.run

    def fake_run(coro):
        async def main():
            await asyncio.gather(coro, trigger_shutdown())

        return real_run(main())

    monkeypatch.setattr("easycat.helpers.asyncio.run", fake_run)

    easycat.run(EasyConfig(openai_api_key="stub"))
    assert "start" in session.events
    assert "shutdown" in session.events
    # Shutdown must come after start.
    assert session.events.index("start") < session.events.index("shutdown")


def test_run_does_not_attach_feedback_under_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PYTEST_CURRENT_TEST suppresses the TTY feedback hook so tests
    that exercise ``run()`` don't get stray prints."""
    session = _StubSession()
    monkeypatch.setattr("easycat.config.create_session", lambda cfg: session)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "yes")

    with patch("easycat.helpers.attach_runtime_feedback") as attach:
        # Synthesize a shutdown so the coroutine returns.
        async def trigger() -> None:
            await asyncio.sleep(0.02)
            import os

            os.kill(os.getpid(), signal.SIGTERM)

        real_run = asyncio.run

        def fake_run(coro):
            async def main():
                await asyncio.gather(coro, trigger())

            return real_run(main())

        monkeypatch.setattr("easycat.helpers.asyncio.run", fake_run)
        easycat.run(EasyConfig(openai_api_key="stub"))

    attach.assert_not_called()


# ── Plan 13: string-keyed provider selection ─────────────────────


def test_parse_stt_deepgram_flux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    cfg = parse_stt_string("deepgram/flux")
    assert isinstance(cfg, DeepgramSTTConfig)
    assert cfg.model == "flux"
    assert cfg.api_key == "dg-test"


def test_parse_stt_bare_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model is optional — bare provider string uses dataclass default."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = parse_stt_string("openai-realtime")
    assert isinstance(cfg, OpenAIRealtimeSTTConfig)
    assert cfg.api_key == "sk-test"


def test_parse_stt_unknown_provider_fuzzy_suggest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(EasyCatError) as excinfo:
        parse_stt_string("deepgrm/flux")
    assert excinfo.value.code == "EASYCAT_E104"
    assert "Did you mean" in excinfo.value.message


def test_parse_stt_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    with pytest.raises(EasyCatError) as excinfo:
        parse_stt_string("deepgram/flux")
    assert excinfo.value.code == "EASYCAT_E203"
    assert "DEEPGRAM_API_KEY" in excinfo.value.message


def test_parse_tts_elevenlabs_uses_model_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ElevenLabs names its field ``model_id`` — we bridge the name."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test")
    cfg = parse_tts_string("elevenlabs/eleven_flash_v2_5")
    assert isinstance(cfg, ElevenLabsTTSConfig)
    assert cfg.model_id == "eleven_flash_v2_5"


def test_parse_tts_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = parse_tts_string("openai")
    assert isinstance(cfg, OpenAITTSConfig)


def test_easyconfig_resolves_string_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: string-keyed provider selection in EasyConfig."""
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = EasyConfig(stt="deepgram/flux", tts="openai", agent=object())
    assert isinstance(cfg.stt, DeepgramSTTConfig)
    assert cfg.stt.model == "flux"
    assert isinstance(cfg.tts, OpenAITTSConfig)


def test_easyconfig_env_autodetect_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero-config case: OPENAI_API_KEY env var picks the OpenAI chain."""
    for var in ("DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = EasyConfig(agent=object())
    assert isinstance(cfg.stt, OpenAIRealtimeSTTConfig)
    assert isinstance(cfg.tts, OpenAITTSConfig)


def test_easyconfig_typed_config_takes_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit typed STTConfig short-circuits the string parser."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    typed = DeepgramSTTConfig(api_key="dg-manual", model="nova-2")
    cfg = EasyConfig(stt=typed, tts="openai", agent=object())
    assert cfg.stt is typed


def test_easyconfig_programmatic_openai_key_for_string_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`openai_api_key="sk..."` works without OPENAI_API_KEY also exported."""
    for var in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    cfg = EasyConfig(
        openai_api_key="sk-programmatic",
        stt="openai-realtime",
        tts="openai",
        agent=object(),
    )
    assert isinstance(cfg.stt, OpenAIRealtimeSTTConfig)
    assert cfg.stt.api_key == "sk-programmatic"
    assert isinstance(cfg.tts, OpenAITTSConfig)
    assert cfg.tts.api_key == "sk-programmatic"


def test_easyconfig_swap_just_stt_keeps_openai_tts_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`stt="deepgram/flux"` + OPENAI_API_KEY autofills the OpenAI TTS chain."""
    for var in ("ELEVENLABS_API_KEY",):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    cfg = EasyConfig(stt="deepgram/flux", agent=object())
    assert isinstance(cfg.stt, DeepgramSTTConfig)
    assert cfg.stt.model == "flux"
    # The autodetect path should still fill in OpenAI TTS even though stt
    # is no longer None when the OpenAI defaults block runs.
    assert isinstance(cfg.tts, OpenAITTSConfig)
