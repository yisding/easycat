"""Documented happy-path smoke test.

Walks the canonical quickstart flow advertised in README:

    EasyConfig(...) -> create_session -> start -> one turn -> stop -> export bundle

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
    r"### Quickstart \(EasyConfig\).*?```python\n(?P<code>.*?)\n```",
    re.DOTALL,
)


class EchoAgent:
    async def run(self, text: str) -> str:
        return f"You said: {text}"


def _uses_run_easyconfig_mic(source: str) -> bool:
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "run":
            continue
        if not node.args:
            continue
        first_arg = node.args[0]
        if (
            isinstance(first_arg, ast.Call)
            and isinstance(first_arg.func, ast.Attribute)
            and first_arg.func.attr == "mic"
            and isinstance(first_arg.func.value, ast.Name)
            and first_arg.func.value.id == "EasyConfig"
        ):
            return True

    return False


def _readme_quickstart_code() -> str:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    match = _QUICKSTART_BLOCK_RE.search(readme)
    assert match is not None, "README.md Quickstart (EasyConfig) code block not found"
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
    missing = sorted(
        name for name, source in sources.items() if not _uses_run_easyconfig_mic(source)
    )

    assert not missing, "Canonical quickstart shape drifted in: " + ", ".join(missing)

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "the same one shown below" not in readme
    assert "the same one shown above" in readme


def test_readme_choose_your_path_routes_primary_onboarding_surfaces() -> None:
    section = _readme_section("## Choose Your Path", "## Learn the pipeline from scratch")
    normalized = re.sub(r"\s+", " ", section)
    expected_rows = {
        "Run a local mic/speaker voice bot": ("[Install](#install)", "uv run easycat doctor"),
        "No mic or API key yet": (
            "[Journal demo](examples/journal_demo.py)",
            "[hardware-free teaching spine](docs/teaching/#hardware-free-checkpoint-spine)",
            "uv run easycat console",
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
    """The 3-line quickstart sits above the fold, followed by a 4-command install."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert readme.count("## Install") == 1
    quickstart_index = readme.index("### Quickstart (EasyConfig)")
    install_index = readme.index("## Install")
    chooser_index = readme.index("## Choose Your Path")
    cli_index = readme.index("## CLI")

    assert quickstart_index < install_index < chooser_index < cli_index
    assert "uv add 'easycat[quickstart]'" in readme
    assert "uv sync --extra quickstart --group dev" in readme
    assert "uv run easycat doctor" in readme
    assert "uv run python examples/openai_agents_voice.py" in readme
    assert "uv run easycat doctor --env-file .env" in readme
    assert "uv run --env-file .env python examples/openai_agents_voice.py" in readme

    repo_block = readme.split("For this repository, four commands", 1)[1]
    repo_commands = repo_block.split("```bash", 1)[1].split("```", 1)[0].strip().splitlines()
    assert repo_commands == [
        "uv sync --extra quickstart --group dev",
        "echo 'OPENAI_API_KEY=your-api-key' > .env",
        "uv run easycat doctor --env-file .env",
        "uv run --env-file .env python examples/openai_agents_voice.py",
    ]


def test_readme_webrtc_browser_fast_path_runs_doctor_preflight() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("### Quickstart: WebRTC in browser (fast path)", 1)[1].split(
        "## Repo layout",
        1,
    )[0]

    expected_order = (
        "uv sync --extra webrtc --extra openai --extra openai-agents --group dev",
        'export OPENAI_API_KEY="your-api-key"',
        "uv run easycat doctor",
        "uv run python examples/webrtc_server.py",
        "http://localhost:8080",
    )
    cursor = -1
    for term in expected_order:
        index = section.index(term)
        assert index > cursor
        cursor = index

    assert "uv run easycat doctor --env-file .env" in section
    assert "uv run --env-file .env python examples/webrtc_server.py" in section


