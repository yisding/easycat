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
    assert "the same one shown above" not in readme
    assert "the same one shown below" in readme


def test_readme_install_guidance_precedes_first_runnable_quickstart() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert readme.count("## Install") == 1
    install_index = readme.index("## Install")
    cli_index = readme.index("## CLI")
    quickstart_index = readme.index("### Quickstart (EasyConfig)")

    assert install_index < cli_index < quickstart_index
    assert "uv add 'easycat[quickstart]'" in readme
    assert "uv sync --extra quickstart --group dev" in readme
    assert "uv run easycat doctor" in readme
    assert "uv run python examples/openai_agents_voice.py" in readme

    repo_block = readme.split("For this repository:", 1)[1]
    repo_commands = repo_block.split("```bash", 1)[1].split("```", 1)[0].strip().splitlines()
    assert repo_commands == [
        "uv sync --extra quickstart --group dev",
        'export OPENAI_API_KEY="your-api-key"',
        "uv run easycat doctor",
        "uv run python examples/openai_agents_voice.py",
    ]


def test_readme_telephony_opt_out_uses_easyconfig_surface() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("### Opt-out auto-detection", 1)[1].split("### ", 1)[0]

    assert "EasyConfig.dnc_list" in section
    assert "EasyConfig.opt_out_detection=False" in section
    assert "SessionConfig.opt_out_detection=False" not in section


def test_readme_pydantic_ai_v2_beta_pin_matches_pyproject() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = pyproject["project"]["optional-dependencies"]["pydantic-ai-v2-beta"]
    pins = [dep for dep in deps if dep.startswith("pydantic-ai==")]
    assert len(pins) == 1

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "`pydantic-ai-v2-beta` extra pins" in readme
    assert pins[0] in readme


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
    section = readme.split("## Bring your own agent", 1)[1].split("### Quickstart", 1)[0]
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


def test_readme_agent_framework_snippets_use_easyconfig_auto_adapt_surface() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    snippets = {
        "### OpenAI Agents SDK (idiomatic)": ("agent=agent",),
        "### PydanticAI (idiomatic)": ("agent=pydantic_agent",),
    }

    for heading, expected_terms in snippets.items():
        section = readme.split(heading, 1)[1].split("### ", 1)[0]

        for term in (
            "from easycat import EasyConfig, create_session",
            'openai_api_key="your-api-key"',
            "session = create_session(config)",
            *expected_terms,
        ):
            assert term in section

        for stale_term in (
            "from easycat import Session, SessionConfig",
            "OpenAIAgentsBridge",
            "PydanticAIBridge",
            "Session(SessionConfig(",
            "bridge =",
        ):
            assert stale_term not in section


def test_readme_langchain_langgraph_section_teaches_auto_adapt_requirements() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("### LangChain and LangGraph", 1)[1].split("### ", 1)[0]

    for term in (
        "EasyConfig(agent=...)",
        "LangChainBridge",
        "uv sync --extra langchain",
        "LangGraphBridge",
        "checkpointer",
        "uv sync --extra langgraph",
        "examples/langchain_voice.py",
        "examples/langgraph_voice.py",
    ):
        assert term in section


def test_readme_llama_agents_local_snippet_uses_easyconfig_auto_adapt() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("### LlamaAgents / LlamaIndex Workflows", 1)[1].split(
        "## Examples",
        1,
    )[0]
    local_snippet = section.split("To call a workflow mounted", 1)[0]

    for term in (
        "from easycat import EasyConfig, create_session",
        'openai_api_key="your-api-key"',
        "agent=GreetingWorkflow()",
        "session = create_session(",
    ):
        assert term in local_snippet

    for stale_term in (
        "from easycat.integrations.agents import LlamaAgentsBridge",
        'input_key="message"',
        "LlamaAgentsBridge(workflow=GreetingWorkflow()",
    ):
        assert stale_term not in local_snippet

    assert "LlamaAgentsBridge(base_url=" in section


def test_readme_cli_section_lists_registered_top_level_commands() -> None:
    from easycat.cli import _app
    from easycat.cli.debug.bundles import bundles_app

    _app._register_commands()
    command_names = {command.name for command in _app.app.registered_commands}
    command_names.update(group.name for group in _app.app.registered_groups)
    command_names.discard(None)

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    cli_section = readme.split("## CLI", 1)[1].split("## ", 1)[0]

    assert "installed CLI form" in cli_section
    assert "uv run easycat doctor" in cli_section
    assert "easycat doctor --env-file .env" in cli_section
    expected_cli_lines = (
        "easycat init --list-templates # compare scaffold templates",
        "easycat bundles list      # list captured debug bundles and crash dumps",
        "easycat bundles show PATH # summarise a debug bundle or SQLite journal",
        "easycat bundles export PATH # write a redacted coding-agent context pack",
        "easycat inspect PATH      # summarise a debug bundle or SQLite journal",
        "easycat replay PATH       # replay a debug bundle or SQLite journal",
    )
    for line in expected_cli_lines:
        assert line in cli_section

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


def test_readme_validation_workflow_lists_registered_validate_commands() -> None:
    from easycat.cli.validate import validate_app

    command_names = {command.name for command in validate_app.registered_commands}
    command_names.discard(None)

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    validation_section = readme.split("## Validation Workflow", 1)[1].split("## ", 1)[0]

    missing = sorted(
        command_name
        for command_name in command_names
        if f"easycat validate {command_name}" not in validation_section
    )

    assert not missing, "README.md validation section missing commands: " + ", ".join(missing)
    assert "easycat validate latency --smoke" in validation_section

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
