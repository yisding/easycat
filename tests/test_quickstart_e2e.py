"""Documented happy-path smoke test.

Walks the runtime behind the canonical ``VoiceApp`` quickstart advertised in README:

    VoiceApp -> EasyConfig -> Session -> one turn -> stop -> export bundle

If this test breaks, the README's quickstart is also broken — that is
the entire point of having it. It uses the existing scripted-provider
harness so it stays hermetic (no API keys, no network).
"""

from __future__ import annotations

import ast
import re
import tomllib
import zipfile
from pathlib import Path
from typing import get_args

import pytest

from easycat import (
    AgentFinal,
    EasyConfig,
    STTFinal,
    TTSAudio,
    create_session,
)

from .integration.harness import (
    EventCollector,
    QueueTransport,
    RecordingTTS,
    ScriptedSTT,
    ScriptedVAD,
    make_chunk,
    make_test_config,
    patch_provider_factories,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
_PROVIDER_DISPLAY_NAMES = {
    "cartesia": "Cartesia",
    "deepgram": "Deepgram",
    "elevenlabs": "ElevenLabs",
    "openai": "OpenAI",
    "openai-realtime": "OpenAI",
}
_VAD_DISPLAY_NAMES = {
    "funasr": "FunASR",
    "krisp": "Krisp",
    "silero": "Silero",
    "ten": "TEN VAD",
}
_NOISE_REDUCTION_DISPLAY_NAMES = {
    "krisp": "Krisp",
    "rnnoise": "RNNoise",
}
_BRIDGE_DISPLAY_NAMES = {
    "GenericWorkflowBridge": "your own async workflow",
    "LangChainBridge": "LangChain",
    "LangGraphBridge": "LangGraph",
    "LlamaAgentsBridge": "LlamaAgents",
    "OpenAIAgentsBridge": "OpenAI Agents SDK",
    "PydanticAIBridge": "PydanticAI",
    "RemoteResponsesAPIBridge": "Remote Responses API",
}
_QUICKSTART_BLOCK_RE = re.compile(
    r"### Quickstart\n.*?```python\n(?P<code>.*?)\n```",
    re.DOTALL,
)


class EchoAgent:
    async def run(self, text: str) -> str:
        return f"You said: {text}"


def _is_voice_app_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "VoiceApp"
    )


def _assigned_voice_app_names(tree: ast.AST) -> set[str]:
    app_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if not _is_voice_app_call(node.value):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        app_names.update(target.id for target in targets if isinstance(target, ast.Name))
    return app_names


def _uses_voice_app_local(source: str) -> bool:
    tree = ast.parse(source)
    app_names = _assigned_voice_app_names(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "run":
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if node.args[0].value != "local":
            continue
        receiver = node.func.value
        if isinstance(receiver, ast.Name) and receiver.id in app_names:
            return True
        if _is_voice_app_call(receiver):
            return True

    return False


def _readme_quickstart_code() -> str:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    match = _QUICKSTART_BLOCK_RE.search(readme)
    assert match is not None, "README.md canonical Quickstart code block not found"
    return match.group("code")


def _readme_section(start: str, end: str) -> str:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    return readme.split(start, 1)[1].split(end, 1)[0]


@pytest.mark.integration_local
async def test_quickstart_happy_path_one_turn_with_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """End-to-end: build session, run one turn, export a debug bundle.

    Asserts the events the README leads users to expect (STTFinal,
    AgentFinal, TTSAudio) all fire in order, and that
    ``export_debug_bundle`` produces a readable zip.
    """
    transport = QueueTransport()
    stt = ScriptedSTT(["hello world"])
    tts = RecordingTTS(chunk_sizes=(640, 640))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=EchoAgent())
    # Override debug=off default so export_debug_bundle has a journal.
    config = EasyConfig(
        stt=config.stt,
        tts=config.tts,
        transport=transport,
        agent=EchoAgent(),
        turn_taking=config.turn_taking,
        debug="light",
    )

    session = create_session(config)

    collector = EventCollector(session.event_bus)
    collector.subscribe(STTFinal, AgentFinal, TTSAudio)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk(), make_chunk())
        stt_final = await collector.wait_for(STTFinal, timeout=3.0)
        agent_final = await collector.wait_for(AgentFinal, timeout=3.0)
        tts_audio = await collector.wait_for(TTSAudio, timeout=3.0)

        assert stt_final.text == "hello world"
        assert agent_final.text == "You said: hello world"
        assert tts_audio.chunk.data  # non-empty payload
    finally:
        await session.stop()

    bundle_path = tmp_path / "quickstart.zip"
    session.export_debug_bundle(str(bundle_path))

    assert bundle_path.exists(), "export_debug_bundle did not write a file"
    with zipfile.ZipFile(bundle_path) as zf:
        names = zf.namelist()
        assert "manifest.json" in names, f"bundle missing manifest: {names}"
        assert any(name.startswith("journal") for name in names), (
            f"bundle missing journal: {names}"
        )