def test_readme_pydantic_ai_v2_requirement_matches_pyproject() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = pyproject["project"]["optional-dependencies"]["pydantic-ai-v2"]
    specs = [dep for dep in deps if dep.startswith("pydantic-ai>=")]
    assert len(specs) == 1

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "`pydantic-ai-v2` extra installs" in readme
    assert specs[0] in readme


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
    section = readme.split("## Bring your own agent", 1)[1].split(
        "### Advanced: own the lifecycle", 1
    )[0]
    normalized_section = re.sub(r"\s+", " ", section)

    missing_display_names = sorted(
        display_name
        for bridge_name, display_name in _BRIDGE_DISPLAY_NAMES.items()
        if bridge_name in auto_detected_bridges and display_name not in normalized_section
    )

    assert not missing_display_names, (
        "README Bring your own agent section missing auto-adapt labels: "
        + ", ".join(missing_display_names)
    )
    assert "OpenAI Agents SDK and PydanticAI objects" not in normalized_section


# Retired manual-bridge API shapes that must stay out of the idiomatic
# EasyConfig auto-adapt README sections.
_STALE_BRIDGE_API_SHAPES = (
    'openai_api_key="your-api-key"',
    "from easycat import Session, SessionConfig",
    "OpenAIAgentsBridge",
    "PydanticAIBridge",
    "Session(SessionConfig(",
    "bridge =",
)


@pytest.mark.parametrize(
    (
        "heading",
        "end_marker",
        "sub_split_marker",
        "must_contain",
        "must_not_contain",
        "full_section_must_contain",
    ),
    [
        (
            "### OpenAI Agents SDK (idiomatic)",
            "### ",
            None,
            (
                "from easycat import EasyConfig, create_session",
                "session = create_session(config)",
                "agent=agent",
            ),
            _STALE_BRIDGE_API_SHAPES,
            (),
        ),
        (
            "### PydanticAI (idiomatic)",
            "### ",
            None,
            (
                "from easycat import EasyConfig, create_session",
                "session = create_session(config)",
                "agent=pydantic_agent",
            ),
            _STALE_BRIDGE_API_SHAPES,
            (),
        ),
        (
            "### LangChain and LangGraph",
            "### ",
            None,
            (
                "EasyConfig(agent=...)",
                "LangChainBridge",
                "uv sync --extra langchain",
                "LangGraphBridge",
                "checkpointer",
                "uv sync --extra langgraph",
                "examples/langchain_voice.py",
                "examples/langgraph_voice.py",
            ),
            (),
            (),
        ),
        (
            "### LlamaAgents / LlamaIndex Workflows",
            "## Examples",
            "To call a workflow mounted",
            (
                "from easycat import EasyConfig, create_session",
                "agent=GreetingWorkflow()",
                "session = create_session(",
            ),
            (
                'openai_api_key="your-api-key"',
                "from easycat.integrations.agents import LlamaAgentsBridge",
                'input_key="message"',
                "LlamaAgentsBridge(workflow=GreetingWorkflow()",
            ),
            ("LlamaAgentsBridge(base_url=",),
        ),
    ],
)
def test_readme_agent_snippet_sections_use_easyconfig_auto_adapt_surface(
    heading: str,
    end_marker: str,
    sub_split_marker: str | None,
    must_contain: tuple[str, ...],
    must_not_contain: tuple[str, ...],
    full_section_must_contain: tuple[str, ...],
) -> None:
    """Guard README agent-framework snippets against rotting to removed API shapes.

    Covers the OpenAI Agents SDK, PydanticAI, LangChain/LangGraph, and LlamaAgents
    sections, each checking the current EasyConfig auto-adapt surface is taught and
    stale shapes (e.g. ``Session(SessionConfig(...))``) are not.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split(heading, 1)[1].split(end_marker, 1)[0]
    snippet = section.split(sub_split_marker, 1)[0] if sub_split_marker else section

    for term in must_contain:
        assert term in snippet
    for stale_term in must_not_contain:
        assert stale_term not in snippet
    for term in full_section_must_contain:
        assert term in section


def test_readme_python_snippets_do_not_embed_placeholder_api_keys() -> None:
    """README code should rely on the documented OPENAI_API_KEY setup."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert 'export OPENAI_API_KEY="your-api-key"' in readme
    assert 'openai_api_key="your-api-key"' not in readme
    assert 'openai_api_key="…"' not in readme


