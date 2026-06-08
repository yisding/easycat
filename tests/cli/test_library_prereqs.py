"""Plans 14 & 15 — library prereqs (run, string-keyed providers).

The CLI's scaffolded templates assume that:

* ``easycat.run(config)`` exists, calls ``create_session``,
  enters the session async context, and wires signal handlers.
* ``easycat.helpers.run_session(session)`` gives preconfigured sessions the
  same lifecycle/signal wrapper without rebuilding them.
* ``EasyConfig(stt="<provider>/<model>", tts="...")`` resolves
  strings to typed configs using env-var API keys and raises
  ``EASYCAT_E104``/``EASYCAT_E203`` on unknowns / missing keys.

See ``TEST_PLANS.md`` §14 and §15.
"""

from __future__ import annotations

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

# ── Plan 14: easycat.run() lifecycle ─────────────────────────────


def test_run_is_exposed_publicly() -> None:
    assert callable(easycat.run)
    # Library consumers should be able to import it as a top-level name.
    from easycat import run as _

    assert _ is easycat.run


def test_run_session_is_exposed_publicly() -> None:
    from easycat.helpers import run_session

    assert callable(run_session)


def test_run_docstring_teaches_public_context_lifecycle() -> None:
    doc = easycat.run.__doc__ or ""

    assert "async with session:" in doc
    assert "stop(force=True)" in doc
    assert "await session.shutdown()" not in doc


class _StubSession:
    """Session double that records lifecycle calls.

    ``run()`` drives teardown through the ``async with session:`` idiom,
    so the stub maps ``__aenter__``/``__aexit__`` onto ``start``/
    ``stop(force=True)`` the same way the real :class:`Session` does.
    """

    def __init__(self) -> None:
        self.events: list[str] = []

    async def start(self) -> None:
        self.events.append("start")

    async def stop(self, *, force: bool = False) -> None:
        self.events.append(f"stop(force={force})")

    async def __aenter__(self) -> _StubSession:
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:  # noqa: ANN002
        await self.stop(force=True)

    def subscribe_event(self, *a, **kw) -> None:  # noqa: ANN002,ANN003
        self.events.append("subscribe")


def _install_immediate_shutdown(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    installs: list[str] = []

    def install_shutdown(_loop, stop_event) -> bool:  # noqa: ANN001
        installs.append("install")
        stop_event.set()
        return True

    monkeypatch.setattr("easycat.helpers._install_shutdown_signal_handlers", install_shutdown)
    return installs


def test_run_uses_session_async_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy-path lifecycle: create → async-enter/start → wait → force-stop."""
    session = _StubSession()

    # Replace create_session with a stub.
    monkeypatch.setattr("easycat.config.create_session", lambda cfg: session)

    # Suppress the feedback hook; it's tested separately.
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "yes")
    installs = _install_immediate_shutdown(monkeypatch)

    easycat.run(EasyConfig(openai_api_key="stub"))
    assert installs == ["install"]
    assert "start" in session.events
    assert "stop(force=True)" in session.events
    # Stop must come after start.
    assert session.events.index("start") < session.events.index("stop(force=True)")


def test_run_session_uses_existing_session_async_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from easycat.helpers import run_session

    session = _StubSession()
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "yes")
    installs = _install_immediate_shutdown(monkeypatch)

    run_session(session)

    assert installs == ["install"]
    assert session.events == ["start", "stop(force=True)"]


def test_run_does_not_attach_feedback_under_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PYTEST_CURRENT_TEST suppresses the TTY feedback hook so tests
    that exercise ``run()`` don't get stray prints."""
    session = _StubSession()
    monkeypatch.setattr("easycat.config.create_session", lambda cfg: session)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "yes")

    with patch("easycat.helpers.attach_runtime_feedback") as attach:
        _install_immediate_shutdown(monkeypatch)
        easycat.run(EasyConfig(openai_api_key="stub"))

    attach.assert_not_called()


def test_run_session_does_not_attach_feedback_under_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from easycat.helpers import run_session

    session = _StubSession()
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "yes")

    with patch("easycat.helpers.attach_runtime_feedback") as attach:
        _install_immediate_shutdown(monkeypatch)
        run_session(session)

    attach.assert_not_called()


# ── Plan 15: string-keyed provider selection ─────────────────────


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
    assert cfg.model == "gpt-realtime-whisper"
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
