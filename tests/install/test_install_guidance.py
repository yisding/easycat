from __future__ import annotations

import pytest

from tests.install._install_guidance_helpers import (
    EASYCAT_EXTRA_RE,
    META_ENTRIES,
    REPO_ROOT,
    REPO_UV_SYNC_EXTRA_COMMAND_RE,
    REPO_UV_SYNC_PYTHON_COMMAND_RE,
    SILERO_TORCH_REQUIRED_RE,
    STALE_INSTALL_PATTERNS,
    UV_EXTRA_RE,
    _iter_guidance_files,
    _iter_reader_guidance_files,
    _known_extras,
    _looks_like_placeholder,
    _normalize_extra,
    _readme_cli_section,
    command_hint_problems,
    documented_commands,
    re,
    tomllib,
)


def test_optional_extra_guidance_uses_current_uv_commands() -> None:
    """Keep onboarding hints aligned for package users and repo-local developers."""
    stale: list[str] = []

    for path in _iter_guidance_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        for label, pattern in STALE_INSTALL_PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                stale.append(f"{rel}:{line}: {label}")

    assert not stale, (
        "Optional-extra guidance should use `uv add 'easycat[...]'` and, for repo-local "
        "setup, `uv sync --extra ... --group dev`: " + "; ".join(stale)
    )


def test_pyproject_allows_the_audited_uv_minor_releases() -> None:
    """Contributor uv accepts the two minors verified by CI and the lockfile."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["tool"]["uv"]["required-version"] == ">=0.11.0,<0.13.0"


@pytest.mark.parametrize(
    ("pattern", "label"),
    [
        (REPO_UV_SYNC_EXTRA_COMMAND_RE, "extra"),
        (REPO_UV_SYNC_PYTHON_COMMAND_RE, "--python"),
    ],
)
def test_repo_local_uv_sync_guidance_keeps_dev_group(pattern: re.Pattern[str], label: str) -> None:
    """Repo-local uv sync setup (optional-extra or --python) should not drop dev tools."""
    stale: list[str] = []

    for path in _iter_guidance_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        for match in pattern.finditer(text):
            command = match.group(0).strip().rstrip(".,")
            if "--group dev" not in command:
                line = text.count("\n", 0, match.start()) + 1
                stale.append(f"{rel}:{line}: {command}")

    assert not stale, f"Repo-local uv sync {label} commands missing --group dev:\n" + "\n".join(
        stale
    )


def test_optional_extra_guidance_references_known_extras() -> None:
    """Catch typoed extras in source/doc install hints before users copy them."""
    known = _known_extras()
    unknown: list[str] = []

    for path in _iter_guidance_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()

        for match in UV_EXTRA_RE.finditer(text):
            extra = _normalize_extra(match.group("extra"))
            if extra not in known:
                line = text.count("\n", 0, match.start()) + 1
                unknown.append(f"{rel}:{line}: unknown --extra {extra!r}")

        for match in EASYCAT_EXTRA_RE.finditer(text):
            for extra in (_normalize_extra(part) for part in match.group("extras").split(",")):
                if not extra or _looks_like_placeholder(extra):
                    continue
                if extra not in known:
                    line = text.count("\n", 0, match.start()) + 1
                    unknown.append(f"{rel}:{line}: unknown easycat extra {extra!r}")

    assert not unknown, "Unknown EasyCat optional extras in install guidance:\n" + "\n".join(
        unknown
    )


def test_install_guide_has_copyable_commands_for_every_optional_dependency() -> None:
    """The authoritative install guide should expose every non-meta repo extra."""
    optional_block = (REPO_ROOT / "docs" / "install.md").read_text(encoding="utf-8")
    expected_extras = sorted(_known_extras() - {"all", "quickstart"})

    missing_commands = [
        f"uv sync --extra {extra} --group dev"
        for extra in expected_extras
        if f"uv sync --extra {extra} --group dev" not in optional_block
    ]

    assert not missing_commands, "Install guide optional dependency list missing: " + ", ".join(
        missing_commands
    )
    assert "uv pip install krisp_audio" in optional_block


def test_install_guide_documents_all_extra_exclusions() -> None:
    all_guidance = (REPO_ROOT / "docs" / "install.md").read_text(encoding="utf-8")

    assert "easycat[all,pydantic-ai]" in all_guidance
    assert "easycat[all,pydantic-ai-v2]" in all_guidance
    assert "uv sync --extra all --extra pydantic-ai --group dev" in all_guidance
    assert "uv sync --extra all --extra pydantic-ai-v2 --group dev" in all_guidance
    assert "`ten-vad`" in all_guidance
    assert "`pydantic-ai`" in all_guidance
    assert "`pydantic-ai-v2`" in all_guidance
    assert "`langchain-v0`" in all_guidance
    assert "Install `langchain-v0` separately" in all_guidance
    assert "mutually exclusive" in all_guidance


def test_install_guide_documents_both_langchain_lines() -> None:
    guidance = (REPO_ROOT / "docs" / "install.md").read_text(encoding="utf-8")

    assert "uv sync --extra langchain --group dev" in guidance
    assert "uv sync --extra langchain-v0 --group dev" in guidance
    assert "Both lines\nrun the same EasyCat bridge contract" in guidance
    assert "`langchain-v0` also cannot be combined with `langgraph`\nor `all`" in guidance


def test_pydantic_ai_v1_extra_has_a_dated_compatibility_exit_plan() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    compatibility_plan = pyproject.split("# Compatibility schedule:", 1)[1].split(
        "pydantic-ai-v2 =", 1
    )[0]

    assert "2027-07-31" in compatibility_plan
    assert "next breaking EasyCat release after that date" in compatibility_plan
    assert "transitional ``pydantic-ai-v2`` alias" in compatibility_plan


def test_local_audio_guidance_and_nightly_smoke_install_portaudio() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    nightly = (REPO_ROOT / ".github" / "workflows" / "nightly-validation.yml").read_text(
        encoding="utf-8"
    )

    for command in (
        "sudo apt-get install -y libportaudio2",
        "brew install portaudio",
    ):
        assert command in readme
    assert '["local", "quickstart", "all"]' in nightly
    assert "sudo apt-get install -y --no-install-recommends libportaudio2" in nightly


def test_quickstart_guidance_does_not_readd_bundled_extras() -> None:
    """``quickstart`` already includes several extras; avoid redundant setup."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = pyproject["project"]["optional-dependencies"]
    quickstart_deps = set(extras["quickstart"])
    bundled_extras = tuple(
        sorted(
            name
            for name, deps in extras.items()
            if name not in {"all", "quickstart"} and deps and set(deps).issubset(quickstart_deps)
        )
    )
    assert {
        "local",
        "openai",
        "openai-agents",
        "silero-vad",
        "smart-turn",
    }.issubset(bundled_extras)
    assert "rnnoise" not in bundled_extras

    redundant: list[str] = []
    extra_pattern = "|".join(re.escape(extra) for extra in bundled_extras)
    pattern = re.compile(rf"--extra\s+quickstart[^\n`|]*--extra\s+(?:{extra_pattern})")
    for path in _iter_guidance_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            redundant.append(f"{rel}:{line}")

    assert not redundant, (
        "Guidance should not re-add extras that `quickstart` already bundles: "
        + "; ".join(redundant)
    )


