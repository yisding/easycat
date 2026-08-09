from __future__ import annotations

from tests.examples._examples_helpers import (
    _UV_SYNC_EXTRA_RE,
    REPO_ROOT,
    Path,
    _app_extras_in,
    _documented_pip_packages,
    _documented_setup_extras,
    _example_readme_rows,
    _example_run_command_problems,
    _import_error_system_exit_messages,
    _readme_install_packages,
    _referenced_env_vars,
    _top_level_example_names,
    _uses_default_openai_providers,
    _visible_code_line_count,
    ast,
    command_hint_problems,
    documented_commands,
    pytest,
    re,
    tomllib,
)


@pytest.mark.parametrize(
    ("example_name", "budget"),
    [
        ("openai_agents_voice.py", 7),
        ("pydantic_ai_voice.py", 8),
        ("ws_server.py", 23),
    ],
)
def test_canonical_local_voice_examples_keep_visible_code_budget(
    example_name: str,
    budget: int,
) -> None:
    path = REPO_ROOT / "examples" / example_name

    assert _visible_code_line_count(path) <= budget


def test_examples_readme_lists_every_top_level_python_example() -> None:
    row_names = {row["link"] for row in _example_readme_rows()}
    missing = sorted(_top_level_example_names() - row_names)

    assert not missing, "examples/README.md missing example rows for: " + ", ".join(missing)


def test_examples_readme_fastest_path_verifies_environment_before_running() -> None:
    readme = (REPO_ROOT / "examples" / "README.md").read_text(encoding="utf-8")
    fast_path = readme.split("For the fastest local mic/speaker path:", 1)[1]
    commands = fast_path.split("```bash", 1)[1].split("```", 1)[0].strip().splitlines()

    assert commands == [
        "uv sync --extra quickstart --group dev",
        'export OPENAI_API_KEY="your-api-key"',
        "uv run easycat doctor",
        "uv run python examples/openai_agents_voice.py",
    ]


def test_examples_readme_command_hints_are_locally_valid() -> None:
    readme = (REPO_ROOT / "examples" / "README.md").read_text(encoding="utf-8")
    commands = documented_commands(
        readme,
        prefixes=("uv run ", "uv sync ", "easycat "),
    )
    problems = command_hint_problems(
        [
            {
                "label": "examples/README.md",
                "path": "examples/README.md",
                "audience": "app builders",
                "description": "Example README setup, preflight, validation, and run commands.",
                "commands": commands,
            }
        ],
        repo_root=REPO_ROOT,
    )

    assert commands
    assert not problems, "examples/README.md command hints are stale:\n" + "\n".join(problems)


def test_examples_readme_choose_example_table_tracks_matrix() -> None:
    readme = (REPO_ROOT / "examples" / "README.md").read_text(encoding="utf-8")
    table = readme.split("## Choose An Example", 1)[1].split("## Core Voice Loops", 1)[0]
    rows = {row["link"]: row for row in _example_readme_rows()}
    linked_examples = set(re.findall(r"\[([^]]+\.py)\]\(([^)]+\.py)\)", table))
    linked_paths = {link for display, link in linked_examples}

    assert linked_paths <= set(rows), "Chooser links missing from example matrix"
    for display, link in linked_examples:
        assert display == link
    for example in ("journal_demo.py", "telephony_helpers.py"):
        assert rows[example]["env"] == "None"
    assert "OPENAI_API_KEY" in rows["openai_agents_voice.py"]["env"]
    assert "--extra quickstart" in rows["openai_agents_voice.py"]["install"]
    for example in ("deepgram_voice.py", "elevenlabs_voice.py", "combined_providers.py"):
        assert "OPENAI_API_KEY" in rows[example]["env"]
        assert "--extra" in rows[example]["install"]
    assert "debug_bundle.py" in linked_paths
    assert "journal_ui.py" in linked_paths