def test_readme_cli_section_lists_registered_top_level_commands() -> None:
    from easycat.cli import _app
    from easycat.cli.debug.bundles import bundles_app

    _app._register_commands()
    command_names = {command.name for command in _app.app.registered_commands}
    command_names.update(group.name for group in _app.app.registered_groups)
    command_names.discard(None)

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    cli_section = readme.split("## CLI", 1)[1].split("## ", 1)[0]
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
        "easycat docs             # show docs for learning, maintenance, validation, operations",
        "easycat docs --audience learners # filter docs by reader audience or broad role",
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

    missing = sorted(
        command_name
        for command_name in command_names
        if f"easycat {command_name}" not in cli_section
    )

    assert not missing, "README.md CLI section missing commands: " + ", ".join(missing)

    missing_bundle_commands = sorted(
        command.name
        for command in bundles_app.registered_commands
        if command.name is not None and f"easycat bundles {command.name}" not in cli_section
    )

    assert not missing_bundle_commands, "README.md CLI section missing bundles commands: " + (
        ", ".join(missing_bundle_commands)
    )


def test_readme_cli_section_does_not_advertise_stale_bundle_commands() -> None:
    from easycat.cli.debug.bundles import bundles_app

    command_names = {command.name for command in bundles_app.registered_commands}
    command_names.discard(None)

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    cli_section = readme.split("## CLI", 1)[1].split("## ", 1)[0]
    advertised = set(
        re.findall(r"(?m)^easycat bundles (?P<command>[a-z][a-z0-9-]*)(?:\s|$)", cli_section)
    )
    stale = sorted(advertised - command_names)

    assert not stale, "README.md CLI section advertises stale bundles commands: " + ", ".join(
        stale
    )


def test_readme_cli_section_does_not_advertise_stale_top_level_commands() -> None:
    from easycat.cli import _app

    _app._register_commands()
    command_names = {command.name for command in _app.app.registered_commands}
    command_names.update(group.name for group in _app.app.registered_groups)
    command_names.discard(None)

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    cli_section = readme.split("## CLI", 1)[1].split("## ", 1)[0]
    advertised = set(re.findall(r"(?m)^easycat (?P<command>[a-z][a-z0-9-]*)(?:\s|$)", cli_section))
    stale = sorted(advertised - command_names)

    assert not stale, "README.md CLI section advertises stale commands: " + ", ".join(stale)


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


def test_readme_observability_section_teaches_stoppable_journal_tail() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    observability_section = readme.split("## Inspecting conversation flow", 1)[1].split(
        "## ",
        1,
    )[0]

    for term in (
        "async def main() -> None:",
        "async with create_session(config) as session:",
        "stop_tailing = asyncio.Event()",
        "session.journal.follow(stop=stop_tailing)",
        "stop_tailing.set()",
        "await tail_task",
        "asyncio.run(main())",
        "uv run easycat inspect .easycat/journals/<session_id>.sqlite",
    ):
        assert term in observability_section
    assert "asyncio.create_task(tail(session))" not in observability_section


def test_readme_local_speech_pipeline_uses_easyconfig_provider_instances() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("### Local/open-source speech pipeline", 1)[1].split(
        "## Inspecting conversation flow",
        1,
    )[0]

    for term in (
        "from easycat import EasyConfig, create_session",
        "EasyConfig.mic(",
        "stt=LocalSTTProvider(...)",
        "tts=LocalTTSProvider(...)",
        "agent=LocalAgent(...)",
        "Provider instances are",
        "AudioProcessingConfig",
        "`audio_processing=AudioProcessingConfig(",
        "`vad=`",
        "`noise_reduction=`",
        "`echo_cancellation=`",
        "custom audio-processing stages",
    ):
        assert term in section

    assert "SessionConfig" not in section
    assert "Session(" not in section


def test_readme_current_capabilities_track_public_provider_and_bridge_surfaces() -> None:
    from easycat.integrations import agents as agent_integrations
    from easycat.noise_reduction import NoiseReducerBackend
    from easycat.stt.factory import available_providers as available_stt_providers
    from easycat.transports import __all__ as transport_exports
    from easycat.tts.factory import available_providers as available_tts_providers
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
