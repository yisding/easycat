from __future__ import annotations

from tests.install._install_guidance_helpers import (
    AGENT_GUIDE_SOURCE_PATH_SECTIONS,
    BRIDGE_DISPLAY_NAMES,
    CODE_SPAN_RE,
    GUIDE_JUST_COMMAND_RE,
    REPO_ROOT,
    _agent_guide_command_sections,
    _clean_code_span_path,
    _extract_markdown_section,
    _guide_pytest_commands,
    _source_path_candidates_for_agent_guide,
    command_hint_problems,
    documented_commands,
    just_recipe_names,
    pytest_target_problems,
    re,
)


def test_agent_guides_use_current_live_marker_name() -> None:
    stale: list[str] = []

    for filename in ("AGENTS.md", "CLAUDE.md"):
        text = (REPO_ROOT / filename).read_text(encoding="utf-8")
        if "@pytest.mark.integration`" in text or "@pytest.mark.integration " in text:
            stale.append(filename)
        assert "@pytest.mark.integration_live" in text

    assert not stale, "Agent guides should use integration_live, not integration: " + ", ".join(
        stale
    )


def test_agent_guide_pytest_command_extractor_handles_guide_formats() -> None:
    command_section = """
- `uv run pytest tests/cli/test_app.py::test_docs_command`.

```bash
uv run pytest tests/cli/test_app.py::test_docs_command_json  # Comment
```
"""

    assert _guide_pytest_commands(command_section) == [
        "uv run pytest tests/cli/test_app.py::test_docs_command",
        "uv run pytest tests/cli/test_app.py::test_docs_command_json",
    ]


def test_agent_guide_easycat_command_validator_checks_nested_commands() -> None:
    command_section = """
- `uv run easycat validate not-a-lane`.

```bash
uv run easycat docs --audience time-travelers  # Comment
```
"""
    commands = documented_commands(
        command_section,
        prefixes=("uv run easycat ", "easycat "),
    )

    problems = command_hint_problems(
        [
            {
                "label": "Broken agent guide",
                "path": "AGENTS.md",
                "audience": "coding agents",
                "description": "Regression fixture for guide command validation.",
                "commands": commands,
            }
        ],
        repo_root=REPO_ROOT,
    )

    assert "Broken agent guide: unknown easycat validate command not-a-lane" in problems
    assert "Broken agent guide: unknown docs audience hint time-travelers" in problems


def test_agent_guide_command_examples_are_current() -> None:
    just_recipes = just_recipe_names(REPO_ROOT)
    command_sections = _agent_guide_command_sections()

    stale_recipes: list[str] = []
    for filename, command_section in command_sections.items():
        for match in GUIDE_JUST_COMMAND_RE.finditer(command_section):
            recipe = match.group("recipe")
            if recipe not in just_recipes:
                stale_recipes.append(f"{filename}: just {recipe}")

    stale_pytest_targets: list[str] = []
    for filename, command_section in command_sections.items():
        for command in _guide_pytest_commands(command_section):
            stale_pytest_targets.extend(
                pytest_target_problems(command, repo_root=REPO_ROOT, label=filename)
            )

    stale_easycat_commands: list[str] = []
    for filename, command_section in command_sections.items():
        commands = documented_commands(
            command_section,
            prefixes=("uv run easycat ", "easycat "),
        )
        stale_easycat_commands.extend(
            command_hint_problems(
                [
                    {
                        "label": filename,
                        "path": filename,
                        "audience": "coding agents",
                        "description": "Agent guide command examples.",
                        "commands": commands,
                    }
                ],
                repo_root=REPO_ROOT,
            )
        )

    assert not stale_recipes, "Agent guide just examples point at missing recipes: " + ", ".join(
        stale_recipes
    )
    assert not stale_pytest_targets, "Agent guide pytest examples are stale:\n" + "\n".join(
        stale_pytest_targets
    )
    assert not stale_easycat_commands, "Agent guide easycat examples are stale:\n" + "\n".join(
        stale_easycat_commands
    )


def test_agent_guide_command_hints_are_locally_valid() -> None:
    stale_commands: list[str] = []

    for filename, command_section in _agent_guide_command_sections().items():
        commands = tuple(
            command
            for command in documented_commands(
                command_section,
                prefixes=("just", "uv sync ", "uv run ", "uvx "),
            )
            if command == "just" or command.startswith(("just ", "uv sync ", "uv run ", "uvx "))
        )
        assert commands, filename
        stale_commands.extend(
            command_hint_problems(
                [
                    {
                        "label": filename,
                        "path": filename,
                        "audience": "coding agents",
                        "description": "Agent guide build, test, docs, and validation commands.",
                        "commands": commands,
                    }
                ],
                repo_root=REPO_ROOT,
            )
        )

    assert not stale_commands, "Agent guide command hints are stale:\n" + "\n".join(stale_commands)