def test_examples_readme_no_key_chooser_avoids_optional_extra_setup() -> None:
    readme = (REPO_ROOT / "examples" / "README.md").read_text(encoding="utf-8")
    table = readme.split("## Choose An Example", 1)[1].split("## Core Voice Loops", 1)[0]
    no_key_row = next(line for line in table.splitlines() if line.startswith("| No API keys |"))
    no_key_links = re.findall(r"\[([^]]+\.py)\]\(([^)]+\.py)\)", no_key_row)
    rows = {row["link"]: row for row in _example_readme_rows()}
    stale: list[str] = []

    assert no_key_links, "No-key chooser row should link offline examples"

    for display, link in no_key_links:
        assert display == link
        row = rows[link]
        path = REPO_ROOT / "examples" / link
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
        if row["env"] != "None":
            stale.append(f"{link}: env cell is {row['env']!r}")
        if "--extra" in row["install"]:
            stale.append(f"{link}: README install uses optional extras")
        if "uv sync --extra" in doc:
            stale.append(f"{link}: docstring setup uses optional extras")
        if "uv sync --group dev" not in doc:
            stale.append(f"{link}: docstring missing base dev sync")

    assert not stale, "No-key chooser examples should stay lightweight: " + "; ".join(stale)


def test_examples_readme_rows_are_command_map_entries() -> None:
    rows = _example_readme_rows()
    row_names = [row["link"] for row in rows]
    duplicate_rows = sorted({name for name in row_names if row_names.count(name) > 1})
    unknown_rows = sorted(set(row_names) - _top_level_example_names())

    assert not duplicate_rows, "examples/README.md has duplicate example rows: " + ", ".join(
        duplicate_rows
    )
    assert not unknown_rows, "examples/README.md links unknown examples: " + ", ".join(
        unknown_rows
    )

    stale_rows: list[str] = []
    for row in rows:
        link = row["link"]
        stem = Path(link).stem
        run_command = row["run"]
        if row["name"] != link:
            stale_rows.append(f"{link}: display name is {row['name']}")
        stale_rows.extend(_example_run_command_problems(row))
        if f"examples/{link}" not in run_command and f"examples.{stem}" not in run_command:
            stale_rows.append(f"{link}: run command does not reference linked example")
        if not row["install"].startswith("`uv sync "):
            stale_rows.append(f"{link}: install cell does not start with `uv sync`")
        if not row["use_when"].strip():
            stale_rows.append(f"{link}: missing Use When text")
        if not row["env"].strip():
            stale_rows.append(f"{link}: missing Env text")

    assert not stale_rows, "Stale examples/README.md rows: " + "; ".join(stale_rows)


def test_examples_readme_run_command_validator_checks_targets() -> None:
    problems = [
        problem
        for row in (
            {"link": "openai_agents_voice.py", "run": "uv run python examples/missing.py"},
            {"link": "twilio_app.py", "run": "uv run uvicorn examples.missing:create_app"},
            {"link": "openai_agents_voice.py", "run": "uv run python"},
            {"link": "openai_agents_voice.py", "run": "uv run easycat doctor"},
        )
        for problem in _example_run_command_problems(row)
    ]

    assert (
        "openai_agents_voice.py: run command script examples/missing.py does not exist" in problems
    )
    assert (
        "openai_agents_voice.py: run command script examples/missing.py "
        "does not match linked example"
    ) in problems
    assert "twilio_app.py: run command ASGI target examples.missing:create_app does not exist" in (
        problems
    )
    assert (
        "twilio_app.py: run command ASGI target examples.missing:create_app "
        "does not match linked example"
    ) in problems
    assert "openai_agents_voice.py: run command is missing a Python script" in problems
    assert "openai_agents_voice.py: unsupported run command executable easycat" in problems


def test_examples_readme_repo_sync_install_cells_include_dev_group() -> None:
    stale: list[str] = []

    for row in _example_readme_rows():
        install = row["install"]
        if "uv sync " not in install:
            continue
        if "--group dev" not in install:
            stale.append(f"{row['link']}: {install}")

    assert not stale, "examples/README.md install cells missing --group dev: " + "; ".join(stale)


def test_examples_readme_run_commands_match_example_docstrings() -> None:
    stale: list[str] = []

    for row in _example_readme_rows():
        path = REPO_ROOT / "examples" / row["link"]
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        doc = ast.get_docstring(module) or ""
        if row["run"] not in doc:
            stale.append(f"{row['link']}: missing `{row['run']}`")

    assert not stale, "examples/README.md run commands drifted from docstrings: " + "; ".join(
        stale
    )


def test_examples_readme_references_known_easycat_extras() -> None:
    readme = (REPO_ROOT / "examples" / "README.md").read_text(encoding="utf-8")
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    known_extras = set(pyproject["project"]["optional-dependencies"])

    referenced_extras = set(re.findall(r"--extra ([A-Za-z0-9_.-]+)", readme))
    unknown_extras = sorted(referenced_extras - known_extras)

    assert not unknown_extras, (
        "examples/README.md references unknown EasyCat extras: " + ", ".join(unknown_extras)
    )


