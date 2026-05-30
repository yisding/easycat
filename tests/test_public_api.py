from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path

import easycat

PUBLIC_API_SNAPSHOT = (
    "AgentDelta",
    "AgentFinal",
    "AudioChunk",
    "AudioFormat",
    "AudioIn",
    "AudioOut",
    "BotStartedSpeaking",
    "BotStoppedSpeaking",
    "CallAnswered",
    "CallEnded",
    "CallFailed",
    "CallIdentity",
    "CancelToken",
    "EasyCatError",
    "EasyConfig",
    "EchoCanceller",
    "Error",
    "ErrorEntry",
    "ErrorStage",
    "Event",
    "EventBus",
    "ICEServer",
    "Interruption",
    "JournalRecordKind",
    "LocalTransportConfig",
    "MarkdownStripProcessor",
    "NoiseReducer",
    "NoiseReducerConfig",
    "OutboundCallConfig",
    "PCM16_MONO_16K",
    "PCM16_MONO_24K",
    "PCM16_MONO_48K",
    "PCM16_MONO_8K",
    "PauseProcessor",
    "PhoneticReplacementProcessor",
    "RunBundle",
    "STTFinal",
    "STTPartial",
    "STTProvider",
    "STTProviderConfig",
    "Session",
    "SessionActions",
    "SessionAudioBroadcaster",
    "SessionConfig",
    "SessionManager",
    "SmartTurnConfig",
    "TTSAudio",
    "TTSMarkers",
    "TTSProvider",
    "TTSProviderConfig",
    "TelephonyConfig",
    "Transport",
    "TurnEnded",
    "TurnManagerConfig",
    "TurnMode",
    "TurnStarted",
    "TwilioConnectionTransport",
    "TwilioSessionActionConfig",
    "VADConfig",
    "VADProvider",
    "VADStartSpeaking",
    "VADStopSpeaking",
    "VoicemailDetectionConfig",
    "WebRTCTransportConfig",
    "WebSocketConnectionTransport",
    "WebSocketTransportConfig",
    "WebTransportConnectionTransport",
    "WebTransportServer",
    "WebTransportTransportConfig",
    "attach_runtime_feedback",
    "auto_adapt_agent",
    "available_stt_providers",
    "available_tts_providers",
    "create_noise_reducer",
    "create_session",
    "create_stt_provider",
    "create_text_session",
    "create_tts_provider",
    "create_vad",
    "default_pronunciation_processors",
    "export_debug_bundle",
    "require_env",
    "run",
    "set_easycat_log_level",
    "wait_for_shutdown_signal",
)


def test_public_api_snapshot() -> None:
    assert tuple(easycat.__all__) == PUBLIC_API_SNAPSHOT
    assert len(easycat.__all__) <= 85


def test_curated_public_api_lazy_imports() -> None:
    from easycat import (
        EasyConfig,
        MarkdownStripProcessor,
        SessionConfig,
        create_session,
    )

    assert EasyConfig.__name__ == "EasyConfig"
    assert SessionConfig.__name__ == "SessionConfig"
    assert MarkdownStripProcessor.__name__ == "MarkdownStripProcessor"
    assert create_session.__name__ == "create_session"


def test_documented_factory_surface_is_importable() -> None:
    # The quick.py docstring and CLAUDE.md advertise these as the canonical
    # top-level factory functions, so they must resolve from ``easycat``.
    from easycat import (
        STTProviderConfig,
        TTSProviderConfig,
        create_noise_reducer,
        create_session,
        create_stt_provider,
        create_tts_provider,
        create_vad,
    )

    assert create_session.__name__ == "create_session"
    assert create_stt_provider.__name__ == "create_stt_provider"
    assert create_tts_provider.__name__ == "create_tts_provider"
    assert create_vad.__name__ == "create_vad"
    assert create_noise_reducer.__name__ == "create_noise_reducer"
    assert STTProviderConfig.__name__ == "STTProviderConfig"
    assert TTSProviderConfig.__name__ == "TTSProviderConfig"


def test_public_api_symbols_resolve() -> None:
    for name in easycat.__all__:
        assert getattr(easycat, name) is not None


