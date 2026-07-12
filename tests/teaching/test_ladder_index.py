from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEACHING_DIR = REPO_ROOT / "docs" / "teaching"
_CHAPTER_ROW_RE = re.compile(
    r"^\| (?P<number>\d+) "
    r"\| \[`(?P<name>[^`]+)`\]\(\./(?P<link>[^)]+)/\) "
    r"\| (?P<description>[^|]+) \|$"
)
_API_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9_]*_API_KEY\b")
_UV_EXTRA_RE = re.compile(r"--extra\s+(?P<extra>[A-Za-z0-9_.-]+)")
_ENV_FILE_RUN_HINT = "add `--env-file .env` after `uv run`"


def _chapter_dirs() -> list[Path]:
    return sorted(path for path in TEACHING_DIR.iterdir() if re.match(r"\d{2}-", path.name))


def _ladder_rows() -> list[dict[str, str]]:
    readme = (TEACHING_DIR / "README.md").read_text(encoding="utf-8")
    ladder_section = readme.split("## The ladder", 1)[1]
    rows: list[dict[str, str]] = []
    malformed: list[str] = []

    for line_number, line in enumerate(ladder_section.splitlines(), start=1):
        if not line.startswith("| ") or "](" not in line or "./" not in line:
            continue
        match = _CHAPTER_ROW_RE.match(line)
        if match is None:
            malformed.append(f"line {line_number}: {line}")
            continue
        rows.append(match.groupdict())

    assert not malformed, "Malformed ladder rows in docs/teaching/README.md: " + "; ".join(
        malformed
    )
    return rows


def _chapter_prerequisites(readme: str) -> str:
    if "## Prerequisites" not in readme:
        return readme
    return readme.split("## Prerequisites", 1)[1].split("## ", 1)[0]


def _python_docstring_and_key_literals(path: Path) -> tuple[str, set[str]]:
    source = path.read_text(encoding="utf-8")
    parsed = ast.parse(source, filename=str(path))
    doc = ast.get_docstring(parsed) or ""
    keys: set[str] = set()

    for node in ast.walk(parsed):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            keys.update(_API_KEY_RE.findall(node.value))

    return doc, keys


def _has_env_file_run_hint(text: str) -> bool:
    return _ENV_FILE_RUN_HINT in text.replace("``", "`").lower()


def test_teaching_ladder_index_matches_chapter_directories() -> None:
    chapter_dirs = _chapter_dirs()
    rows = _ladder_rows()

    expected_names = [path.name for path in chapter_dirs]
    row_names = [row["link"] for row in rows]
    row_numbers = [int(row["number"]) for row in rows]

    assert row_names == expected_names
    assert row_numbers == list(range(len(chapter_dirs)))
    assert len(set(row_names)) == len(row_names)

    mismatched_display_names = [
        f"{row['number']}: {row['name']} links to {row['link']}"
        for row in rows
        if row["name"] != row["link"]
    ]
    assert not mismatched_display_names, "; ".join(mismatched_display_names)


def test_public_teaching_ladder_entrypoints_advertise_actual_chapter_count() -> None:
    chapter_count = len(_chapter_dirs())
    expected_phrase = f"{chapter_count}-chapter"
    docs = {
        "README.md": (REPO_ROOT / "README.md").read_text(encoding="utf-8"),
        "docs/teaching/README.md": (TEACHING_DIR / "README.md").read_text(encoding="utf-8"),
    }
    missing = [name for name, text in docs.items() if expected_phrase not in text]

    assert chapter_count == len(_ladder_rows())
    assert not missing, (
        "Teaching ladder entrypoints should advertise the actual chapter count: "
        + ", ".join(missing)
    )