def test_examples_readme_install_extras_cover_docstring_setup() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = pyproject["project"]["optional-dependencies"]
    quickstart_deps = set(extras["quickstart"])
    bundled_by_quickstart = {
        name
        for name, deps in extras.items()
        if name != "quickstart" and deps and set(deps).issubset(quickstart_deps)
    }
    stale: list[str] = []

    for row in _example_readme_rows():
        path = REPO_ROOT / "examples" / row["link"]
        documented_extras = _documented_setup_extras(path)
        if not documented_extras:
            continue

        row_extras = set(_UV_SYNC_EXTRA_RE.findall(row["install"]))
        optional_if_quickstart = bundled_by_quickstart if "quickstart" in row_extras else set()
        missing = sorted(documented_extras - row_extras - optional_if_quickstart)
        if missing:
            stale.append(f"{row['link']}: missing {missing}")

    assert not stale, "examples/README.md install cells omit setup extras: " + "; ".join(stale)


def test_examples_readme_install_package_collector_reads_pip_and_package_snippets() -> None:
    install = (
        "`uv sync --extra quickstart --group dev`; `acme-sdk<1`, `acme-plugin<1`, "
        "`--extra ten-vad`, or `uv pip install krisp_audio` for optional backends"
    )

    assert _readme_install_packages(install) == {
        "acme-plugin<1",
        "acme-sdk<1",
        "krisp_audio",
    }


def test_default_openai_provider_examples_install_openai_sdk() -> None:
    stale: list[str] = []

    for row in _example_readme_rows():
        path = REPO_ROOT / "examples" / row["link"]
        if not _uses_default_openai_providers(path):
            continue

        install_extras = set(_UV_SYNC_EXTRA_RE.findall(row["install"]))
        docstring_extras = _documented_setup_extras(path)
        for label, extras in (
            ("examples/README.md install", install_extras),
            ("example docstring setup", docstring_extras),
        ):
            if "quickstart" not in extras and "openai" not in extras:
                stale.append(f"{row['link']}: {label} missing --extra openai")

    assert not stale, "Default OpenAI provider examples omit the OpenAI SDK: " + "; ".join(stale)


def test_example_env_var_collector_reads_direct_environ_access(tmp_path: Path) -> None:
    path = tmp_path / "example.py"
    path.write_text(
        (
            'import os\nrequire_env("REQUIRED_API_KEY")\nos.getenv("OPTIONAL_TOKEN")\n'
            'os.environ.get("OPTIONAL_HOST")\nos.environ["DIRECT_SECRET"]\n'
            '_env_flag("FEATURE_FLAG")'
        ),
        encoding="utf-8",
    )

    assert _referenced_env_vars(path) == {
        "DIRECT_SECRET",
        "FEATURE_FLAG",
        "OPTIONAL_HOST",
        "OPTIONAL_TOKEN",
        "REQUIRED_API_KEY",
    }


def test_examples_readme_env_cells_cover_referenced_env_vars() -> None:
    stale: list[str] = []

    for row in _example_readme_rows():
        path = REPO_ROOT / "examples" / row["link"]
        referenced = _referenced_env_vars(path)
        if not referenced:
            continue
        missing = sorted(name for name in referenced if name not in row["env"])
        if missing:
            stale.append(f"{row['link']}: missing {missing}")

    assert not stale, "examples/README.md env cells omit referenced env vars: " + "; ".join(stale)


