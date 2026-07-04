from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path

import easycat
from easycat._public_api import LAZY_EXPORTS
from easycat.cli._app import _DOCS_ONBOARDING_RAW_GUARD_COMMANDS

PUBLIC_IMPORT_SURFACE_ROOTS = (
    Path("README.md"),
    Path("docs"),
    Path("examples"),
    Path("src/easycat/cli/scaffold/templates"),
)

PUBLIC_API_SNAPSHOT = (
    "AgentDelta",
    "AgentFinal",
    "AudioChunk",
    "AudioFormat",
    "AudioIn",
    "AudioOut",
    "AudioProcessingConfig",
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
    "ObservabilityConfig",
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
    "SessionPolicyConfig",
    "SmartTurnConfig",
    "SupervisorListenerAttached",
    "SupervisorListenerDetached",
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
    "VoiceApp",
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
    "register_stt_provider",
    "register_tts_provider",
    "require_env",
    "run",
    "run_webrtc_config_server",
    "serve_webrtc_config_sessions",
    "set_easycat_log_level",
    "wait_for_shutdown_signal",
)


TRANSPORT_EXTENSION_SURFACE = (
    "AudioQueueMixin",
    "ServerTransportBase",
    "TransportDegraded",
)


def test_public_api_snapshot() -> None:
    assert tuple(easycat.__all__) == PUBLIC_API_SNAPSHOT
    assert len(easycat.__all__) <= 95


def test_public_api_registry_tracks_snapshot() -> None:
    assert tuple(sorted(LAZY_EXPORTS)) == PUBLIC_API_SNAPSHOT


def test_type_checking_block_matches_lazy_exports() -> None:
    static = _type_checking_imports_from_init()
    registry = set(LAZY_EXPORTS.values())
    missing = registry - static
    extra = static - registry
    assert not missing, f"__init__ TYPE_CHECKING block missing: {sorted(missing)}"
    assert not extra, f"__init__ TYPE_CHECKING block has stale imports: {sorted(extra)}"


def test_public_api_contract_doc_tracks_top_level_exports() -> None:
    doc = Path("docs/public-api.md").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")

    documented = _documented_top_level_allowlist(doc)
    missing = sorted(set(easycat.__all__) - documented)
    extra = sorted(documented - set(easycat.__all__))

    assert not missing, "docs/public-api.md missing exports: " + ", ".join(missing)
    assert not extra, "docs/public-api.md lists non-exported names: " + ", ".join(extra)
    assert "[public API contract](docs/public-api.md)" in readme
    assert "PUBLIC_API_SNAPSHOT" in doc
    assert "Reader-facing snippets in the root README" in doc
    assert "scaffold templates must use this allowlist" in doc
    assert "uv run easycat docs" in doc
    assert "uv run easycat docs --json" in doc
    assert "uv run easycat explain json-schema" in doc
    assert "uv run pytest tests/test_public_api.py" in doc
    assert "just guard-docs" in doc
    assert _DOCS_ONBOARDING_RAW_GUARD_COMMANDS[0] in doc
    assert "If `just` is not installed" in doc
    assert "[`CONTRIBUTING.md`](../CONTRIBUTING.md#the-development-loop)" in doc


def test_public_api_contract_doc_has_unique_allowlist_entries() -> None:
    doc = Path("docs/public-api.md").read_text(encoding="utf-8")
    documented = _documented_top_level_allowlist_entries(doc)
    duplicates = sorted({name for name in documented if documented.count(name) > 1})

    assert not duplicates, "docs/public-api.md duplicates exports: " + ", ".join(duplicates)


def test_public_api_contract_doc_teaches_entry_and_lifecycle_paths() -> None:
    doc = Path("docs/public-api.md").read_text(encoding="utf-8")
    preferred = doc.split("## Preferred Imports", 1)[1].split("## Top-Level Allowlist", 1)[0]

    assert "from easycat import EasyConfig, run" in preferred
    assert "from easycat import EasyConfig, STTFinal, create_session" in preferred
    assert "from easycat.helpers import run_session" in preferred
    assert "session = create_session(EasyConfig.mic(agent=agent))" in preferred
    assert "session.subscribe_event(STTFinal" in preferred
    assert "run_session(session)" in preferred
    assert "subscription.unsubscribe()" in preferred
    assert "Session.from_providers(" in preferred
    assert "SessionConfig" not in preferred