def test_teaching_ladder_index_points_to_docs_and_preflight() -> None:
    readme = (TEACHING_DIR / "README.md").read_text(encoding="utf-8")
    normalized = re.sub(r"\s+", " ", readme)

    assert "uv run easycat docs" in readme
    assert "uv run easycat docs --audience learners" in readme
    assert "uv run easycat docs --audience learners --json" in readme
    assert "maintained docs map" in normalized
    assert "narrow that map to learner-facing routes" in normalized
    assert "automation needs that smaller route map" in normalized
    assert (
        "Coding agent? Use the root [AGENTS.md](../../AGENTS.md) for repository coding rules"
    ) in normalized
    assert "[llms.txt](../../llms.txt) for machine-readable docs route discovery" in normalized
    assert "when a script or coding agent" not in normalized
    assert "uv run easycat explain json-schema" in readme
    assert "uv run easycat doctor" in readme
    assert "uv run easycat doctor --env-file .env" in readme
    assert "uv run easycat doctor --json" in readme
    assert "uv run easycat doctor --env-file .env --json" in readme
    assert _has_env_file_run_hint(normalized)
    assert "first-run environment checks as parseable rows" in normalized
    assert "environment/check rows as parseable output" in normalized
    assert "uv run easycat validate quick" in readme
    assert "uv run easycat validate quick --json" in readme
    assert "uv run easycat validate report .easycat/validation/latest.json" in readme
    assert "uv run easycat validate report .easycat/validation/latest.json --json" in readme
    assert "repository validation lane from the root" in normalized


def test_teaching_ladder_starting_point_table_tracks_chapter_prerequisites() -> None:
    readme = (TEACHING_DIR / "README.md").read_text(encoding="utf-8")
    table = readme.split("## Choose a starting point", 1)[1].split("## The ladder", 1)[0]
    normalized_table = re.sub(r"\s+", " ", table)
    chapters = {
        chapter_dir.name: (chapter_dir / "README.md").read_text(encoding="utf-8")
        for chapter_dir in _chapter_dirs()
    }
    normalized_chapters = {name: re.sub(r"\s+", " ", text) for name, text in chapters.items()}

    for link in (
        "./00-hello-audio/",
        "./01-echo/",
        "./02-transcribe/",
        "./03-parrot-naive/",
        "./10-cleaning-signal/",
        "./11-journal/",
        "./12-evals-and-latency/",
        "./13-swap-providers-and-transports/",
        "./14-bring-your-own-agent/",
        "./15-operate-in-production/",
    ):
        assert f"]({link})" in table
    for phrase in (
        "No mic or API keys",
        "A mic and speakers, but no API keys",
        "`OPENAI_API_KEY`",
        "`OPENAI_API_KEY` and `DEEPGRAM_API_KEY`",
        "Provider or transport comparison work",
        "Production or custom-agent work",
        "checked-in bundles",
        "only optional live-key script",
        "without provider calls",
        "first `RunBundle`",
        "Local/WebRTC/Twilio transports",
        "`SessionManager`",
    ):
        assert phrase in normalized_table

    assert "No API keys needed" in chapters["11-journal"]
    assert "without API keys, without a mic" in normalized_chapters["11-journal"]
    assert "uv sync --extra local --group dev" in chapters["00-hello-audio"]
    assert "sounddevice" in chapters["00-hello-audio"]
    assert "numpy" in chapters["00-hello-audio"]
    assert "A working microphone and speakers" in chapters["00-hello-audio"]
    assert "uv sync --extra local --group dev" in chapters["01-echo"]
    assert "uv sync --extra quickstart --group dev" not in _chapter_prerequisites(
        chapters["01-echo"]
    )
    assert "A mic and speakers" in chapters["01-echo"]
    assert "OPENAI_API_KEY" in chapters["02-transcribe"]
    assert "or any other provider" in _chapter_prerequisites(chapters["02-transcribe"])
    for chapter in (
        "03-parrot-naive",
        "04-vad-preroll",
        "05-blocking-agent",
        "06-streaming-agent",
        "07-tools",
        "08-smart-turn",
        "09-interruption",
        "10-cleaning-signal",
    ):
        prerequisites = _chapter_prerequisites(chapters[chapter])
        assert "OPENAI_API_KEY" in prerequisites
        assert "DEEPGRAM_API_KEY" in prerequisites
    assert "WebRTC" in chapters["13-swap-providers-and-transports"]
    assert "Twilio" in chapters["13-swap-providers-and-transports"]
    assert "SessionManager" in chapters["15-operate-in-production"]