def test_env_examples_document_doctor_preflight() -> None:
    missing_doctor: list[str] = []
    missing_env_file: list[str] = []
    missing_env_file_json: list[str] = []
    missing_env_run: list[str] = []

    for row in _example_readme_rows():
        if row["env"].startswith("None"):
            continue
        env_run = row["run"].replace("uv run ", "uv run --env-file .env ", 1)
        path = REPO_ROOT / "examples" / row["link"]
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        doc = ast.get_docstring(module) or ""
        if "uv run easycat doctor" not in doc:
            missing_doctor.append(row["link"])
        if "uv run easycat doctor --env-file .env" not in doc:
            missing_env_file.append(row["link"])
        if "uv run easycat doctor --env-file .env --json" not in doc:
            missing_env_file_json.append(row["link"])
        if env_run not in doc:
            missing_env_run.append(f"{row['link']}: `{env_run}`")

    assert not missing_doctor, (
        "Example docstrings with required env vars should document "
        "`uv run easycat doctor`: " + ", ".join(missing_doctor)
    )
    assert not missing_env_file, (
        "Example docstrings with required env vars should document "
        "`uv run easycat doctor --env-file .env`: " + ", ".join(missing_env_file)
    )
    assert not missing_env_file_json, (
        "Example docstrings with required env vars should document "
        "`uv run easycat doctor --env-file .env --json`: " + ", ".join(missing_env_file_json)
    )
    assert not missing_env_run, (
        "Example docstrings with required env vars should document the `.env` run command: "
        + "; ".join(missing_env_run)
    )


def test_mixed_client_server_examples_document_server_preflight() -> None:
    stale: list[str] = []

    for row in _example_readme_rows():
        if not row["env"].startswith("None for client") or "API_KEY" not in row["env"]:
            continue
        path = REPO_ROOT / "examples" / row["link"]
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        doc = ast.get_docstring(module) or ""
        paired_server_runs = [
            line.strip()
            for line in doc.splitlines()
            if line.strip().startswith("uv run python examples/") and line.strip() != row["run"]
        ]
        if not paired_server_runs:
            stale.append(f"{row['link']}: missing paired server run command")
        if "uv run easycat doctor" not in doc:
            stale.append(f"{row['link']}: missing `uv run easycat doctor`")
        if "uv run easycat doctor --env-file .env" not in doc:
            stale.append(f"{row['link']}: missing `.env` doctor command")
        if "uv run easycat doctor --env-file .env --json" not in doc:
            stale.append(f"{row['link']}: missing parseable `.env` doctor command")
        for run_command in paired_server_runs:
            env_run = run_command.replace("uv run ", "uv run --env-file .env ", 1)
            if env_run not in doc:
                stale.append(f"{row['link']}: missing `{env_run}`")

    assert not stale, (
        "Client examples that rely on a credentialed paired server should document "
        "the server doctor preflight: " + "; ".join(stale)
    )


def test_examples_readme_none_env_rows_are_explicit_in_docstrings() -> None:
    stale: list[str] = []

    for row in _example_readme_rows():
        if not row["env"].startswith("None"):
            continue
        path = REPO_ROOT / "examples" / row["link"]
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        doc = ast.get_docstring(module) or ""
        if "for client" in row["env"]:
            required = "client; no API keys required"
            if required not in doc:
                stale.append(f"{row['link']}: missing {required!r}")
        elif "No API keys required" not in doc and "does NOT need any API keys" not in doc:
            stale.append(f"{row['link']}: missing no-API-key note")

    assert not stale, "Example docstrings with None env cells need explicit notes: " + "; ".join(
        stale
    )


def test_examples_readme_install_cells_cover_docstring_pip_packages() -> None:
    stale: list[str] = []

    for row in _example_readme_rows():
        path = REPO_ROOT / "examples" / row["link"]
        documented_packages = _documented_pip_packages(path)
        if not documented_packages:
            continue

        row_packages = _readme_install_packages(row["install"])
        missing = sorted(documented_packages - row_packages)
        if missing:
            stale.append(f"{row['link']}: missing {missing}")

    assert not stale, "examples/README.md install cells omit pip packages: " + "; ".join(stale)


def test_top_level_examples_document_setup_and_run_commands() -> None:
    missing: list[str] = []

    for path in sorted((REPO_ROOT / "examples").glob("*.py")):
        if path.name == "__init__.py":
            continue
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        doc = ast.get_docstring(module) or ""
        has_setup = "uv sync" in doc or "uv add" in doc
        has_run = "uv run" in doc
        if not has_setup or not has_run:
            missing.append(
                f"{path.name} (setup={'yes' if has_setup else 'no'}, "
                f"run={'yes' if has_run else 'no'})"
            )

    assert not missing, "Example docstrings missing setup/run guidance: " + ", ".join(missing)


def test_top_level_examples_use_repo_local_extra_setup_commands() -> None:
    stale: list[str] = []

    for example_name in sorted(_top_level_example_names()):
        path = REPO_ROOT / "examples" / example_name
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        doc = ast.get_docstring(module) or ""
        if "easycat[" in doc:
            stale.append(example_name)

    assert not stale, (
        "Top-level example setup docstrings should describe repo extras with "
        "`uv sync --extra ...`, not `easycat[...]`: " + ", ".join(stale)
    )