def test_touching_easyconfig_does_not_eager_load_telephony_stack() -> None:
    """Cold-start guard: ``import easycat; easycat.EasyConfig`` must not drag
    in the telephony runtime stack or heavy transport SDKs.

    Runs in a fresh interpreter so the test process's own imports don't
    pollute ``sys.modules``. ``config.py`` may load the two telephony
    *config-only* submodules (``dtmf`` / ``voicemail``) for its dataclass
    defaults, but the runtime classifiers (state machines, IVR navigator,
    outbound manager, screening, number health, retries, twiml, session
    actions, compliance) must stay lazy.
    """
    import subprocess
    import sys

    code = (
        "import sys, easycat\n"
        "easycat.EasyConfig\n"
        "tele = sorted(m for m in sys.modules if m.startswith('easycat.telephony'))\n"
        "print('\\n'.join(tele))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = {line for line in result.stdout.splitlines() if line}
    # Only the package and the two config-only submodules are allowed.
    allowed = {
        "easycat.telephony",
        "easycat.telephony.dtmf",
        "easycat.telephony.voicemail",
    }
    forbidden = loaded - allowed
    assert not forbidden, f"EasyConfig eager-loaded telephony runtime modules: {sorted(forbidden)}"
    # The runtime classifiers in particular must not be present.
    for mod in (
        "easycat.telephony.call_state",
        "easycat.telephony.outbound",
        "easycat.telephony.ivr",
        "easycat.telephony.screening",
        "easycat.telephony.number_health",
        "easycat.telephony.retry",
        "easycat.telephony.session_actions",
        "easycat.telephony.twiml",
    ):
        assert mod not in loaded


def test_culled_symbols_remain_available_from_modules() -> None:
    from easycat.debug.testing import load_bundle
    from easycat.integrations.agents import AgentRunner, AgentRunnerConfig
    from easycat.quick import speak, transcribe_file
    from easycat.session import split_at_sentence_boundaries
    from easycat.session.actions import CoreSessionActionExecutor

    assert "AgentRunner" not in easycat.__all__
    assert "AgentRunnerConfig" not in easycat.__all__
    assert "CoreSessionActionExecutor" not in easycat.__all__
    assert "load_bundle" not in easycat.__all__
    assert "speak" not in easycat.__all__
    assert "transcribe_file" not in easycat.__all__

    assert AgentRunner.__name__ == "AgentRunner"
    assert AgentRunnerConfig.__name__ == "AgentRunnerConfig"
    assert CoreSessionActionExecutor.__name__ == "CoreSessionActionExecutor"
    assert load_bundle.__name__ == "load_bundle"
    assert speak.__name__ == "speak"
    assert transcribe_file.__name__ == "transcribe_file"
    assert split_at_sentence_boundaries("Hello world. ") == ("Hello world. ", "")


def _easycat_imports_from_ast(source: str, filename: str) -> Iterable[tuple[int, str]]:
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "easycat":
            for alias in node.names:
                if alias.name != "*":
                    yield node.lineno, alias.name


def _easycat_imports_from_markdown(path: Path) -> Iterable[tuple[int, str]]:
    text = path.read_text(encoding="utf-8")
    for match in re.finditer(r"```(?:python|py)?\n(?P<code>.*?)```", text, flags=re.DOTALL):
        code = match.group("code")
        if "from easycat import" not in code:
            continue
        start_line = text[: match.start("code")].count("\n") + 1
        try:
            for line, name in _easycat_imports_from_ast(code, f"{path}:{start_line}"):
                yield start_line + line - 1, name
        except SyntaxError:
            for offset, line in enumerate(code.splitlines(), start=start_line):
                stripped = line.strip()
                if not stripped.startswith("from easycat import "):
                    continue
                imported = stripped.removeprefix("from easycat import ").strip("()")
                for part in imported.split(","):
                    name = part.strip().split(" as ", 1)[0]
                    if name:
                        yield offset, name


def test_docs_and_examples_use_only_public_top_level_imports() -> None:
    public = set(easycat.__all__)
    violations: list[str] = []

    for root in (Path("docs"), Path("examples")):
        for path in sorted(root.rglob("*.py")):
            for line, name in _easycat_imports_from_ast(
                path.read_text(encoding="utf-8"), str(path)
            ):
                if name not in public:
                    violations.append(f"{path}:{line}: {name}")
        for path in sorted(root.rglob("*.md")):
            for line, name in _easycat_imports_from_markdown(path):
                if name not in public:
                    violations.append(f"{path}:{line}: {name}")

    assert not violations, "Non-public `from easycat import ...` usages:\n" + "\n".join(violations)
