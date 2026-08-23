from __future__ import annotations

import pytest

from tests.docs._docs_index_helpers import (
    _DOCS_LINKS,
    AGENT_GUIDE_MACHINE_COMMANDS,
    CODE_SPAN_RE,
    DOCS_MAP_COMMANDS,
    EXAMPLE_README_ROW_RE,
    ONBOARDING_GUARD_COMMANDS,
    RAW_ONBOARDING_GUARD_COMMANDS,
    REPO_ROOT,
    _docs_entries,
    _documented_command_lines,
    _documented_commands,
    _reference_section_field_names,
    _route_target_text,
    re,
)


def test_quickstart_docs_route_matches_install_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    route_commands = set(entries["README.md#install"].get("commands", ()))
    install_section = (
        _route_target_text("README.md#install")
        .split("## Install", 1)[1]
        .split("## Choose Your Path", 1)[0]
    )
    install_commands = [
        match.group(1)
        for match in CODE_SPAN_RE.finditer(install_section)
        if match.group(1).startswith(("uv sync ", "uv run "))
    ]
    missing = [command for command in install_commands if command not in route_commands]

    assert not missing, "Quickstart docs route missing install command hints: " + ", ".join(
        missing
    )


def test_coding_agents_docs_route_matches_guide_command_hints() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    command_section = agents.split("## Build, Test, and Development Commands", 1)[1].split(
        "## Coding Style",
        1,
    )[0]
    route_commands = entries["AGENTS.md"].get("commands", ())

    for command in (
        DOCS_MAP_COMMANDS
        + (
            "uv run easycat docs --audience coding-agents",
            "uv run easycat docs --audience coding-agents --json",
        )
        + AGENT_GUIDE_MACHINE_COMMANDS
        + ONBOARDING_GUARD_COMMANDS
    ):
        assert command in command_section
        assert command in route_commands

    for command in RAW_ONBOARDING_GUARD_COMMANDS:
        assert command in command_section
        assert command not in route_commands


def test_maintainer_guide_docs_route_matches_guide_command_hints() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    guide = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    command_section = guide.split("## Commands", 1)[1].split("## Architecture", 1)[0]
    route_commands = entries["CLAUDE.md"].get("commands", ())

    for command in (
        DOCS_MAP_COMMANDS
        + (
            "uv run easycat docs --audience maintainers",
            "uv run easycat docs --audience maintainers --json",
        )
        + AGENT_GUIDE_MACHINE_COMMANDS
        + ("uv run pytest tests/install/test_install_guidance.py",)
        + ONBOARDING_GUARD_COMMANDS
    ):
        assert command in command_section
        assert command in route_commands

    for command in RAW_ONBOARDING_GUARD_COMMANDS:
        assert command in command_section
        assert command not in route_commands