def test_example_import_guards_include_package_and_repo_install_paths() -> None:
    stale: list[str] = []

    for example_name in sorted(_top_level_example_names()):
        path = REPO_ROOT / "examples" / example_name
        for message in _import_error_system_exit_messages(path):
            if "uv sync --extra" not in message:
                continue
            missing_bits = [
                bit
                for bit in ("For an app, run:", "uv add 'easycat[", "In this repo, run:")
                if bit not in message
            ]
            if "Install with: uv sync" in message:
                missing_bits.append("no repo-only Install with wording")
            if missing_bits:
                stale.append(f"{example_name}: missing {', '.join(missing_bits)}")

    assert not stale, "Example import guards missing install paths: " + "; ".join(stale)


def test_example_import_guards_match_documented_setup_extras() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    known_extras = set(pyproject["project"]["optional-dependencies"])
    stale: list[str] = []

    for example_name in sorted(_top_level_example_names()):
        path = REPO_ROOT / "examples" / example_name
        documented_extras = _documented_setup_extras(path)
        if not documented_extras:
            continue

        documented_app_extras = documented_extras & known_extras
        for message in _import_error_system_exit_messages(path):
            if "uv sync --extra" not in message:
                continue
            repo_extras = set(_UV_SYNC_EXTRA_RE.findall(message))
            app_extras = _app_extras_in(message)
            missing_repo = sorted(documented_extras - repo_extras)
            missing_app = sorted(documented_app_extras - app_extras)
            if missing_repo or missing_app:
                stale.append(
                    f"{example_name}: repo missing {missing_repo or '-'}, "
                    f"app missing {missing_app or '-'}"
                )

    assert not stale, "Example import guards drifted from documented setup: " + "; ".join(stale)


def test_example_import_guards_match_documented_pip_packages() -> None:
    stale: list[str] = []

    for example_name in sorted(_top_level_example_names()):
        path = REPO_ROOT / "examples" / example_name
        packages = _documented_pip_packages(path)
        if not packages:
            continue
        for message in _import_error_system_exit_messages(path):
            if "uv pip install" not in message:
                continue
            missing = sorted(package for package in packages if package not in message)
            if missing:
                stale.append(f"{example_name}: missing {missing}")

    assert not stale, "Example import guards omit documented pip packages: " + "; ".join(stale)


def test_example_repo_sync_commands_include_dev_group() -> None:
    stale: list[str] = []

    for example_name in sorted(_top_level_example_names()):
        path = REPO_ROOT / "examples" / example_name
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        texts = [ast.get_docstring(module) or "", *_import_error_system_exit_messages(path)]
        for line in "\n".join(texts).splitlines():
            if "uv sync " not in line or " --extra " not in line:
                continue
            if "--group dev" not in line:
                stale.append(f"{example_name}: {line.strip()}")

    assert not stale, "Example repo sync commands missing --group dev: " + "; ".join(stale)


def test_pydantic_ai_workflow_voice_example_stays_slim() -> None:
    path = REPO_ROOT / "examples" / "pydantic_ai_workflow_voice.py"
    source = path.read_text(encoding="utf-8")

    assert _visible_code_line_count(path) <= 30
    assert "class SupportWorkflow" in source
    assert "async def on_user_turn" in source
    assert "active_agent_id" in source
    assert "EasyConfig.mic(agent=SupportWorkflow())" in source
    assert "BaseModel" not in source
    assert "RunUsage" not in source
    assert "message_history=" not in source
    assert "output_type=" not in source


def test_push_to_talk_example_uses_runner_lifecycle():
    path = REPO_ROOT / "examples/push_to_talk.py"
    source = path.read_text(encoding="utf-8")

    assert _visible_code_line_count(path) <= 27
    assert "TurnMode.PUSH_TO_TALK" in source
    assert "EasyConfig.mic(" in source
    assert "create_session(config)" in source
    assert "run_stdin_push_to_talk_session(session)" in source
    assert "run_stdin_push_to_talk(session)" not in source
    assert "attach_runtime_feedback" not in source
    assert "asyncio.run(" not in source
    assert "async with create_session(config) as session:" not in source
    assert "LocalTransportConfig" not in source
    assert "require_env" not in source
    assert "await session.start_turn()" not in source
    assert "await session.end_turn()" not in source
    assert "threading.Thread" not in source
    assert "loop.add_reader" not in source
    assert "await session.start()" not in source
    assert "await session.stop(" not in source
    assert "SessionConfig" not in source
    assert "Session(" not in source