def test_agent_guides_preflight_credentialed_example_runs() -> None:
    stale: list[str] = []
    plain_doctor_re = re.compile(r"(?m)(?:^- `|^)uv run easycat doctor(?:`|\s+#|$)")

    for filename, command_section in _agent_guide_command_sections().items():
        doctor_match = plain_doctor_re.search(command_section)
        for command in (
            "uv run python examples/ws_server.py",
            "uv run python examples/webrtc_server.py",
        ):
            command_index = command_section.find(command)
            if command_index == -1:
                continue
            if doctor_match is None:
                stale.append(f"{filename}: missing plain doctor preflight before `{command}`")
                continue
            if doctor_match.start() > command_index:
                stale.append(f"{filename}: doctor preflight appears after `{command}`")

    assert not stale, (
        "Agent guide credentialed example commands should be preceded by "
        "`uv run easycat doctor`: " + "; ".join(stale)
    )


def test_agent_guides_reference_config_package_layout() -> None:
    assert (REPO_ROOT / "src" / "easycat" / "config").is_dir()
    assert not (REPO_ROOT / "src" / "easycat" / "config.py").exists()

    stale_mentions: list[str] = []
    for filename in ("AGENTS.md", "CLAUDE.md"):
        text = (REPO_ROOT / filename).read_text(encoding="utf-8")
        assert "`config/`" in text, filename
        if "`config.py`" in text:
            stale_mentions.append(filename)

    assert not stale_mentions, (
        "Agent guides should reference config/, not config.py: " + ", ".join(stale_mentions)
    )


def test_agent_guides_name_major_source_and_test_packages() -> None:
    """Keep first-contact maintainer maps aligned with major source and test packages."""
    src_prefix, test_prefix = "src/easycat", "tests"
    for package_name in ("cli", "debugger", "vad", "validation"):
        assert (REPO_ROOT / src_prefix / package_name).is_dir()
        assert (REPO_ROOT / test_prefix / package_name).is_dir()

    missing: list[str] = []
    for filename in ("AGENTS.md", "CLAUDE.md"):
        text = (REPO_ROOT / filename).read_text(encoding="utf-8")
        for package_name in ("cli", "debugger", "vad", "validation"):
            source_mention = f"`{package_name}/`"
            if source_mention not in text:
                missing.append(f"{filename}: {source_mention}")
            test_mention = f"`{test_prefix}/{package_name}/`"
            if test_mention not in text:
                missing.append(f"{filename}: {test_mention}")

    assert not missing, "Agent guides missing major source/test packages: " + ", ".join(missing)


def test_agent_guide_source_path_mentions_exist() -> None:
    missing: list[str] = []

    for filename, (start_heading, end_heading) in AGENT_GUIDE_SOURCE_PATH_SECTIONS.items():
        text = (REPO_ROOT / filename).read_text(encoding="utf-8")
        section, start_line = _extract_markdown_section(text, start_heading, end_heading)
        for offset, line in enumerate(section.splitlines()):
            line_number = start_line + offset
            for match in CODE_SPAN_RE.finditer(line):
                path_text = _clean_code_span_path(match.group(1))
                candidates = _source_path_candidates_for_agent_guide(
                    filename,
                    line,
                    path_text,
                )
                if candidates and not any(path.exists() for path in candidates):
                    missing.append(f"{filename}:{line_number}: `{path_text}`")

    assert not missing, "Agent guide source path mentions are stale:\n" + "\n".join(missing)


def test_claude_overview_tracks_public_agent_bridges() -> None:
    from easycat.integrations import agents as agent_integrations

    bridge_names = {
        name
        for name in agent_integrations.__all__
        if name.endswith("Bridge") and name != "ExternalAgentBridge"
    }
    missing_display_map = sorted(bridge_names - set(BRIDGE_DISPLAY_NAMES))
    assert not missing_display_map, "CLAUDE.md bridge display map missing: " + ", ".join(
        missing_display_map
    )

    overview = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8").split("## Commands", 1)[0]
    missing_display_names = sorted(
        display_name
        for bridge_name, display_name in BRIDGE_DISPLAY_NAMES.items()
        if bridge_name in bridge_names and display_name not in overview
    )

    assert not missing_display_names, (
        "CLAUDE.md overview missing public bridge labels: " + ", ".join(missing_display_names)
    )


def test_claude_provider_registry_guidance_tracks_factory_names() -> None:
    from easycat.stt import factory as stt_factory
    from easycat.tts import factory as tts_factory

    key_patterns = (
        (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8").split("## Session Lifecycle", 1)[0]
    )

    assert hasattr(stt_factory, "_PROVIDER_TO_CONFIG")
    assert hasattr(tts_factory, "_PROVIDER_TO_CONFIG")

    assert "stt/factory.py" in key_patterns
    assert "tts/factory.py" in key_patterns
    assert "_PROVIDER_TO_CONFIG" in key_patterns
