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
    "Session",
    "SessionActions",
    "SessionAudioBroadcaster",
    "SessionConfig",
    "SessionManager",
    "SmartTurnConfig",
    "TTSAudio",
    "TTSMarkers",
    "TTSProvider",
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
    "create_session",
    "create_text_session",
    "default_pronunciation_processors",
    "export_debug_bundle",
    "require_env",
    "run",
    "set_easycat_log_level",
    "wait_for_shutdown_signal",
)


def test_public_api_snapshot() -> None:
    assert tuple(easycat.__all__) == PUBLIC_API_SNAPSHOT
    assert len(easycat.__all__) <= 80


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


def test_public_api_symbols_resolve() -> None:
    for name in easycat.__all__:
        assert getattr(easycat, name) is not None


def test_culled_symbols_remain_available_from_modules() -> None:
    from easycat.debug.testing import load_bundle
    from easycat.integrations.agents import AgentRunner, AgentRunnerConfig
    from easycat.quick import speak, transcribe_file
    from easycat.session import split_at_sentence_boundaries
    from easycat.session.actions import CoreSessionActionExecutor
    from easycat.stt.factory import STTProviderConfig, create_stt_provider

    assert "AgentRunner" not in easycat.__all__
    assert "AgentRunnerConfig" not in easycat.__all__
    assert "CoreSessionActionExecutor" not in easycat.__all__
    assert "STTProviderConfig" not in easycat.__all__
    assert "load_bundle" not in easycat.__all__
    assert "speak" not in easycat.__all__
    assert "transcribe_file" not in easycat.__all__

    assert AgentRunner.__name__ == "AgentRunner"
    assert AgentRunnerConfig.__name__ == "AgentRunnerConfig"
    assert CoreSessionActionExecutor.__name__ == "CoreSessionActionExecutor"
    assert STTProviderConfig.__name__ == "STTProviderConfig"
    assert create_stt_provider.__name__ == "create_stt_provider"
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