def test_custom_transport_example_uses_public_transport_surface():
    path = REPO_ROOT / "examples/custom_transport.py"
    source = path.read_text(encoding="utf-8")

    assert _visible_code_line_count(path) <= 55
    assert "from easycat.transports import LocalTransport" in source
    assert "EasyConfig(" in source
    assert "transport=transport" in source
    assert "run(" in source
    assert "easycat.transports.AudioQueueMixin" in source  # docstring pointer
    assert "docs/extending/transport.md" in source
    assert "create_session" not in source
    assert "SessionConfig" not in source
    assert "Session(" not in source


def test_custom_provider_examples_use_easyconfig_surface():
    for relpath in (
        "examples/custom_stt_provider.py",
        "examples/custom_tts_provider.py",
        "examples/custom_vad_provider.py",
    ):
        path = REPO_ROOT / relpath
        source = path.read_text(encoding="utf-8")

        assert _visible_code_line_count(path) <= 45
        assert "EasyConfig.mic(" in source
        assert "from easycat import EasyConfig, require_env, run" in source
        assert "run(" in source
        assert "create_session" not in source
        assert "attach_runtime_feedback" not in source
        assert "wait_for_shutdown_signal" not in source
        assert "asyncio.run(" not in source
        assert "await session.start()" not in source
        assert "await session.stop()" not in source
        assert "SessionConfig" not in source
        assert "Session(" not in source
        assert "AgentRunner(" not in source


def test_examples_keep_easyconfig_env_first_for_openai_key():
    """Examples preflight OPENAI_API_KEY but let EasyConfig consume the env var."""
    exceptions = {
        # create_app(api_key=...) intentionally supports injection without
        # mutating process env, so this example must pass the key explicitly.
        "twilio_app.py",
    }
    stale: list[str] = []

    for path in sorted((REPO_ROOT / "examples").glob("*.py")):
        if path.name in exceptions:
            continue
        source = path.read_text(encoding="utf-8")
        if "openai_api_key=api_key" in source:
            stale.append(path.name)

    assert not stale, "Examples should let EasyConfig read OPENAI_API_KEY: " + ", ".join(stale)


def test_provider_shortcut_examples_let_easyconfig_read_provider_keys():
    """Provider examples should not duplicate the string shortcut env lookup."""
    stale: list[str] = []
    checks = {
        "cartesia_voice.py": ("CARTESIA_API_KEY",),
        "deepgram_voice.py": ("DEEPGRAM_API_KEY",),
        "elevenlabs_voice.py": ("ELEVENLABS_API_KEY",),
        "combined_providers.py": ("DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"),
    }

    for name, keys in checks.items():
        source = (REPO_ROOT / "examples" / name).read_text(encoding="utf-8")
        for key in keys:
            if f'require_env("{key}")' in source:
                stale.append(f"{name}: {key}")

    assert not stale, "Provider examples should let EasyConfig read provider keys: " + ", ".join(
        stale
    )


def test_pydantic_ai_examples_let_easyconfig_validate_openai_key():
    """PydanticAI slim examples should use EasyConfig's default OpenAI validation."""
    stale: list[str] = []

    for name in (
        "pydantic_ai_voice.py",
        "function_tools_pydantic.py",
        "session_actions_pydantic.py",
        "pydantic_ai_workflow_voice.py",
    ):
        source = (REPO_ROOT / "examples" / name).read_text(encoding="utf-8")
        if 'require_env("OPENAI_API_KEY")' in source:
            stale.append(name)

    assert not stale, "PydanticAI examples should let EasyConfig validate OPENAI_API_KEY: " + (
        ", ".join(stale)
    )


def test_agent_event_subscription_example_uses_run_session():
    path = REPO_ROOT / "examples/agent_event_subscription.py"
    source = path.read_text(encoding="utf-8")

    assert _visible_code_line_count(path) <= 48
    assert "from easycat import" in source
    assert "run_session" in source
    assert "create_session(" in source
    assert "session.subscribe_agent_events(" in source
    assert "session.unsubscribe_handlers(registrations)" in source
    assert "require_env" not in source
    assert "attach_runtime_feedback" not in source
    assert "wait_for_shutdown_signal" not in source
    assert "asyncio.run(" not in source
    assert "await session.start()" not in source
    assert "await session.stop()" not in source