async def test_quickstart_public_api_imports_resolve() -> None:
    """The literal imports the README quickstart uses must resolve.

    Catches drift between ``easycat.__init__`` lazy registrations and
    the symbols documented in README.md.
    """
    from easycat import (  # noqa: F401
        EasyConfig,
        Session,
        SessionConfig,
        create_session,
        create_text_session,
    )
    from easycat.integrations.agents import (  # noqa: F401
        AgentRunner,
        AgentRunnerConfig,
        OpenAIAgentsBridge,
        PydanticAIBridge,
        auto_adapt_agent,
    )


def test_documented_canonical_voice_quickstart_shape_stays_consistent() -> None:
    """README, first example, and scaffold template all teach the same entry shape."""
    sources = {
        "README.md Quickstart": _readme_quickstart_code(),
        "examples/openai_agents_voice.py": (
            REPO_ROOT / "examples" / "openai_agents_voice.py"
        ).read_text(encoding="utf-8"),
        "openai-agents scaffold template": (
            REPO_ROOT / "src/easycat/cli/scaffold/templates/openai-agents/agent.py"
        ).read_text(encoding="utf-8"),
    }
    missing = sorted(name for name, source in sources.items() if not _uses_voice_app_local(source))

    assert not missing, "Canonical quickstart shape drifted in: " + ", ".join(missing)

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "Quickstart (EasyConfig)" not in readme
    assert "Quickstart (VoiceApp)" not in readme
    assert 'same `VoiceApp(...).run("local")` shape' in readme


def test_readme_choose_your_path_routes_primary_onboarding_surfaces() -> None:
    section = _readme_section("## Choose Your Path", "## Learn the pipeline from scratch")
    normalized = re.sub(r"\s+", " ", section)
    expected_rows = {
        "Run a local mic/speaker voice bot": ("[Install](#install)", "uv run easycat doctor"),
        "No mic or API key yet": (
            "[Journal demo](examples/journal_demo.py)",
            "[hardware-free teaching spine](docs/teaching/#hardware-free-checkpoint-spine)",
            "uv run easycat console --voice-demo",
            "uv run python examples/journal_demo.py",
            "uv run python docs/teaching/offline_spine.py --run --jobs 4",
        ),
        "Learn the pipeline step by step": (
            "[Teaching ladder](docs/teaching/)",
            "starting-point table",
        ),
        "Learn EasyCat feature by feature": (
            "[EasyCat feature ladder](docs/using-easycat/)",
            "VoiceApp",
        ),
        "Choose a runnable example": (
            "[Examples matrix](examples/README.md)",
            "no-key, browser, provider, or debugging examples",
        ),
        "Scaffold a new app": (
            "[CLI and scaffolds](#cli)",
            "uv run easycat init --list-templates",
        ),
        "Contribute or validate a change": (
            "[Developer textbook](docs/development/)",
            "[Contributing](CONTRIBUTING.md)",
            "uv run easycat validate quick",
        ),
        "Maintain architecture, package boundaries, or coding-agent context": (
            "[Architecture map](CLAUDE.md) and [agent guide](AGENTS.md)",
            "uv run easycat docs --audience maintainers",
        ),
        "Operate or debug sessions": (
            "[Observability](docs/observability.md)",
            "easycat bundles list",
        ),
    }

    # Route existence and command-hint validity for this section are covered by
    # tests/docs/test_route_registry.py::test_docs_index_routes_primary_reader_paths,
    # ::test_start_here_docs_route_tracks_root_path_chooser_commands, and
    # tests/docs/test_command_hints.py::test_root_path_chooser_command_hints_are_locally_valid.
    for goal, (link, *first_moves) in expected_rows.items():
        assert goal in section
        assert link in section
        for first_move in first_moves:
            assert first_move in normalized
    assert "[validation workflow](docs/validation.md)" in section
    assert "(#validation-workflow)" not in section