def test_teaching_chapters_have_reader_entrypoints() -> None:
    missing: list[str] = []

    for chapter_dir in _chapter_dirs():
        if not (chapter_dir / "README.md").exists():
            missing.append(f"{chapter_dir.name}: README.md")
        if not (chapter_dir / "EXERCISES.md").exists():
            missing.append(f"{chapter_dir.name}: EXERCISES.md")
        if not list(chapter_dir.glob("*.py")):
            missing.append(f"{chapter_dir.name}: runnable script")

    assert not missing, "Teaching chapters missing reader entrypoints: " + ", ".join(missing)


def test_teaching_exercise_pages_link_back_to_the_chapter_and_ladder() -> None:
    stale: list[str] = []

    for chapter_dir in _chapter_dirs():
        exercises = (chapter_dir / "EXERCISES.md").read_text(encoding="utf-8")
        if "[← Back to chapter](./README.md)" not in exercises:
            stale.append(f"{chapter_dir.name}: chapter link")
        if "[Ladder index](../)" not in exercises:
            stale.append(f"{chapter_dir.name}: ladder link")

    assert not stale, "Teaching exercise navigation is incomplete: " + ", ".join(stale)


def test_teaching_chapter_readmes_include_runnable_commands() -> None:
    missing: list[str] = []

    for chapter_dir in _chapter_dirs():
        readme = (chapter_dir / "README.md").read_text(encoding="utf-8")
        command_prefix = f"uv run python docs/teaching/{chapter_dir.name}/"
        if command_prefix not in readme:
            missing.append(chapter_dir.name)

    assert not missing, "Teaching chapter READMEs missing runnable commands: " + ", ".join(missing)


def test_teaching_script_run_commands_are_documented_in_chapter_docs() -> None:
    stale: list[str] = []

    for chapter_dir in _chapter_dirs():
        docs = "\n".join(
            (chapter_dir / filename).read_text(encoding="utf-8")
            for filename in ("README.md", "EXERCISES.md")
            if (chapter_dir / filename).exists()
        )

        for script in sorted(chapter_dir.glob("*.py")):
            doc, _ = _python_docstring_and_key_literals(script)
            command = f"uv run python docs/teaching/{chapter_dir.name}/{script.name}"
            if command in doc and command not in docs:
                stale.append(f"{chapter_dir.name}/{script.name}: missing `{command}`")

    assert not stale, "Teaching script run commands missing from chapter docs: " + "; ".join(stale)


def test_teaching_chapter_prerequisites_cover_script_setup() -> None:
    stale: list[str] = []

    for chapter_dir in _chapter_dirs():
        readme = (chapter_dir / "README.md").read_text(encoding="utf-8")
        prerequisites = _chapter_prerequisites(readme)
        readme_extras = set(_UV_EXTRA_RE.findall(prerequisites))
        readme_keys = set(_API_KEY_RE.findall(prerequisites))
        script_extras: set[str] = set()
        script_keys: set[str] = set()

        for script in sorted(chapter_dir.glob("*.py")):
            doc, literal_keys = _python_docstring_and_key_literals(script)
            script_extras.update(_UV_EXTRA_RE.findall(doc))
            script_keys.update(_API_KEY_RE.findall(doc))
            script_keys.update(literal_keys)

        missing_extras = sorted(script_extras - readme_extras)
        missing_keys = sorted(script_keys - readme_keys)
        if missing_extras or missing_keys:
            stale.append(
                f"{chapter_dir.name}: missing extras {missing_extras or '-'}, "
                f"keys {missing_keys or '-'}"
            )

    assert not stale, "Teaching chapter prerequisites drifted from scripts: " + "; ".join(stale)