def test_vad_backends_example_uses_easyconfig_provider_config_surface():
    path = REPO_ROOT / "examples/vad_backends.py"
    source = path.read_text(encoding="utf-8")

    assert _visible_code_line_count(path) <= 28
    assert "vad_config = VADConfig(backend=backend)" in source
    assert "probe = create_vad(vad_config)" in source
    assert "EasyConfig.mic(" in source
    assert "vad=vad_config" in source
    assert "vad=vad," not in source
    assert "require_env" not in source
    assert "from easycat import EasyConfig, run" in source
    assert "run(" in source
    assert "create_session" not in source
    assert "attach_runtime_feedback" not in source
    assert "wait_for_shutdown_signal" not in source
    assert "asyncio.run(" not in source
    assert "await session.start()" not in source
    assert "await session.stop()" not in source
    assert "SessionConfig" not in source
    assert "Session(" not in source
    assert "AgentRunner(" not in source
    assert "uv sync --extra quickstart --extra funasr-vad --group dev" in source
    assert "uv sync --extra quickstart --extra ten-vad --group dev" in source
    assert "uv sync --extra silero-vad --group dev" not in source


def test_twilio_voice_app_documents_proxy_signature_settings():
    source = (REPO_ROOT / "examples/voice_app_twilio.py").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "examples" / "README.md").read_text(encoding="utf-8")

    for setting in ("TRUST_PROXY_HEADERS", "TWILIO_PUBLIC_TWIML_URL"):
        assert setting in source
        assert setting in readme


def test_journal_demo_uses_easyconfig_provider_instance_surface():
    path = REPO_ROOT / "examples/journal_demo.py"
    source = path.read_text(encoding="utf-8")

    assert _visible_code_line_count(path) <= 40
    assert "scripted_turn_providers(" in source
    assert "EasyConfig.mic(" in source
    assert 'debug="light"' in source
    assert "turn_taking=TurnManagerConfig" in source
    assert "create_session(config)" in source
    assert "async with create_session(config) as session:" in source
    assert "session = create_session(config)" not in source
    assert "await session.start()" not in source
    assert "await session.stop()" not in source
    assert "SessionConfig" not in source
    assert "Session(" not in source
    assert "InMemoryRingBuffer" not in source
    assert "class Stub" not in source


def test_reconnecting_ws_client_uses_shared_shutdown_and_connect_helpers() -> None:
    path = REPO_ROOT / "examples" / "reconnecting_ws_client.py"
    source = path.read_text(encoding="utf-8")

    assert _visible_code_line_count(path) <= 75
    assert "create_shutdown_event()" in source
    assert "connect_until_stopped(ws, stop)" in source
    assert "add_signal_handler" not in source
    assert "signal.SIGINT" not in source


def test_debug_bundle_example_uses_record_to_auto_capture():
    path = REPO_ROOT / "examples/debug_bundle.py"
    source = path.read_text(encoding="utf-8")

    assert _visible_code_line_count(path) <= 55
    assert "EasyConfig.mic(" in source
    assert "record_to=BUNDLE_DIR" in source
    assert 'debug="light"' in source
    assert "run(" in source
    assert "def _new_bundle_after" in source
    assert 'BUNDLE_DIR.glob("*.zip")' in source
    assert "create_session(" not in source
    assert "attach_runtime_feedback(" not in source
    assert "wait_for_shutdown_signal(" not in source
    assert "asyncio.run(" not in source
    assert "session.export_debug_bundle(" not in source


def test_journal_ui_example_uses_run_session():
    path = REPO_ROOT / "examples/journal_ui.py"
    source = path.read_text(encoding="utf-8")

    assert _visible_code_line_count(path) <= 23
    assert "serve_session(session" in source
    assert "run_session(session)" in source
    assert 'debug="light"' in source
    assert "require_env" not in source
    assert "attach_runtime_feedback" not in source
    assert "wait_for_shutdown_signal" not in source
    assert "asyncio.run(" not in source
    assert "await session.start()" not in source
    assert "await session.stop()" not in source