def test_transport_extension_surface_is_public_and_documented() -> None:
    """`easycat.transports` exposes the out-of-tree transport building blocks.

    Provider authors subclass ``AudioQueueMixin`` / ``ServerTransportBase``
    and subscribe to ``TransportDegraded``; the extension surface must stay
    importable from ``easycat.transports`` and documented in
    ``docs/public-api.md``.
    """
    import easycat.transports as transports

    doc = Path("docs/public-api.md").read_text(encoding="utf-8")
    try:
        section = doc.split("## Transport Extension Surface", 1)[1].split(
            "## Top-Level Allowlist", 1
        )[0]
    except IndexError as exc:
        raise AssertionError(
            "docs/public-api.md is missing the Transport Extension Surface section"
        ) from exc

    for name in TRANSPORT_EXTENSION_SURFACE:
        assert name in transports.__all__, f"easycat.transports.__all__ missing {name}"
        assert getattr(transports, name) is not None
        assert f"`{name}`" in section, f"docs/public-api.md does not document {name}"

    from easycat.events import TransportDegraded as events_transport_degraded
    from easycat.transports import TransportDegraded as transports_transport_degraded

    assert transports_transport_degraded is events_transport_degraded
    assert "extending/" in section


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
    # The recipes.py docstring and public API docs advertise these as the canonical
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
    pollute ``sys.modules``. ``easycat.config`` may load the two telephony
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
    from easycat.quick import speak as quick_speak
    from easycat.quick import transcribe_file as quick_transcribe_file
    from easycat.recipes import speak, transcribe_file
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
    assert quick_speak is speak
    assert quick_transcribe_file is transcribe_file
    assert split_at_sentence_boundaries("Hello world. ") == ("Hello world. ", "")


def _easycat_imports_from_ast(source: str, filename: str) -> Iterable[tuple[int, str]]:
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "easycat":
            for alias in node.names:
                if alias.name != "*":
                    yield node.lineno, alias.name


def _type_checking_imports_from_init() -> set[tuple[str, str]]:
    source = Path("src/easycat/__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="src/easycat/__init__.py")
    pairs: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            continue
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.ImportFrom) and stmt.module:
                for alias in stmt.names:
                    if alias.name != "*":
                        pairs.add((stmt.module, alias.name))
    return pairs


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


def _documented_top_level_allowlist(doc: str) -> set[str]:
    return set(_documented_top_level_allowlist_entries(doc))


def _documented_top_level_allowlist_entries(doc: str) -> list[str]:
    try:
        section = doc.split("## Top-Level Allowlist", maxsplit=1)[1]
    except IndexError as exc:
        raise AssertionError("docs/public-api.md is missing the Top-Level Allowlist") from exc

    names: list[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- `") or not stripped.endswith("`"):
            continue
        names.append(stripped.removeprefix("- `").removesuffix("`"))
    return names


def test_reader_facing_surfaces_use_only_public_top_level_imports() -> None:
    public = set(easycat.__all__)
    violations: list[str] = []

    for root in PUBLIC_IMPORT_SURFACE_ROOTS:
        python_paths = [root] if root.is_file() and root.suffix == ".py" else root.rglob("*.py")
        markdown_paths = [root] if root.is_file() and root.suffix == ".md" else root.rglob("*.md")

        for path in sorted(python_paths):
            for line, name in _easycat_imports_from_ast(
                path.read_text(encoding="utf-8"), str(path)
            ):
                if name not in public:
                    violations.append(f"{path}:{line}: {name}")
        for path in sorted(markdown_paths):
            for line, name in _easycat_imports_from_markdown(path):
                if name not in public:
                    violations.append(f"{path}:{line}: {name}")

    assert not violations, "Non-public `from easycat import ...` usages:\n" + "\n".join(violations)