def test_teaching_chapter_key_prerequisites_document_env_file_doctor() -> None:
    stale: list[str] = []

    for chapter_dir in _chapter_dirs():
        readme = (chapter_dir / "README.md").read_text(encoding="utf-8")
        prerequisites = _chapter_prerequisites(readme)
        if not _API_KEY_RE.search(prerequisites):
            continue
        if "uv run easycat doctor" not in prerequisites:
            continue
        if "uv run easycat doctor --env-file .env" not in prerequisites:
            stale.append(chapter_dir.name)

    assert not stale, (
        "Teaching chapter key prerequisites missing .env doctor preflight: " + ", ".join(stale)
    )


def test_teaching_chapter_key_prerequisites_document_env_file_runtime() -> None:
    stale: list[str] = []

    for chapter_dir in _chapter_dirs():
        readme = (chapter_dir / "README.md").read_text(encoding="utf-8")
        prerequisites = _chapter_prerequisites(readme)
        if not _API_KEY_RE.search(prerequisites):
            continue
        if not _has_env_file_run_hint(prerequisites):
            stale.append(chapter_dir.name)

    assert not stale, "Teaching chapter key prerequisites missing .env runtime hint: " + ", ".join(
        stale
    )


def test_teaching_script_key_docstrings_run_doctor() -> None:
    missing_doctor: list[str] = []
    missing_env_file: list[str] = []
    missing_env_run: list[str] = []

    for chapter_dir in _chapter_dirs():
        for script in sorted(chapter_dir.glob("*.py")):
            doc, _ = _python_docstring_and_key_literals(script)
            if not _API_KEY_RE.search(doc):
                continue
            path = script.relative_to(REPO_ROOT).as_posix()
            if "uv run easycat doctor" not in doc:
                missing_doctor.append(path)
            if "uv run easycat doctor --env-file .env" not in doc:
                missing_env_file.append(path)
            if not _has_env_file_run_hint(doc):
                missing_env_run.append(path)

    assert not missing_doctor, "Teaching script key setup missing doctor preflight: " + ", ".join(
        missing_doctor
    )
    assert not missing_env_file, (
        "Teaching script key setup missing .env doctor preflight: " + ", ".join(missing_env_file)
    )
    assert not missing_env_run, (
        "Teaching script key setup missing .env runtime hint: " + ", ".join(missing_env_run)
    )


def test_chapter_13_provider_mix_documents_required_extras() -> None:
    readme = (TEACHING_DIR / "13-swap-providers-and-transports" / "README.md").read_text(
        encoding="utf-8"
    )

    assert "--extra deepgram" in readme
    assert "--extra elevenlabs" in readme
    assert "--extra webrtc" in readme
    assert "--extra telephony" in readme


def test_chapter_15_teaches_public_session_lifecycle() -> None:
    chapter_dir = TEACHING_DIR / "15-operate-in-production"
    files = {
        "README.md": (chapter_dir / "README.md").read_text(encoding="utf-8"),
        "EXERCISES.md": (chapter_dir / "EXERCISES.md").read_text(encoding="utf-8"),
        "main.py": (chapter_dir / "main.py").read_text(encoding="utf-8"),
    }
    stale_public_calls: list[str] = []

    for name, text in files.items():
        for term in (
            "session.shutdown()",
            "session.close()",
            "session.destroy()",
            "four lifecycle methods",
        ):
            if term in text:
                stale_public_calls.append(f"{name}: {term}")

    assert not stale_public_calls, "Stale public lifecycle calls: " + ", ".join(stale_public_calls)

    readme = files["README.md"]
    assert "async with session:" in readme
    assert "await session.stop()" in readme
    assert "await session.stop(force=True)" in readme