def test_rnnoise_demos_install_the_opt_in_extra() -> None:
    command = "uv sync --extra quickstart --extra rnnoise --group dev"
    for relative_path in (
        "examples/noise_reduction_backends.py",
        "examples/README.md",
        "docs/teaching/10-cleaning-signal/README.md",
    ):
        guidance = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert command in guidance, f"{relative_path} must install the RNNoise extra"


def test_silero_guidance_uses_bundled_onnx_not_torch() -> None:
    """Silero install docs should not send newcomers to PyTorch."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    silero_deps = pyproject["project"]["optional-dependencies"]["silero-vad"]
    install_guide = (REPO_ROOT / "docs" / "install.md").read_text(encoding="utf-8")
    stale: list[str] = []

    assert any(dep.startswith("onnxruntime") for dep in silero_deps)
    assert not any(dep.startswith("torch") for dep in silero_deps)
    assert "no torch required" in install_guide

    for path in _iter_reader_guidance_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        for match in SILERO_TORCH_REQUIRED_RE.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            stale.append(f"{rel}:{line}: {match.group(0).strip()}")

    assert not stale, "Silero guidance should use bundled ONNX, not torch:\n" + "\n".join(stale)


def test_reader_guidance_lets_easyconfig_read_openai_env_key() -> None:
    """Reader-facing EasyConfig snippets should rely on OPENAI_API_KEY setup."""
    exceptions = {
        # create_app(api_key=...) intentionally supports injection without
        # mutating process env, so this example must pass the key explicitly.
        REPO_ROOT / "examples" / "twilio_app.py",
        REPO_ROOT / "examples" / "telnyx_app.py",
    }
    stale: list[str] = []

    for path in _iter_reader_guidance_files():
        if path in exceptions:
            continue
        text = path.read_text(encoding="utf-8")
        if "openai_api_key=" in text:
            stale.append(path.relative_to(REPO_ROOT).as_posix())

    assert not stale, (
        "Reader-facing EasyConfig snippets should let EasyConfig read OPENAI_API_KEY:\n"
        + "\n".join(stale)
    )


def test_docs_json_guidance_points_to_schema_contract() -> None:
    """Automation route-map hints should also teach the JSON envelope contract."""
    missing: list[str] = []

    for path in _iter_reader_guidance_files():
        text = path.read_text(encoding="utf-8")
        if "easycat docs --json" not in text:
            continue
        if "easycat explain json-schema" not in text:
            missing.append(path.relative_to(REPO_ROOT).as_posix())

    assert not missing, (
        "`easycat docs --json` guidance should also point scripts/coding agents "
        "to `easycat explain json-schema`:\n" + "\n".join(missing)
    )


def test_env_file_doctor_guidance_points_to_json_variant() -> None:
    """Docs that mention ``.env`` doctor checks should also show the parseable form."""
    missing: list[str] = []

    for path in _iter_reader_guidance_files():
        text = path.read_text(encoding="utf-8")
        if "easycat doctor --env-file .env" not in text:
            continue
        if "easycat doctor --env-file .env --json" not in text:
            missing.append(path.relative_to(REPO_ROOT).as_posix())

    assert not missing, (
        "`.env` doctor guidance should also expose the machine-readable variant:\n"
        + "\n".join(missing)
    )


def test_template_list_guidance_points_to_catalog_json() -> None:
    """Template comparison guidance should expose the machine-readable catalog too."""
    missing: list[str] = []

    for path in _iter_reader_guidance_files():
        text = path.read_text(encoding="utf-8")
        if "easycat init --list-templates" not in text:
            continue
        if "copyable create/preflight/check/fix/docs/json-schema/run commands" not in text:
            continue
        if "easycat init --list-templates --json" not in text:
            missing.append(path.relative_to(REPO_ROOT).as_posix())

    assert not missing, (
        "Template comparison guidance with copyable commands should also point "
        "scripts/coding agents to `easycat init --list-templates --json`:\n" + "\n".join(missing)
    )


def test_readme_cli_command_examples_are_locally_valid() -> None:
    cli_section = _readme_cli_section()
    commands = documented_commands(
        cli_section,
        prefixes=("easycat ", "uv run easycat "),
    )

    problems = command_hint_problems(
        [
            {
                "label": "README.md CLI",
                "path": "README.md#cli",
                "audience": "app builders",
                "description": "Root README CLI command examples.",
                "commands": commands,
            }
        ],
        repo_root=REPO_ROOT,
    )

    assert commands
    assert not problems, "README.md CLI command examples are stale:\n" + "\n".join(problems)


def test_cli_reference_routes_automation_to_schema_command_families() -> None:
    """The CLI reference should route agents to each JSON command family."""
    cli_reference = (REPO_ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    normalized_reference = re.sub(r"\s+", " ", cli_reference)
    schema_body = META_ENTRIES["json-schema"].body

    command_families = (
        "easycat docs --json",
        "easycat docs --audience learners --json",
        "easycat init --list-templates --json",
        "easycat init NAME --json",
        "easycat doctor --json",
        "easycat validate quick --json",
        "easycat validate contracts --json",
        "easycat validate release --json",
        "easycat validate report PATH --json",
        "easycat bundles list --json",
        "easycat bundles show PATH --json",
        "easycat bundles export PATH --output DIR --json",
        "easycat inspect PATH --json",
        "easycat replay PATH --json",
    )

    for schema_command in command_families:
        assert schema_command in schema_body
    assert "`audience_filter`" in normalized_reference
    assert "`available_audiences`" in normalized_reference
    assert "`available_audience_filters`" in normalized_reference
    assert "`audience_alias_note`" in normalized_reference


def test_readme_cli_validate_examples_are_copyable() -> None:
    """Bare ``easycat validate`` shows help; the README should show useful subcommands."""
    cli_section = _readme_cli_section()
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    validation_doc = (REPO_ROOT / "docs" / "validation.md").read_text(encoding="utf-8")

    assert not re.search(r"(?m)^easycat validate\s+#", cli_section)
    assert "easycat validate report PATH" not in readme
    assert "easycat validate report PATH" not in validation_doc


def test_cli_init_examples_name_target_directory() -> None:
    """``easycat init`` requires NAME unless listing templates."""
    cli_section = _readme_cli_section()
    production_chapter = (
        REPO_ROOT / "docs" / "teaching" / "15-operate-in-production" / "README.md"
    ).read_text(encoding="utf-8")

    assert not re.search(r"(?m)^easycat init\s+#", cli_section)
    assert "**`uv run easycat init`**" not in production_chapter