def test_readme_quickstart_leads_and_install_block_uses_env_convention() -> None:
    """One local VoiceApp quickstart leads and setup preserves existing secrets."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert readme.count("## Install") == 1
    quickstart_index = readme.index("### Quickstart")
    install_index = readme.index("## Install")
    chooser_index = readme.index("## Choose Your Path")
    cli_index = readme.index("## CLI")

    assert quickstart_index < install_index < chooser_index < cli_index
    assert 'dependencies = ["easycat[quickstart]"]' in readme
    assert 'git = "https://github.com/yisding/easycat.git"' in readme
    assert "uv run easycat doctor" in readme
    assert "uv run python examples/openai_agents_voice.py" in readme
    assert "uv run easycat doctor --env-file .env" in readme
    assert "uv run --env-file .env python examples/openai_agents_voice.py" in readme

    voice_app_block = readme.split("### Quickstart", 1)[1].split("## Install", 1)[0]
    assert 'app.run("local")' in voice_app_block
    assert "from easycat import VoiceApp" in voice_app_block

    repo_block = readme.split("For this repository, the commands below go", 1)[1]
    repo_commands = repo_block.split("```bash", 1)[1].split("```", 1)[0].strip().splitlines()
    install_command = repo_commands[0]
    assert "--extra quickstart" in install_command
    assert repo_commands == [
        "uv sync --extra quickstart --group dev",
        "test -e .env || cp .env.example .env",
        "uv run easycat doctor --env-file .env",
        "uv run --env-file .env python examples/openai_agents_voice.py",
    ]
    assert "echo 'OPENAI_API_KEY" not in readme
    assert "uv run easycat console --voice-demo" in readme

    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=" in env_example
    assert "sk-" not in env_example


def test_browser_playground_fast_path_runs_doctor_preflight() -> None:
    section = (REPO_ROOT / "docs" / "browser-playground.md").read_text(encoding="utf-8")

    expected_order = (
        "uv sync --extra quickstart --extra webrtc --group dev",
        "test -e .env || cp .env.example .env",
        "uv run easycat doctor --env-file .env",
        "uv run --env-file .env easycat serve",
        "http://localhost:8080",
    )
    cursor = -1
    for term in expected_order:
        index = section.index(term)
        assert index > cursor
        cursor = index

    assert "uv run easycat doctor --env-file .env" in section
    assert "uv run --env-file .env easycat serve" in section


def test_readme_pydantic_ai_v2_requirement_matches_pyproject() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = pyproject["project"]["optional-dependencies"]["pydantic-ai-v2"]
    specs = [dep for dep in deps if dep.startswith("pydantic-ai>=")]
    assert len(specs) == 1

    install_guide = (REPO_ROOT / "docs" / "install.md").read_text(encoding="utf-8")
    assert "`pydantic-ai-v2` extra installs" in install_guide
    assert specs[0] in install_guide


def test_readme_intro_tracks_public_agent_bridge_surface() -> None:
    from easycat.integrations import agents as agent_integrations

    bridge_names = {
        name
        for name in agent_integrations.__all__
        if name.endswith("Bridge") and name != "ExternalAgentBridge"
    }
    missing_display_names = sorted(bridge_names - _BRIDGE_DISPLAY_NAMES.keys())
    assert not missing_display_names, "README intro bridge display map missing: " + ", ".join(
        missing_display_names
    )

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    intro = readme.split("## Learn the pipeline", 1)[0]
    missing_intro_names = sorted(
        display_name
        for bridge_name, display_name in _BRIDGE_DISPLAY_NAMES.items()
        if bridge_name in bridge_names and display_name not in intro
    )

    assert not missing_intro_names, (
        "README intro missing public agent bridge labels: " + ", ".join(missing_intro_names)
    )
    assert "OpenAI Agents SDK, PydanticAI agents, or PydanticAI workflows" not in intro


def test_readme_bring_your_own_agent_tracks_auto_adapt_surface() -> None:
    from easycat.integrations.agents._factory import auto_adapt_agent

    doc = auto_adapt_agent.__doc__ or ""
    auto_detected_bridges = {
        bridge_name
        for bridge_name in _BRIDGE_DISPLAY_NAMES
        if bridge_name in doc and bridge_name != "PydanticAIBridge"
    }
    # PydanticAIBridge appears in the pydantic_graph explicit-construction
    # warning too, so require it separately through the Agent-mode bullet.
    assert "pydantic_ai.Agent" in doc
    auto_detected_bridges.add("PydanticAIBridge")

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Build beyond the quickstart", 1)[1].split("Detailed routes:", 1)[0]
    normalized_section = re.sub(r"\s+", " ", section)

    missing_display_names = sorted(
        display_name
        for bridge_name, display_name in _BRIDGE_DISPLAY_NAMES.items()
        if bridge_name in auto_detected_bridges and display_name not in normalized_section
    )

    assert not missing_display_names, (
        "README advanced route missing auto-adapt labels: " + ", ".join(missing_display_names)
    )
    assert "OpenAI Agents SDK and PydanticAI objects" not in normalized_section


def test_agent_bridge_guide_owns_framework_specific_guidance() -> None:
    """Keep framework detail on one maintained page instead of duplicating the README."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    guide = (REPO_ROOT / "docs" / "using-easycat" / "05-agent-bridges" / "README.md").read_text(
        encoding="utf-8"
    )

    assert "[agent bridges](docs/using-easycat/05-agent-bridges/)" in readme
    for term in (
        "OpenAI Agents SDK",
        "PydanticAI",
        "LangChain",
        "LangGraph",
        "LlamaAgents",
        "Remote Responses API",
        "Auto-detection is the default path",
        "Construct a bridge when you need bridge options",
    ):
        assert term in guide

    assert 'openai_api_key="your-api-key"' not in guide
    assert "Session(SessionConfig(" not in guide