def test_architecture_explanation_carries_claude_guide_prose() -> None:
    """docs/architecture.md owns the architecture explanation; CLAUDE.md links to it."""
    page = re.sub(
        r"\s+", " ", (REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    )
    guide = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    architecture_section = guide.split("## Architecture", 1)[1].split("## Key Patterns", 1)[0]

    assert "[docs/architecture.md](docs/architecture.md)" in architecture_section
    assert "ExternalAgentBridge" in page
    assert "SessionWiringContext" not in guide


def test_events_reference_tracks_public_event_types() -> None:
    """docs/reference/events.md must list exactly the exported concrete events."""
    import easycat
    from easycat.events import Event

    text = (REPO_ROOT / "docs" / "reference" / "events.md").read_text(encoding="utf-8")
    catalog = text.split("## Event Catalog", 1)[1].split("\n## ", 1)[0]
    documented = set(re.findall(r"^- `([A-Za-z]+)`", catalog, flags=re.MULTILINE))
    exported = {
        name
        for name in easycat.__all__
        if isinstance(getattr(easycat, name), type)
        and issubclass(getattr(easycat, name), Event)
        and getattr(easycat, name) is not Event
    }

    missing = sorted(exported - documented)
    extra = sorted(documented - exported)
    assert not missing, "docs/reference/events.md missing events: " + ", ".join(missing)
    assert not extra, "docs/reference/events.md lists non-exported events: " + ", ".join(extra)

    # The page must teach the provider-scoped vs EasyCat-level distinction.
    assert "provider-scoped" in text.lower()
    assert "`STTEvent`" in text
    assert "`TTSEvent`" in text
    assert "easycat.events" in text
    normalized_text = " ".join(text.split())
    assert "confirmation/take timestamp" in normalized_text
    assert "not always the start of model work" in normalized_text


def test_easyconfig_reference_tracks_config_fields() -> None:
    """The handwritten EasyConfig reference must match the live dataclasses."""
    import dataclasses

    from easycat import EasyConfig

    text = (REPO_ROOT / "docs" / "reference" / "easyconfig.md").read_text(encoding="utf-8")
    expected = {"## Construction Fields": {f.name for f in dataclasses.fields(EasyConfig)}}

    problems: list[str] = []
    for heading, names in expected.items():
        documented = _reference_section_field_names(text, heading)
        for name in sorted(names - documented):
            problems.append(f"{heading}: missing `{name}`")
        for name in sorted(documented - names):
            problems.append(f"{heading}: documents unknown `{name}`")

    assert not problems, "docs/reference/easyconfig.md is out of sync:\n" + "\n".join(problems)

    assert "test_easyconfig_reference_tracks_config_fields" in text


def test_session_lifecycle_reference_matches_lifecycle_contract() -> None:
    text = (REPO_ROOT / "docs" / "reference" / "session-lifecycle.md").read_text(encoding="utf-8")
    guide = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    lifecycle_section = guide.split("## Session Lifecycle", 1)[1].split("## Style", 1)[0]

    assert "docs/reference/session-lifecycle.md" in lifecycle_section
    for marker in (
        "`stop(force=True)`",
        "There are no separate public close/destroy phases",
        "`session.journal.read()`",
        "`session.export_debug_bundle(path)`",
        "`async with session:`",
        "asyncio.to_thread(create_session, config)",
        "record_to",
        "`await session.cancel_turn()`",
        "`await session.reset_state()`",
        "`await session.send_text(text)`",
    ):
        assert marker in text, f"docs/reference/session-lifecycle.md missing {marker!r}"
    for stale in (
        "session.shutdown()",
        "Session.shutdown()",
        "session.close()",
        "Session.close()",
        "session.destroy()",
        "Session.destroy()",
        "`session.cancel_turn()`",
        "`session.reset_state()`",
        "`session.send_text(text)`",
    ):
        assert stale not in text


def test_explain_concept_topics_print_docs_routes() -> None:
    """`easycat explain events|turn-taking|journal` must point at live docs routes."""
    from easycat.cli.diagnose._codes import META_ENTRIES

    route_paths = {entry["path"] for entry in _DOCS_LINKS}
    expected_routes = {
        "events": "docs/reference/events.md",
        "turn-taking": "docs/architecture.md",
        "journal": "docs/reference/session-lifecycle.md",
    }

    for slug, route in expected_routes.items():
        assert slug in META_ENTRIES, f"easycat explain is missing the {slug!r} concept topic"
        assert route in META_ENTRIES[slug].body
        assert route in route_paths


def test_teaching_ladder_docs_route_matches_learner_start_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    teaching_readme = (REPO_ROOT / "docs" / "teaching" / "README.md").read_text(encoding="utf-8")
    route_commands = entries["docs/teaching/"].get("commands", ())
    progress = (REPO_ROOT / "docs" / "teaching" / "PROGRESS.md").read_text(encoding="utf-8")
    progress_commands = entries["docs/teaching/PROGRESS.md"].get("commands", ())
    first_lesson = (REPO_ROOT / "docs" / "teaching" / "00-hello-audio" / "README.md").read_text(
        encoding="utf-8"
    )
    first_lesson_commands = entries["docs/teaching/00-hello-audio/"].get("commands", ())

    for command in (
        "uv sync --extra local --group dev",
        "uv sync --extra quickstart --group dev",
        "uv run easycat doctor",
        "uv run easycat doctor --env-file .env",
        "uv run easycat docs --audience learners",
        "uv run easycat docs --audience learners --json",
        "uv run python docs/teaching/00-hello-audio/main.py",
        "uv run python docs/teaching/offline_spine.py",
        "uv run python docs/teaching/offline_spine.py --json",
        "uv run python docs/teaching/offline_spine.py --run --jobs 4",
        "uv run python docs/teaching/offline_spine.py --run --jobs 4 --json",
        "uv run easycat validate quick",
        "uv run easycat validate quick --json",
        "uv run easycat validate report .easycat/validation/latest.json",
        "uv run easycat validate report .easycat/validation/latest.json --json",
    ):
        assert command in teaching_readme
        assert command in route_commands

    for command in (
        "uv run python docs/teaching/00-hello-audio/format_boundaries.py",
        "uv run python docs/teaching/offline_spine.py --run --jobs 4 --show-evidence",
    ):
        assert command in progress
        assert command in progress_commands

    for command in (
        "uv sync --extra local --group dev",
        "uv run python docs/teaching/00-hello-audio/main.py",
    ):
        assert command in first_lesson
        assert command in first_lesson_commands

    assert "uv run pytest tests/teaching/test_ladder_index.py" not in route_commands
    assert "easycat validate quick" not in route_commands
    assert "easycat validate report .easycat/validation/latest.json" not in route_commands
    assert "uv run easycat validate quick" not in first_lesson_commands
    assert "easycat validate quick" not in first_lesson_commands


def test_feature_ladder_docs_route_matches_first_lesson_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    ladder_readme = (REPO_ROOT / "docs" / "using-easycat" / "README.md").read_text(
        encoding="utf-8"
    )
    lesson_readme = (
        REPO_ROOT / "docs" / "using-easycat" / "00-first-voice-app" / "README.md"
    ).read_text(encoding="utf-8")
    ladder_commands = entries["docs/using-easycat/"].get("commands", ())
    lesson_commands = entries["docs/using-easycat/00-first-voice-app/"].get("commands", ())

    shared_commands = (
        "uv sync --extra quickstart --group dev",
        "uv run easycat doctor",
        "uv run easycat doctor --env-file .env",
        "uv run python docs/using-easycat/00-first-voice-app/main.py",
        "uv run --env-file .env python docs/using-easycat/00-first-voice-app/main.py",
    )
    for command in shared_commands:
        assert command in ladder_readme
        assert command in lesson_readme
        assert command in ladder_commands
        assert command in lesson_commands

    for command in (
        "uv run easycat docs --audience learners",
        "uv run easycat docs --audience learners --json",
    ):
        assert command in ladder_readme
        assert command in ladder_commands


@pytest.mark.parametrize(
    ("chapter_dir", "docs_path_key", "expected_commands"),
    [
        (
            "01-runtime-modes",
            "docs/using-easycat/01-runtime-modes/",
            (
                "uv sync --extra quickstart --extra webrtc --extra telephony --group dev",
                "uv run easycat doctor",
                "uv run easycat doctor --env-file .env",
                "uv run python docs/using-easycat/01-runtime-modes/main.py local",
                "uv run python docs/using-easycat/01-runtime-modes/main.py browser",
                "uv run python docs/using-easycat/01-runtime-modes/main.py websocket",
                "uv run python docs/using-easycat/01-runtime-modes/main.py twilio",
                (
                    "uv run --env-file .env python "
                    "docs/using-easycat/01-runtime-modes/main.py browser"
                ),
            ),
        ),
        (
            "02-providers-and-voices",
            "docs/using-easycat/02-providers-and-voices/",
            (
                "uv sync --extra quickstart --extra deepgram --extra elevenlabs --group dev",
                "uv run easycat doctor",
                "uv run easycat doctor --provider deepgram",
                "uv run easycat doctor --provider elevenlabs",
                "uv run python docs/using-easycat/02-providers-and-voices/main.py list",
                (
                    "uv run python docs/using-easycat/02-providers-and-voices/main.py "
                    "openai --voice alloy"
                ),
                (
                    "uv run python docs/using-easycat/02-providers-and-voices/main.py "
                    "deepgram-stt --voice nova"
                ),
                (
                    "uv run python docs/using-easycat/02-providers-and-voices/main.py "
                    "elevenlabs-voice"
                ),
                (
                    "uv run --env-file .env python "
                    "docs/using-easycat/02-providers-and-voices/main.py deepgram-stt"
                ),
            ),
        ),
        (
            "03-conversation-controls",
            "docs/using-easycat/03-conversation-controls/",
            (
                "uv sync --extra quickstart --group dev",
                "uv run easycat doctor",
                "uv run easycat doctor --env-file .env",
                "uv run python docs/using-easycat/03-conversation-controls/main.py balanced",
                "uv run python docs/using-easycat/03-conversation-controls/main.py vad-only",
                "uv run python docs/using-easycat/03-conversation-controls/main.py fast",
                "uv run python docs/using-easycat/03-conversation-controls/main.py clean",
                "uv run python docs/using-easycat/03-conversation-controls/main.py raw",
                "uv run python docs/using-easycat/03-conversation-controls/push_to_talk.py",
                (
                    "uv run --env-file .env python "
                    "docs/using-easycat/03-conversation-controls/main.py fast"
                ),
            ),
        ),
        (
            "04-tools-actions",
            "docs/using-easycat/04-tools-actions/",
            (
                "uv sync --extra quickstart --group dev",
                "uv run easycat doctor",
                "uv run easycat doctor --env-file .env",
                "uv run python docs/using-easycat/04-tools-actions/main.py preview",
                "uv run python docs/using-easycat/04-tools-actions/main.py run",
                "uv run --env-file .env python docs/using-easycat/04-tools-actions/main.py run",
            ),
        ),
        (
            "05-agent-bridges",
            "docs/using-easycat/05-agent-bridges/",
            (
                "uv sync --extra quickstart --group dev",
                "uv run easycat doctor",
                "uv run easycat doctor --env-file .env",
                "uv run python docs/using-easycat/05-agent-bridges/main.py matrix",
                "uv run python docs/using-easycat/05-agent-bridges/main.py run",
                "uv run --env-file .env python docs/using-easycat/05-agent-bridges/main.py run",
            ),
        ),
        (
            "06-session-control",
            "docs/using-easycat/06-session-control/",
            (
                "uv sync --extra quickstart --group dev",
                "uv run easycat doctor",
                "uv run easycat doctor --env-file .env",
                "uv run python docs/using-easycat/06-session-control/main.py text",
                "uv run python docs/using-easycat/06-session-control/main.py voice",
                (
                    "uv run --env-file .env python "
                    "docs/using-easycat/06-session-control/main.py voice"
                ),
            ),
        ),
        (
            "07-observability",
            "docs/using-easycat/07-observability/",
            (
                "uv sync --group dev",
                "uv sync --extra debugger --group dev",
                (
                    "uv run python docs/using-easycat/07-observability/main.py "
                    "pair .easycat/tutorial/ch07"
                ),
                "uv run easycat bundles show .easycat/tutorial/ch07/baseline.bundle --json",
                (
                    "uv run easycat replay .easycat/tutorial/ch07/baseline.bundle "
                    "--fidelity artifact --tool-policy deny --json"
                ),
                (
                    "uv run easycat diff .easycat/tutorial/ch07/baseline.bundle "
                    ".easycat/tutorial/ch07/candidate.bundle --json"
                ),
            ),
        ),
        (
            "08-testing-evals",
            "docs/using-easycat/08-testing-evals/",
            (
                "uv sync --group dev",
                "uv run python docs/using-easycat/08-testing-evals/main.py",
                "uv run easycat latency .easycat/tutorial/ch07/baseline.bundle --json",
                "uv run easycat doctor --json",
                "uv run easycat validate latency --smoke --json",
            ),
        ),
        (
            "09-multi-caller",
            "docs/using-easycat/09-multi-caller/",
            (
                "uv sync --group dev",
                "uv run python docs/using-easycat/09-multi-caller/main.py",
            ),
        ),
        (
            "10-telephony",
            "docs/using-easycat/10-telephony/",
            (
                "uv sync --group dev",
                "uv run python docs/using-easycat/10-telephony/main.py",
                (
                    "uv sync "
                    "--extra openai --extra telephony --extra telephony-fastapi "
                    "--extra openai-agents --group dev"
                ),
                "uv run easycat doctor --env-file .env --json",
                (
                    "uv run --env-file .env uvicorn examples.twilio_app:create_app "
                    "--factory --host 0.0.0.0"
                ),
                (
                    "uv run --env-file .env uvicorn examples.telnyx_app:create_app "
                    "--factory --host 0.0.0.0"
                ),
            ),
        ),
        (
            "11-production-ops",
            "docs/using-easycat/11-production-ops/",
            (
                "uv sync --group dev",
                "uv run python docs/using-easycat/11-production-ops/main.py",
                (
                    "uv run python docs/using-easycat/11-production-ops/main.py "
                    "--data-dir .easycat/tutorial/ch11"
                ),
                (
                    "uv run easycat inspect .easycat/tutorial/ch11/journals/"
                    "chapter-11-ops-checkpoint.sqlite --json"
                ),
                "uv run easycat validate quick --json",
                "uv run easycat validate report .easycat/validation/latest.json --json",
                "uv run easycat validate release --json",
            ),
        ),
    ],
)
def test_feature_chapter_docs_route_matches_chapter_commands(
    chapter_dir: str, docs_path_key: str, expected_commands: tuple[str, ...]
) -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    readme = (REPO_ROOT / "docs" / "using-easycat" / chapter_dir / "README.md").read_text(
        encoding="utf-8"
    )
    route = entries[docs_path_key]

    assert route["audience"] == "learners"
    assert route["diataxis"] == "tutorial"
    for command in expected_commands:
        assert command in readme
        assert command in route["commands"]


def test_examples_docs_route_matches_examples_fast_path() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    examples_readme = (REPO_ROOT / "examples" / "README.md").read_text(encoding="utf-8")
    intro = examples_readme.split("For the fastest local mic/speaker path:", 1)[0]
    chooser = examples_readme.split("## Choose An Example", 1)[1].split("## Core Voice Loops", 1)[
        0
    ]
    fast_path = examples_readme.split("For the fastest local mic/speaker path:", 1)[1]
    route_commands = entries["examples/README.md"].get("commands", ())
    example_rows = {
        match.group("link"): match.group("run")
        for line in examples_readme.splitlines()
        if (match := EXAMPLE_README_ROW_RE.match(line)) is not None
    }

    for command in (
        "uv run easycat init --list-templates",
        "uv run easycat init my-agent",
        "uv run easycat init --list-templates --json",
    ):
        assert command in intro
        assert command in route_commands

    no_key_row = next(line for line in chooser.splitlines() if line.startswith("| No API keys |"))
    no_key_commands = [
        example_rows[link] for _, link in re.findall(r"\[([^]]+\.py)\]\(([^)]+\.py)\)", no_key_row)
    ]
    assert no_key_commands
    for command in no_key_commands:
        assert command in route_commands

    for command in (
        "uv run easycat doctor",
        "uv run easycat doctor --env-file .env",
        "uv run python examples/openai_agents_voice.py",
        "uv run --env-file .env python examples/openai_agents_voice.py",
        "uv run easycat validate quick",
        "uv run easycat validate quick --json",
        "uv run easycat validate report .easycat/validation/latest.json",
        "uv run easycat validate report .easycat/validation/latest.json --json",
    ):
        assert command in fast_path
        assert command in route_commands

    assert "easycat doctor" not in route_commands
    assert "easycat validate quick" not in route_commands
    assert "easycat validate report .easycat/validation/latest.json" not in route_commands


def test_public_api_docs_route_matches_contract_guard_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    contract = (REPO_ROOT / "docs" / "public-api.md").read_text(encoding="utf-8")
    route_commands = entries["docs/public-api.md"].get("commands", ())

    for command in (
        "uv run easycat docs",
        "uv run easycat docs --audience maintainers",
        "uv run easycat docs --json",
        "uv run easycat docs --audience maintainers --json",
        "uv run easycat explain json-schema",
        "uv run pytest tests/test_public_api.py",
        "just guard-docs",
    ):
        assert command in contract
        assert command in route_commands

    assert RAW_ONBOARDING_GUARD_COMMANDS[0] in contract
    assert RAW_ONBOARDING_GUARD_COMMANDS[0] not in route_commands
    assert "If `just` is not installed" in contract
    assert "[`CONTRIBUTING.md`](../CONTRIBUTING.md#the-development-loop)" in contract
    assert "easycat docs --json" not in route_commands


def test_provider_contract_docs_route_matches_contract_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    contract_readme = (REPO_ROOT / "tests" / "contracts" / "README.md").read_text(encoding="utf-8")
    route_commands = entries["tests/contracts/README.md"].get("commands", ())

    for command in (
        "uv run easycat docs --audience provider-maintainers",
        "uv run easycat docs --audience provider-maintainers --json",
        "uv run easycat validate contracts",
        "uv run easycat validate contracts --json",
        "uv run pytest tests/contracts",
        "uv run pytest tests/contracts/test_provider_session_matrix.py",
    ):
        assert command in contract_readme
        assert command in route_commands

    assert "easycat validate contracts" not in route_commands


def test_extending_docs_route_matches_provider_author_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    extending_readme = (REPO_ROOT / "docs" / "extending" / "README.md").read_text(encoding="utf-8")
    route = entries["docs/extending/"]
    route_commands = route.get("commands", ())

    assert route["audience"] == "provider maintainers"
    for command in (
        "uv run easycat docs --audience provider-maintainers",
        "uv run easycat docs --audience provider-maintainers --json",
        "uv run easycat init my-stt --template provider-stt",
        "uv run easycat init my-tts --template provider-tts",
        "uv run easycat init my-vad --template provider",
        "uv run python examples/custom_transport.py",
        "uv run pytest tests/test_public_api.py",
        "uv run pytest tests/contracts",
    ):
        assert command in extending_readme
        assert command in route_commands

    for page in ("stt.md", "tts.md", "vad.md", "transport.md", "agent-bridge.md"):
        assert (REPO_ROOT / "docs" / "extending" / page).is_file()
        assert f"({page})" in extending_readme


def test_deployment_docs_route_matches_docker_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    deployment = (REPO_ROOT / "docs" / "deployment" / "docker.md").read_text(encoding="utf-8")
    route_commands = entries["docs/deployment/docker.md"].get("commands", ())

    for command in (
        "uv run easycat docs --audience operators",
        "uv run easycat docs --audience operators --json",
        "docker compose -f docker/compose.yaml up --build",
        "python -m http.server 8080 --directory examples",
        "docker compose --env-file docker/.env -f docker/compose.yaml up --build",
        "docker compose -f docker/compose.yaml down",
    ):
        assert command in deployment
        assert command in route_commands

    assert "docker compose up --build" not in route_commands


def test_observability_docs_route_matches_journal_cli_entry_points() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    observability = (REPO_ROOT / "docs" / "observability.md").read_text(encoding="utf-8")
    cli_section = observability.split("- CLI entry points:", 1)[1].split(
        "### D — OpenTelemetry facade",
        1,
    )[0]
    route_commands = entries["docs/observability.md"].get("commands", ())

    assert "uv run easycat docs --audience operators" in observability
    assert "uv run easycat docs --audience operators" in route_commands
    assert "uv run easycat docs --audience operators --json" in observability
    assert "uv run easycat docs --audience operators --json" in route_commands
    for command in (
        "easycat bundles list",
        "easycat bundles list --json",
        "easycat bundles show PATH",
        "easycat bundles show PATH --json",
        "easycat inspect PATH",
        "easycat inspect PATH --json",
        "easycat replay PATH",
        "easycat replay PATH --json",
        "easycat latency PATH",
        "easycat latency PATH --json",
        "easycat bundles export PATH",
        "easycat bundles export PATH --output DIR --json",
        "uv sync --extra debugger --group dev",
    ):
        documented_command = command.replace("PATH", "<path>")
        assert f"`{documented_command}`" in cli_section
        assert command in route_commands

    for lifecycle_marker in (
        "Artifact lifecycle and storage budget",
        "50 journals",
        "2 GiB",
        "14 days",
        'journal_retention="archive"',
        'journal_retention="delete"',
        "session.export_debug_bundle",
    ):
        assert lifecycle_marker in observability

    # The latency/diff/journal/tail commands are registered on cli/_app.py but
    # were absent from every docs route, so they never reached `docs --json` or
    # the generated llms.txt. Pin them to the operators observability route and
    # require their prose to stay visible on the page.
    for command in (
        "easycat diff PATH PATH",
        "easycat journal grep PATH --query TEXT",
        "easycat journal follow PATH",
        "easycat journal promote PATH TURN_ID --out FILE",
        "easycat tail PATH",
    ):
        documented_command = command.replace("PATH", "<path>")
        assert f"`{documented_command}`" in cli_section
        assert command in route_commands

    assert "easycat bundles show <path>" not in route_commands
    assert "easycat bundles export <path>" not in route_commands
    assert "serve_bundle" in cli_section
    assert "serve_session" in cli_section
    assert "allow_remote=True" in cli_section


def test_latency_docs_route_matches_latency_cli_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    latency = (REPO_ROOT / "docs" / "latency.md").read_text(encoding="utf-8")
    route_commands = entries["docs/latency.md"].get("commands", ())

    # `easycat latency` is the per-bundle percentile command this page exists to
    # explain; keep it pinned to the route so it stays in `docs --json`.
    for command in (
        "easycat latency PATH",
        "easycat latency PATH --json",
    ):
        assert command in route_commands
        assert command in latency


def test_journal_durability_docs_route_matches_inspection_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    durability = (REPO_ROOT / "src" / "easycat" / "runtime" / "DURABILITY.md").read_text(
        encoding="utf-8"
    )
    route_commands = entries["src/easycat/runtime/DURABILITY.md"].get("commands", ())

    for command in (
        "uv run easycat docs --audience operators-and-maintainers",
        "uv run easycat docs --audience operators-and-maintainers --json",
        "uv run pytest tests/runtime/test_sqlite_journal.py",
        "uv run easycat inspect .easycat/journals/<session_id>.sqlite",
        "uv run easycat inspect .easycat/journals/<session_id>.sqlite --json",
        "uv run easycat inspect .easycat/crash-dumps/<session_id>.sqlite --json",
    ):
        assert command in durability
        assert command in route_commands

    assert "uv run easycat inspect PATH" not in route_commands


def test_validation_docs_route_matches_validation_workflow_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    validation_section = (REPO_ROOT / "docs" / "validation.md").read_text(encoding="utf-8")
    route_commands = entries["docs/validation.md"].get("commands", ())
    guard_commands = _documented_commands(validation_section, prefixes=("just guard-",))
    raw_guard_commands = _documented_command_lines(
        validation_section,
        prefixes=("uv run pytest ",),
    )
    validation_commands = _documented_commands(
        validation_section,
        prefixes=("uv run easycat validate ",),
    )

    assert guard_commands
    assert raw_guard_commands
    assert validation_commands
    assert "If `just` is not installed" in validation_section
    assert "[`CONTRIBUTING.md`](../CONTRIBUTING.md#the-development-loop)" in validation_section
    assert "`uv run pytest ...` command behind each guard" in validation_section
    for command in guard_commands:
        assert command in validation_section
        assert command in route_commands

    for command in RAW_ONBOARDING_GUARD_COMMANDS:
        assert command in validation_section
        assert command not in route_commands

    for command in validation_commands:
        assert command in validation_section
        assert command in route_commands

    assert "easycat validate quick" not in route_commands
    assert "easycat validate report .easycat/validation/latest.json" not in route_commands
    assert "easycat validate report .easycat/validation/latest.json --json" not in route_commands


def test_contributing_docs_route_matches_validation_lane_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    quick_start = contributing.split("## Quick start", 1)[1].split(
        "## The development loop",
        1,
    )[0]
    maintenance_section = contributing.split("## Maintaining docs and onboarding maps", 1)[
        1
    ].split("## Parallel runs and xdist safety", 1)[0]
    validation_section = contributing.split(
        "## Validation slices and the `easycat validate` CLI",
        1,
    )[1].split("## ", 1)[0]
    route_commands = entries["CONTRIBUTING.md"].get("commands", ())
    guard_commands = _documented_commands(maintenance_section, prefixes=("just guard-",))
    validation_commands = _documented_commands(
        validation_section,
        prefixes=("uv run easycat validate ",),
    )

    assert "uv run easycat docs --audience contributors" in quick_start
    assert "uv run easycat docs --audience contributors" in route_commands
    assert "uv run easycat docs --audience contributors --json" in quick_start
    assert "uv run easycat docs --audience contributors --json" in route_commands

    assert guard_commands
    assert validation_commands
    assert "If `just` is not installed" in maintenance_section
    assert "[the development loop](#the-development-loop)" in maintenance_section
    assert "`uv run pytest ...` command behind each guard" in maintenance_section
    for command in guard_commands:
        assert command in maintenance_section
        assert command in route_commands

    for command in RAW_ONBOARDING_GUARD_COMMANDS:
        assert command in contributing
        assert command not in route_commands

    for command in validation_commands:
        assert command in validation_section
        assert command in route_commands

    assert "easycat validate quick" not in route_commands
    assert "easycat validate report .easycat/validation/latest.json" not in route_commands
    assert "easycat validate report .easycat/validation/latest.json --json" not in route_commands


def test_validation_reference_docs_route_matches_json_commands() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}
    reference = (REPO_ROOT / "docs" / "reference" / "validation-vocabulary.md").read_text(
        encoding="utf-8"
    )
    route_commands = entries["docs/reference/validation-vocabulary.md"].get("commands", ())

    for command in (
        "easycat docs --audience release-maintainers",
        "easycat docs --audience release-maintainers --json",
        "easycat validate quick --json",
        "easycat validate contracts --json",
        "easycat validate release --json",
        "easycat validate report .easycat/validation/latest.json --json",
    ):
        assert command in reference
        assert command in route_commands

    assert "uv run easycat validate quick --json" not in route_commands