def test_chapter_15_cli_section_lists_registered_commands() -> None:
    from typer.main import get_command

    from easycat.cli import _app
    from easycat.cli.debug.bundles import bundles_app
    from easycat.cli.validate import validate_app

    _app._register_commands()
    readme = (TEACHING_DIR / "15-operate-in-production" / "README.md").read_text(encoding="utf-8")
    cli_section = readme.split("## The `easycat` CLI", 1)[1].split("## ", 1)[0]

    assert "uv run easycat" in cli_section
    assert "drop the `uv run` prefix" in cli_section
    assert "uv run easycat --help" not in cli_section
    for section, command_names in _app._JOURNEY_SECTIONS:
        assert section in cli_section
        for command_name in command_names:
            assert re.search(rf"^\s+{re.escape(command_name)}\s+", cli_section, re.M)
            assert _app._COMMAND_TEXT[command_name].journey in cli_section
    for command, purpose in _app._CLI_HINTS:
        assert f"Run {command} for {purpose}" in cli_section
    assert "doctor    check environment + provider reachability" not in cli_section
    assert "docs      show documentation entry points" not in cli_section
    assert "Inspect a debug bundle or SQLite journal" not in cli_section
    assert "Inspect captured debug bundles and crash dumps" not in cli_section
    assert "`uv run easycat docs`" in cli_section
    assert "`uv run easycat docs --json`" in cli_section
    assert "`uv run easycat init --list-templates`" in cli_section
    assert "`uv run easycat init --list-templates --json`" in cli_section
    normalized_cli_section = re.sub(r"\s+", " ", cli_section)
    assert "base `easycat[...]` package requirements and extras" in normalized_cli_section
    assert "required environment variables" in normalized_cli_section
    assert "optional environment knobs" in normalized_cli_section
    assert "generated files" in normalized_cli_section
    assert "copyable create/preflight/check/fix/docs/json-schema/run commands" in (
        normalized_cli_section
    )
    assert "same template catalog and post-scaffold command previews" in normalized_cli_section
    assert "same route map with command hints" in normalized_cli_section
    assert "audience labels" in normalized_cli_section
    assert "`uv run easycat docs --audience operators`" in cli_section
    assert "`uv run easycat docs --audience operators --json`" in cli_section
    assert "production and observability route set" in normalized_cli_section
    assert "parseable operator-facing routes" in normalized_cli_section
    assert "architecture and maintenance guides" in normalized_cli_section
    assert (
        "Replace uppercase or angle-bracket placeholders such as `PATH` or `<session_id>` "
        "before running those hints"
    ) in normalized_cli_section
    assert (
        "Coding agent? Use the root [AGENTS.md](../../../AGENTS.md) for repository coding rules"
    ) in normalized_cli_section
    assert "[llms.txt](../../../llms.txt) for machine-readable docs route discovery" in (
        normalized_cli_section
    )
    assert "when a script or coding agent" not in normalized_cli_section
    assert "`uv run easycat doctor --env-file .env`" in cli_section
    assert "`uv run easycat doctor --json`" in cli_section
    assert "parseable first-run environment checks" in normalized_cli_section
    assert "`uv run easycat explain json-schema`" in cli_section
    assert "standard `--json` envelope" in cli_section
    assert "command-specific success and error fields" in normalized_cli_section
    assert "validate quick --json" in cli_section
    assert "current quick validation run inside the standard CLI envelope" in (
        normalized_cli_section
    )
    assert "validate contracts --json" in cli_section
    assert "parseable contract run" in normalized_cli_section
    assert "validate release --json" in cli_section
    assert "installed-wheel validation result inside the standard CLI envelope" in (
        normalized_cli_section
    )
    assert "validate report .easycat/validation/latest.json" in cli_section
    assert "validate report .easycat/validation/latest.json --json" in cli_section
    assert "re-emits the saved report inside that same envelope" in normalized_cli_section
    assert ".easycat/validation/runs/<run_id>/report.json" in cli_section
    assert "validate report <path>" not in cli_section
    assert "Add `--json` when a coding agent or script" not in normalized_cli_section

    top_level_commands = {command.name for command in _app.app.registered_commands}
    top_level_commands.update(group.name for group in _app.app.registered_groups)
    top_level_commands.discard(None)

    missing_top_level = sorted(
        command_name
        for command_name in top_level_commands
        if f"easycat {command_name}" not in cli_section
    )
    assert not missing_top_level, "Chapter 15 CLI section missing commands: " + ", ".join(
        missing_top_level
    )

    missing_bundle_commands = sorted(
        command.name
        for command in bundles_app.registered_commands
        if command.name is not None and f"easycat bundles {command.name}" not in cli_section
    )
    assert not missing_bundle_commands, "Chapter 15 CLI section missing bundles commands: " + (
        ", ".join(missing_bundle_commands)
    )

    missing_validate_commands = sorted(
        command_name
        for command_name in get_command(validate_app).commands
        if f"uv run easycat validate {command_name}" not in cli_section
    )
    assert not missing_validate_commands, "Chapter 15 CLI section missing validate commands: " + (
        ", ".join(missing_validate_commands)
    )

    validate_commands = set(get_command(validate_app).commands)
    advertised_validate_commands = set(
        re.findall(r"uv run easycat validate (?P<command>[a-z][a-z0-9-]*)(?:\s|$)", cli_section)
    )
    stale_validate_commands = sorted(advertised_validate_commands - validate_commands)
    assert not stale_validate_commands, (
        "Chapter 15 CLI section advertises stale validate commands: "
        + ", ".join(stale_validate_commands)
    )