def test_readme_python_snippets_do_not_embed_placeholder_api_keys() -> None:
    """README should keep provider keys out of snippets and shell history."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert 'export OPENAI_API_KEY="your-api-key"' not in readme
    assert "test -e .env || cp .env.example .env" in readme
    assert 'openai_api_key="your-api-key"' not in readme
    assert 'openai_api_key="…"' not in readme


def test_cli_reference_lists_registered_top_level_commands() -> None:
    from easycat.cli import _app
    from easycat.cli.debug.bundles import bundles_app

    _app._register_commands()
    command_names = {command.name for command in _app.app.registered_commands}
    command_names.update(group.name for group in _app.app.registered_groups)
    command_names.discard(None)

    cli_section = (REPO_ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    cli_lines = set(cli_section.splitlines())
    normalized_cli_section = re.sub(r"\s+", " ", cli_section)

    assert "installed CLI form" in cli_section
    assert "base `easycat[...]` package requirement and extras" in normalized_cli_section
    assert "required environment variables" in normalized_cli_section
    assert "optional environment knobs" in normalized_cli_section
    assert "generated files" in normalized_cli_section
    assert "copyable create/preflight/check/fix/docs/json-schema/run commands" in (
        normalized_cli_section
    )
    assert "uv run easycat doctor" in cli_section
    assert "easycat doctor --env-file .env" in cli_section
    expected_cli_lines = (
        (
            "easycat init --list-templates # compare templates, base package requirements, "
            "env vars, files, preflight/check/fix/docs/json-schema/run commands"
        ),
        "easycat init --list-templates --json # emit the machine-readable template catalog",
        "easycat doctor           # check API keys, optional extras, provider reachability",
        "easycat doctor --json    # emit machine-readable environment checks",
        "easycat doctor --env-file .env --json # emit checks with project .env loaded",
        "easycat docs             # list route labels and available audience filters",
        "easycat docs --verbose   # expand every route with descriptions and command hints",
        "easycat docs --audience learners # expand routes for one reader audience or broad role",
        "easycat docs --audience learners --json # emit a filtered docs route map for learners",
        "easycat docs --json      # emit docs routes, audiences, and command hints for automation",
        ("easycat docs --audience app-builders # filter docs to scaffold and app-building routes"),
        (
            "easycat docs --audience app-builders --json # emit a filtered docs route map "
            "for app builders"
        ),
        ("easycat docs --audience operators # filter docs to deployment and observability routes"),
        (
            "easycat docs --audience operators --json # emit a filtered docs route map "
            "for operators"
        ),
        (
            "easycat docs --audience maintainers # filter docs to architecture and "
            "maintenance routes"
        ),
        (
            "easycat docs --audience maintainers --json # emit a filtered docs route map "
            "for maintainers"
        ),
        ("easycat docs --audience coding-agents # filter docs to repository coding-agent routes"),
        (
            "easycat docs --audience coding-agents --json # emit a filtered docs route map "
            "for coding agents"
        ),
        "easycat explain E102     # look up errors and CLI schema topics",
        "easycat explain json-schema # document the --json envelope and command metadata",
        "easycat bundles list      # list captured debug bundles and crash dumps",
        "easycat bundles list --json # emit machine-readable bundle list",
        "easycat bundles show PATH # summarise a debug bundle or SQLite journal",
        "easycat bundles show PATH --json # emit machine-readable bundle/journal summary",
        "easycat bundles export PATH # write a redacted coding-agent context pack",
        "easycat bundles export PATH --output DIR --json # emit context-pack metadata",
        "easycat inspect PATH      # summarise a debug bundle or SQLite journal",
        "easycat inspect PATH --json # emit machine-readable bundle/journal summary",
        "easycat replay PATH       # replay a debug bundle or SQLite journal",
        "easycat replay PATH --json # emit machine-readable replay summary",
    )
    for line in expected_cli_lines:
        assert line in cli_section
    stale_cli_lines = (
        "easycat doctor           # check API keys, Python version, optional extras",
        "easycat docs             # show documentation entry points",
        "easycat docs --audience learners # show docs for one reader audience",
        "easycat docs --audience maintainers --json # emit a filtered docs route map",
        "easycat docs             # show docs for learning, validation, operations",
        "easycat docs             # show quickstart, examples, validation, operations",
        "easycat docs             # show quickstart, examples, and teaching routes",
        "easycat explain E102     # look up an EasyCat error code",
    )
    for line in stale_cli_lines:
        assert line not in cli_lines
    assert "easycat docs --json" in cli_section
    assert "easycat docs --audience learners" in cli_section
    assert "easycat docs --audience learners --json" in cli_section
    assert "easycat docs --audience app-builders" in cli_section
    assert "easycat docs --audience app-builders --json" in cli_section
    assert "easycat docs --audience operators" in cli_section
    assert "easycat docs --audience operators --json" in cli_section
    assert "easycat docs --audience maintainers" in cli_section
    assert "easycat docs --audience maintainers --json" in cli_section
    assert "easycat docs --audience coding-agents" in cli_section
    assert "easycat docs --audience coding-agents --json" in cli_section

    missing = sorted(
        command_name
        for command_name in command_names
        if f"easycat {command_name}" not in cli_section
    )

    assert not missing, "docs/cli.md missing commands: " + ", ".join(missing)

    missing_bundle_commands = sorted(
        command.name
        for command in bundles_app.registered_commands
        if command.name is not None and f"easycat bundles {command.name}" not in cli_section
    )

    assert not missing_bundle_commands, "docs/cli.md missing bundles commands: " + (
        ", ".join(missing_bundle_commands)
    )


def test_cli_reference_does_not_advertise_stale_bundle_commands() -> None:
    from easycat.cli.debug.bundles import bundles_app

    command_names = {command.name for command in bundles_app.registered_commands}
    command_names.discard(None)

    cli_section = (REPO_ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    advertised = set(
        re.findall(r"(?m)^easycat bundles (?P<command>[a-z][a-z0-9-]*)(?:\s|$)", cli_section)
    )
    stale = sorted(advertised - command_names)

    assert not stale, "docs/cli.md advertises stale bundles commands: " + ", ".join(stale)


def test_cli_reference_does_not_advertise_stale_top_level_commands() -> None:
    from easycat.cli import _app

    _app._register_commands()
    command_names = {command.name for command in _app.app.registered_commands}
    command_names.update(group.name for group in _app.app.registered_groups)
    command_names.discard(None)

    cli_section = (REPO_ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    advertised = set(re.findall(r"(?m)^easycat (?P<command>[a-z][a-z0-9-]*)(?:\s|$)", cli_section))
    stale = sorted(advertised - command_names)

    assert not stale, "docs/cli.md advertises stale commands: " + ", ".join(stale)


def test_validation_workflow_doc_lists_registered_validate_commands() -> None:
    from typer.main import get_command

    from easycat.cli.validate import validate_app

    command_names = set(get_command(validate_app).commands)

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Validation Workflow" not in readme
    validation_section = (REPO_ROOT / "docs" / "validation.md").read_text(encoding="utf-8")

    missing = sorted(
        command_name
        for command_name in command_names
        if f"easycat validate {command_name}" not in validation_section
    )

    assert not missing, "docs/validation.md missing validate commands: " + ", ".join(missing)
    assert "easycat validate latency --smoke" in validation_section

    advertised = set(
        re.findall(
            r"easycat validate (?P<command>[a-z][a-z0-9-]*)(?:\s|$)",
            validation_section,
        )
    )
    stale = sorted(advertised - command_names)
    assert not stale, "docs/validation.md advertises stale validate commands: " + ", ".join(stale)

    validation_blocks = re.findall(r"```bash\n(.*?)\n```", validation_section, flags=re.DOTALL)
    commands = [
        line.strip()
        for block in validation_blocks
        for line in block.splitlines()
        if "easycat validate" in line
    ]
    assert commands
    assert all(command.startswith("uv run easycat validate ") for command in commands)


def test_observability_guide_teaches_stoppable_journal_tail() -> None:
    observability_section = (REPO_ROOT / "docs" / "observability.md").read_text(encoding="utf-8")

    for term in (
        "async def run_and_tail(config: EasyConfig) -> None:",
        "async with create_session(config) as session:",
        "stop_tailing = asyncio.Event()",
        "session.journal.follow(stop=stop_tailing)",
        "stop_tailing.set()",
        "await tail_task",
        "uv run easycat inspect .easycat/journals/<session_id>.sqlite",
    ):
        assert term in observability_section
    assert "asyncio.create_task(tail(session))" not in observability_section


def test_provider_guide_teaches_easyconfig_instance_injection() -> None:
    section = (REPO_ROOT / "docs" / "extending" / "README.md").read_text(encoding="utf-8")

    for term in (
        "from easycat import EasyConfig, run",
        "EasyConfig.mic(",
        "stt=MySTT()",
        "tts=MyTTS()",
        "agent=my_agent",
        "An instance",
        "`vad=`",
        "`noise_reduction=`",
        "`echo_cancellation=`",
        "provider *instances*",
    ):
        assert term in section

    assert "SessionConfig" not in section
    assert "Session(" not in section


def test_readme_current_capabilities_track_public_provider_and_bridge_surfaces() -> None:
    from easycat.integrations import agents as agent_integrations
    from easycat.noise_reduction import NoiseReducerBackend
    from easycat.stt.factory import available_stt_providers
    from easycat.transports import __all__ as transport_exports
    from easycat.tts.factory import available_tts_providers
    from easycat.vad import VADBackend

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    capabilities = readme.split("## Current capabilities", 1)[1].split("## ", 1)[0]

    for provider in available_stt_providers():
        assert _PROVIDER_DISPLAY_NAMES[provider] in capabilities
    for provider in available_tts_providers():
        assert _PROVIDER_DISPLAY_NAMES[provider] in capabilities

    vad_backends = set(get_args(VADBackend)) - {"auto"}
    missing_vad_display_map = sorted(vad_backends - _VAD_DISPLAY_NAMES.keys())
    assert not missing_vad_display_map, "VAD display map missing: " + ", ".join(
        missing_vad_display_map
    )
    for backend in vad_backends:
        assert _VAD_DISPLAY_NAMES[backend] in capabilities

    noise_backends = set(get_args(NoiseReducerBackend)) - {"auto"}
    missing_noise_display_map = sorted(noise_backends - _NOISE_REDUCTION_DISPLAY_NAMES.keys())
    assert not missing_noise_display_map, "Noise reduction display map missing: " + ", ".join(
        missing_noise_display_map
    )
    for backend in noise_backends:
        assert _NOISE_REDUCTION_DISPLAY_NAMES[backend] in capabilities
    assert "passthrough fallback" in capabilities

    for transport in ("Local", "WebSocket", "WebRTC", "WebTransport", "Twilio"):
        if any(name.startswith(transport) for name in transport_exports):
            assert transport in capabilities

    bridge_names = {
        name
        for name in agent_integrations.__all__
        if name.endswith("Bridge") and name != "ExternalAgentBridge"
    }
    missing_bridges = sorted(name for name in bridge_names if name not in readme)

    assert not missing_bridges, "README.md missing public agent bridge names: " + ", ".join(
        missing_bridges
    )
