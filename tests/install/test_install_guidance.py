from __future__ import annotations

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


def test_repo_local_uv_sync_extra_guidance_keeps_dev_group() -> None:
    """Repo-local optional-extra setup should not drop development tools."""
    stale: list[str] = []

    for path in _iter_guidance_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        for match in REPO_UV_SYNC_EXTRA_COMMAND_RE.finditer(text):
            command = match.group(0).strip().rstrip(".,")
            if "--group dev" not in command:
                line = text.count("\n", 0, match.start()) + 1
                stale.append(f"{rel}:{line}: {command}")

    assert not stale, "Repo-local uv sync extra commands missing --group dev:\n" + "\n".join(stale)


def test_repo_local_uv_sync_python_guidance_keeps_dev_group() -> None:
    """Repo-local Python-version sync hints should still install dev tools."""
    stale: list[str] = []

    for path in _iter_guidance_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        for match in REPO_UV_SYNC_PYTHON_COMMAND_RE.finditer(text):
            command = match.group(0).strip().rstrip(".,")
            if "--group dev" not in command:
                line = text.count("\n", 0, match.start()) + 1
                stale.append(f"{rel}:{line}: {command}")

    assert not stale, "Repo-local uv sync --python commands missing --group dev:\n" + "\n".join(
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


def test_readme_optional_dependency_list_has_copyable_install_commands() -> None:
    """The optional-dependency list should expose every non-meta repo extra."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    optional_block = readme.split("Optional dependencies you may need depending on", 1)[1].split(
        "## CLI", 1
    )[0]
    expected_extras = sorted(_known_extras() - {"all", "quickstart"})

    missing_commands = [
        f"uv sync --extra {extra} --group dev"
        for extra in expected_extras
        if f"uv sync --extra {extra} --group dev" not in optional_block
    ]

    assert not missing_commands, "README optional dependency list missing: " + ", ".join(
        missing_commands
    )
    assert "uv pip install krisp_audio" in optional_block


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
        "rnnoise",
        "silero-vad",
        "smart-turn",
    }.issubset(bundled_extras)

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


def test_silero_guidance_uses_bundled_onnx_not_torch() -> None:
    """Silero install docs should not send newcomers to PyTorch."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    silero_deps = pyproject["project"]["optional-dependencies"]["silero-vad"]
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    stale: list[str] = []

    assert any(dep.startswith("onnxruntime") for dep in silero_deps)
    assert not any(dep.startswith("torch") for dep in silero_deps)
    assert "no torch required" in readme

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


def test_readme_cli_explain_examples_are_copyable() -> None:
    """``easycat explain`` requires a code or --list; the README should show one."""
    cli_section = _readme_cli_section()

    assert not re.search(r"(?m)^easycat explain\s+#", cli_section)
    assert "easycat explain E102" in cli_section
    assert "easycat explain json-schema" in cli_section
    assert "easycat explain --list" in cli_section
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    normalized_readme = re.sub(r"\s+", " ", readme)
    assert "standard `--json` envelope" in normalized_readme
    assert "command-specific fields" in normalized_readme
    assert "`entries`, `commands`, `catalog`" in normalized_readme
    assert "`command_note`" in normalized_readme
    assert "`available_audience_filters`" in normalized_readme
    assert "`audience_alias_note`" in normalized_readme
    assert "`base_requirement`, `create_command`, `repo_create_command`" in normalized_readme
    assert "`next_step_commands`" in normalized_readme
    assert "`pyproject_name`, `run_command`" in normalized_readme
    assert "`run_command`, `check_command`, `fix_command`, `environment`, `checks`" in (
        normalized_readme
    )
    assert "`fix_command`, `environment`, `checks`, `validation`" in normalized_readme
    assert "`source_path`, and `fidelity_effective`" in normalized_readme
    assert (
        "Replace uppercase or angle-bracket placeholders in command hints, such as `PATH` "
        "or `<session_id>`"
    ) in normalized_readme


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


def test_readme_json_guidance_covers_schema_command_families() -> None:
    """README automation guidance should route agents to each JSON command family."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    normalized_readme = re.sub(r"\s+", " ", readme)
    schema_body = META_ENTRIES["json-schema"].body

    command_family_mentions = {
        "easycat docs --json": "docs route map",
        "easycat docs --audience learners --json": "docs route map",
        "easycat init --list-templates --json": "template catalog",
        "easycat init NAME --json": "scaffold output",
        "easycat doctor --json": "doctor environment/checks output",
        "easycat validate quick --json": "validation quick/contracts/release/report output",
        "easycat validate contracts --json": ("validation quick/contracts/release/report output"),
        "easycat validate release --json": ("validation quick/contracts/release/report output"),
        "easycat validate report PATH --json": (
            "validation quick/contracts/release/report output"
        ),
        "easycat bundles list --json": "bundle list/show/export",
        "easycat bundles show PATH --json": "bundle list/show/export",
        "easycat bundles export PATH --output DIR --json": "bundle list/show/export",
        "easycat inspect PATH --json": "inspect",
        "easycat replay PATH --json": "replay",
    }

    for schema_command, readme_phrase in command_family_mentions.items():
        assert schema_command in schema_body
        assert readme_phrase in normalized_readme
    assert "`audience_filter`" in normalized_readme
    assert "`available_audiences`" in normalized_readme
    assert "`available_audience_filters`" in normalized_readme
    assert "`audience_alias_note`" in normalized_readme


def test_readme_cli_debug_json_examples_are_copyable() -> None:
    """Debug CLI commands should include machine-readable support handoffs."""
    cli_section = _readme_cli_section()

    for command in (
        "easycat bundles list --json",
        "easycat bundles show PATH --json",
        "easycat bundles export PATH --output DIR --json",
        "easycat inspect PATH --json",
        "easycat replay PATH --json",
    ):
        assert command in cli_section
    assert "machine-readable bundle list" in cli_section
    assert "machine-readable bundle/journal summary" in cli_section
    assert "context-pack metadata" in cli_section
    assert "machine-readable replay summary" in cli_section


def test_readme_cli_validate_examples_are_copyable() -> None:
    """Bare ``easycat validate`` shows help; the README should show useful subcommands."""
    cli_section = _readme_cli_section()
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    validation_doc = (REPO_ROOT / "docs" / "validation.md").read_text(encoding="utf-8")

    assert not re.search(r"(?m)^easycat validate\s+#", cli_section)
    assert "easycat validate quick" in cli_section
    assert "easycat validate quick --json" in cli_section
    assert "easycat validate contracts" in cli_section
    assert "easycat validate contracts --json" in cli_section
    assert "easycat validate release" in cli_section
    assert "easycat validate release --json" in cli_section
    assert "easycat validate report .easycat/validation/latest.json" in cli_section
    assert "easycat validate report .easycat/validation/latest.json --json" in cli_section
    assert "uv run easycat validate quick --json" in validation_doc
    assert "uv run easycat validate contracts --json" in validation_doc
    assert "uv run easycat validate release --json" in validation_doc
    assert "uv run easycat validate report .easycat/validation/latest.json --json" in (
        validation_doc
    )
    assert "easycat validate report PATH" not in readme
    assert "easycat validate report PATH" not in validation_doc


def test_readme_cli_doctor_documents_env_file_option() -> None:
    """``easycat doctor`` should show the direct .env path for scaffold users."""
    cli_section = _readme_cli_section()
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    normalized_readme = re.sub(r"\s+", " ", readme)

    assert "easycat doctor --env-file .env" in cli_section
    assert "easycat doctor --json" in cli_section
    assert "environment/check rows without Rich formatting" in normalized_readme


def test_cli_init_examples_name_target_directory() -> None:
    """``easycat init`` requires NAME unless listing templates."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    normalized_readme = re.sub(r"\s+", " ", readme)
    cli_section = _readme_cli_section()
    production_chapter = (
        REPO_ROOT / "docs" / "teaching" / "15-operate-in-production" / "README.md"
    ).read_text(encoding="utf-8")
    normalized_production_chapter = re.sub(r"\s+", " ", production_chapter)

    assert not re.search(r"(?m)^easycat init\s+#", cli_section)
    assert "easycat init my-agent" in cli_section
    assert "easycat init --list-templates" in cli_section
    assert "easycat init --list-templates --json" in cli_section
    assert "`easycat init my-agent` scaffolds" in readme
    assert "`easycat init --list-templates` shows" in readme
    assert "base `easycat[...]` package requirement and extras" in normalized_readme
    assert "required environment variables" in normalized_readme
    assert "optional environment knobs" in normalized_readme
    assert "generated files" in normalized_readme
    assert "copyable create/preflight/check/fix/docs/json-schema/run commands" in (
        normalized_readme
    )
    assert "`uv run easycat init my-agent`" in production_chapter
    assert "`uv run easycat init --list-templates`" in production_chapter
    assert "base `easycat[...]` package requirements and extras" in normalized_production_chapter
    assert "required environment variables" in normalized_production_chapter
    assert "optional environment knobs" in normalized_production_chapter
    assert "generated files" in normalized_production_chapter
    assert "copyable create/preflight/check/fix/docs/json-schema/run commands" in (
        normalized_production_chapter
    )
    assert "**`uv run easycat init`**" not in production_chapter