def test_chapter_15_doctor_exercise_uses_repo_local_command() -> None:
    chapter_dir = TEACHING_DIR / "15-operate-in-production"
    files = {
        "README.md": (chapter_dir / "README.md").read_text(encoding="utf-8"),
        "EXERCISES.md": (chapter_dir / "EXERCISES.md").read_text(encoding="utf-8"),
    }
    stale_mentions = [name for name, text in files.items() if "`easycat doctor`" in text]

    assert not stale_mentions, "Chapter 15 doctor exercise should use uv run: " + ", ".join(
        stale_mentions
    )
    for text in files.values():
        assert "`uv run easycat doctor`" in text


def test_teaching_cli_error_code_examples_use_current_namespace() -> None:
    from easycat.errors import REGISTRY

    stale_mentions: list[str] = []
    unknown_mentions: list[str] = []

    for path in sorted(TEACHING_DIR.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"\bEC-[A-Z0-9-]+\b", text):
            stale_mentions.append(path.relative_to(REPO_ROOT).as_posix())
        for match in re.finditer(r"\bEASYCAT_E\d{3}\b", text):
            code = match.group(0)
            if code not in REGISTRY:
                line = text.count("\n", 0, match.start()) + 1
                unknown_mentions.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{line}: {code}")

    assert not stale_mentions, "Teaching docs use legacy EC-* error codes: " + ", ".join(
        stale_mentions
    )
    assert not unknown_mentions, "Teaching docs reference unknown EasyCat errors: " + ", ".join(
        unknown_mentions
    )


def test_teaching_docs_do_not_claim_teaching_tests_are_missing() -> None:
    stale_mentions: list[str] = []

    for chapter_dir in _chapter_dirs():
        for filename in ("README.md", "EXERCISES.md"):
            path = chapter_dir / filename
            text = path.read_text(encoding="utf-8")
            if "`tests/teaching/` doesn't exist yet" in text:
                stale_mentions.append(path.relative_to(REPO_ROOT).as_posix())

    assert not stale_mentions, "Teaching docs claim tests/teaching/ is missing: " + ", ".join(
        stale_mentions
    )
