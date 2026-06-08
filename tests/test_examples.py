from __future__ import annotations

import ast
import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from websockets.datastructures import Headers

from easycat import EasyConfig, WebSocketTransportConfig, create_session
from tests._command_hints import command_hint_problems, documented_commands

REPO_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE_README_ROW_RE = re.compile(
    r"^\| \[(?P<name>[^\]]+\.py)\]\((?P<link>[^)]+\.py)\) "
    r"\| (?P<use_when>[^|]+) "
    r"\| `(?P<run>[^`]+)` "
    r"\| (?P<install>[^|]+) "
    r"\| (?P<env>[^|]+) \|$"
)
_SUPPORT_FILE_LINK_RE = re.compile(
    r"^- \[(?P<name>[^]]+\.(?:conf|html|service|sh))\]"
    r"\((?P<link>[^)]+\.(?:conf|html|service|sh))\):"
)
_EASYCAT_EXTRA_RE = re.compile(r"easycat\[(?P<extras>[^\]]+)\]")
_UV_PIP_INSTALL_RE = re.compile(r"uv pip install\s+(?P<args>[^\n`|]+)")
_UV_SYNC_EXTRA_RE = re.compile(r"--extra\s+(?P<extra>[A-Za-z0-9_.-]+)")


def _top_level_example_names() -> set[str]:
    return {
        path.name for path in (REPO_ROOT / "examples").glob("*.py") if path.name != "__init__.py"
    }


def _documented_support_file_links() -> set[str]:
    readme = (REPO_ROOT / "examples" / "README.md").read_text(encoding="utf-8")
    links: set[str] = set()

    for line in readme.splitlines():
        match = _SUPPORT_FILE_LINK_RE.match(line)
        if match is not None:
            assert match.group("name") == match.group("link")
            links.add(match.group("link"))

    return links


def _support_files_to_document() -> set[str]:
    examples_dir = REPO_ROOT / "examples"
    files = set(examples_dir.glob("*.html"))
    files.update((examples_dir / "webrtc_static").glob("*.html"))
    files.update((examples_dir / "ec2_webrtc").glob("*.conf"))
    files.update((examples_dir / "ec2_webrtc").glob("*.service"))
    files.add(examples_dir / "ec2_webrtc" / "deploy.sh")
    return {path.relative_to(examples_dir).as_posix() for path in files if path.exists()}


def _example_readme_rows() -> list[dict[str, str]]:
    readme = (REPO_ROOT / "examples" / "README.md").read_text(encoding="utf-8")
    rows: list[dict[str, str]] = []
    malformed: list[str] = []

    for line_number, line in enumerate(readme.splitlines(), start=1):
        if not line.startswith("| [") or ".py]" not in line:
            continue
        match = _EXAMPLE_README_ROW_RE.match(line)
        if match is None:
            malformed.append(f"line {line_number}: {line}")
            continue
        rows.append(match.groupdict())

    assert not malformed, "Malformed example rows in examples/README.md: " + "; ".join(malformed)
    return rows


def _example_run_command_problems(row: dict[str, str]) -> list[str]:
    link = row["link"]
    tokens = shlex.split(row["run"])
    problems: list[str] = []

    if len(tokens) < 3 or tokens[:2] != ["uv", "run"]:
        return [f"{link}: run command is not a uv run command"]

    if tokens[2] == "python":
        if len(tokens) < 4:
            return [f"{link}: run command is missing a Python script"]
        script = tokens[3]
        script_path = REPO_ROOT / script
        if not script_path.exists():
            problems.append(f"{link}: run command script {script} does not exist")
        if script_path.parent != REPO_ROOT / "examples" or script_path.name != link:
            problems.append(f"{link}: run command script {script} does not match linked example")
        return problems

    if tokens[2] == "uvicorn":
        if len(tokens) < 4:
            return [f"{link}: run command is missing an ASGI target"]
        target = tokens[3]
        module_name = target.partition(":")[0]
        if not module_name:
            return [f"{link}: run command has an empty ASGI module target"]
        module_path = REPO_ROOT / Path(*module_name.split(".")).with_suffix(".py")
        if not module_path.exists():
            problems.append(f"{link}: run command ASGI target {target} does not exist")
        if module_path.parent != REPO_ROOT / "examples" or module_path.name != link:
            problems.append(
                f"{link}: run command ASGI target {target} does not match linked example"
            )
        return problems

    return [f"{link}: unsupported run command executable {tokens[2]}"]


def _is_module_docstring_node(module: ast.Module, node: ast.stmt) -> bool:
    return bool(
        module.body
        and module.body[0] is node
        and isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _is_import_error_handler(handler: ast.ExceptHandler) -> bool:
    if isinstance(handler.type, ast.Name):
        return handler.type.id == "ImportError"
    return False


def _is_import_guard_try(node: ast.stmt) -> bool:
    return isinstance(node, ast.Try) and any(
        _is_import_error_handler(handler) for handler in node.handlers
    )


def _visible_code_line_count(path: Path) -> int:
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source, filename=str(path))
    source_lines = source.splitlines()
    line_count = 0

    for node in module.body:
        if _is_module_docstring_node(module, node) or _is_import_guard_try(node):
            continue
        assert node.end_lineno is not None
        line_count += sum(
            1 for index in range(node.lineno - 1, node.end_lineno) if source_lines[index].strip()
        )

    return line_count


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_string(node.left)
        right = _literal_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _import_error_system_exit_messages(path: Path) -> list[str]:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    messages: list[str] = []

    for handler in (node for node in ast.walk(module) if isinstance(node, ast.ExceptHandler)):
        if not _is_import_error_handler(handler):
            continue
        for node in ast.walk(handler):
            if not isinstance(node, ast.Raise):
                continue
            call = node.exc
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "SystemExit"
                and call.args
            ):
                message = _literal_string(call.args[0])
                if message is not None:
                    messages.append(message)

    return messages


def _documented_setup_extras(path: Path) -> set[str]:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    doc = ast.get_docstring(module) or ""
    return set(_UV_SYNC_EXTRA_RE.findall(doc))


def _uses_default_openai_providers(path: Path) -> bool:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            is_easy_config_call = node.func.id == "EasyConfig"
        elif isinstance(node.func, ast.Attribute):
            is_easy_config_call = (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "EasyConfig"
                and node.func.attr in {"browser", "mic", "phone"}
            )
        else:
            is_easy_config_call = False

        if not is_easy_config_call:
            continue

        provider_overrides = {kw.arg for kw in node.keywords if kw.arg in {"stt", "tts"}}
        if provider_overrides != {"stt", "tts"}:
            return True

    return False


def _dockerfile_default_extras() -> set[str]:
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(r'^ARG EXTRAS="(?P<extras>[^"]+)"$', dockerfile, re.MULTILINE)
    assert match is not None, "docker/Dockerfile must declare ARG EXTRAS"
    return set(_UV_SYNC_EXTRA_RE.findall(match.group("extras")))


def _docker_build_arg_extra_sets(*texts: str) -> list[set[str]]:
    return [
        set(_UV_SYNC_EXTRA_RE.findall(match.group("extras")))
        for text in texts
        for match in re.finditer(r'--build-arg EXTRAS="(?P<extras>[^"]+)"', text)
    ]


def _pip_install_packages_in(text: str) -> set[str]:
    packages: set[str] = set()

    for match in _UV_PIP_INSTALL_RE.finditer(text):
        args = match.group("args").split("#", 1)[0].strip()
        for token in shlex.split(args):
            if token.startswith("-"):
                continue
            packages.add(token)

    return packages


def _documented_pip_packages(path: Path) -> set[str]:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    doc = ast.get_docstring(module) or ""
    return _pip_install_packages_in(doc)


def _readme_install_packages(install: str) -> set[str]:
    packages = _pip_install_packages_in(install)

    for snippet in re.findall(r"`([^`]+)`", install):
        snippet = snippet.strip()
        if "uv pip install" in snippet:
            packages.update(_pip_install_packages_in(snippet))
            continue
        if snippet.startswith(("uv ", "--")):
            continue
        for token in shlex.split(snippet):
            package = token.strip(",.;")
            if package and not package.startswith("-"):
                packages.add(package)

    return packages


def _app_extras_in(message: str) -> set[str]:
    extras: set[str] = set()
    for match in _EASYCAT_EXTRA_RE.finditer(message):
        extras.update(part.strip() for part in match.group("extras").split(",") if part.strip())
    return extras


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _env_var_literal_from_call(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        if func.id not in {"require_env", "_env_flag"}:
            return None
    elif isinstance(func, ast.Attribute):
        if func.attr == "getenv":
            if not isinstance(func.value, ast.Name) or func.value.id != "os":
                return None
        elif func.attr == "get":
            if not _is_os_environ(func.value):
                return None
        else:
            return None
    else:
        return None

    if node.args:
        return _literal_string(node.args[0])
    return None


def _referenced_env_vars(path: Path) -> set[str]:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()

    for node in ast.walk(module):
        if isinstance(node, ast.Call):
            name = _env_var_literal_from_call(node)
            if name is not None:
                names.add(name)
        elif isinstance(node, ast.Subscript) and _is_os_environ(node.value):
            name = _literal_string(node.slice)
            if name is not None:
                names.add(name)

    return names


class _DummyAgent:
    async def run(self, text: str) -> str:
        return text


def _load_slim_example(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    *,
    framework: str | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Import a slim example that runs ``easycat.run(...)`` at module scope.

    Stubs ``easycat.run`` so importing doesn't block on a real session,
    sets any env vars the example consumes at module scope (via
    ``require_env`` or EasyConfig string shortcuts), and evicts any cached
    copy so the fresh import sees the monkeypatched ``run``.
    """
    if framework:
        pytest.importorskip(framework)

    import easycat

    monkeypatch.setattr(easycat, "run", lambda config: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)

    sys.modules.pop(module_name, None)
    __import__(module_name)


# ── Slim examples (module-scope ``easycat.run(...)``) ────────────────


def test_openai_agents_voice_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.openai_agents_voice", framework="agents")


@pytest.mark.parametrize(
    ("example_name", "budget"),
    [
        ("openai_agents_voice.py", 7),
        ("pydantic_ai_voice.py", 8),
        ("ws_server.py", 15),
    ],
)
def test_canonical_local_voice_examples_keep_visible_code_budget(
    example_name: str,
    budget: int,
) -> None:
    path = REPO_ROOT / "examples" / example_name

    assert _visible_code_line_count(path) <= budget


def test_top_level_examples_do_not_alias_easycat_imports() -> None:
    aliased: list[str] = []

    for example_name in sorted(_top_level_example_names()):
        path = REPO_ROOT / "examples" / example_name
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname and alias.name.startswith("easycat"):
                        aliased.append(f"{example_name}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module == "easycat":
                for alias in node.names:
                    if alias.asname:
                        aliased.append(
                            f"{example_name}:{node.lineno}: from easycat import {alias.name}"
                        )

    assert not aliased, "Top-level examples should teach EasyCat names without aliases: " + (
        "; ".join(aliased)
    )


def test_examples_readme_lists_every_top_level_python_example() -> None:
    row_names = {row["link"] for row in _example_readme_rows()}
    missing = sorted(_top_level_example_names() - row_names)

    assert not missing, "examples/README.md missing example rows for: " + ", ".join(missing)


def test_examples_readme_lists_browser_and_deploy_support_files() -> None:
    documented = _documented_support_file_links()
    expected = _support_files_to_document()

    missing = sorted(expected - documented)
    stale = sorted(documented - expected)

    assert not missing, "examples/README.md missing support-file links for: " + ", ".join(missing)
    assert not stale, "examples/README.md has stale support-file links for: " + ", ".join(stale)


def test_ec2_webrtc_deploy_docs_do_not_claim_to_configure_https() -> None:
    server = (REPO_ROOT / "examples" / "webrtc_server.py").read_text(encoding="utf-8")
    deploy = (REPO_ROOT / "examples" / "ec2_webrtc" / "deploy.sh").read_text(encoding="utf-8")

    assert "configures HTTPS" not in server
    assert "behind an HTTPS reverse proxy" in server
    assert "Backend HTTP URL: http://$EXTERNAL_IP:8080/webrtc_client.html" in deploy
    assert "Browser URL:      https://<your-domain>/webrtc_client.html" in deploy
    assert (
        "Signaling URL:    https://<your-domain>                     (after TLS proxy)" in deploy
    )
    assert "Signaling URL:    https://<your-domain>/offer" not in deploy
    assert "Client URL:      http://$EXTERNAL_IP:8080/webrtc_client.html" not in deploy


def test_ec2_webrtc_deploy_keeps_browser_turn_credentials_opt_in() -> None:
    deploy = (REPO_ROOT / "examples" / "ec2_webrtc" / "deploy.sh").read_text(encoding="utf-8")

    assert "WEBRTC_EXPOSE_ICE_CREDENTIALS=0" in deploy
    assert "Browser TURN auth remains hidden from /config by default." in deploy
    assert "trusted demos or short-lived TURN creds" in deploy


def test_ec2_webrtc_turns_port_is_optional_until_certs_are_configured() -> None:
    deploy = (REPO_ROOT / "examples" / "ec2_webrtc" / "deploy.sh").read_text(encoding="utf-8")
    coturn = (REPO_ROOT / "examples" / "ec2_webrtc" / "coturn.conf").read_text(encoding="utf-8")

    assert "TURN_SERVER_URL=turn:$EXTERNAL_IP:3478" in deploy
    assert "TCP 8080, TCP/UDP 3478, UDP 49152-65535" in deploy
    assert "Optional TURNS: TCP 5349 after coturn cert/pkey are configured" in deploy
    assert "TCP 5349   — TURNS" not in deploy
    assert "# tls-listening-port=5349" in coturn
    assert "\ntls-listening-port=5349" not in coturn


def test_ec2_webrtc_turn_template_handles_generated_password_characters() -> None:
    deploy = (REPO_ROOT / "examples" / "ec2_webrtc" / "deploy.sh").read_text(encoding="utf-8")

    assert "openssl rand -base64 24" in deploy
    assert 'sed -i "s/__TURN_PASSWORD__/$TURN_PASSWORD/"' not in deploy
    assert 'sed -i "s/__EXTERNAL_IP__/$EXTERNAL_IP/"' not in deploy
    assert "<<'PY' | sudo tee /etc/turnserver.conf" in deploy
    assert '.replace("__TURN_PASSWORD__", sys.argv[3])' in deploy


def test_ec2_webrtc_deploy_enables_coturn_across_default_variants() -> None:
    deploy = (REPO_ROOT / "examples" / "ec2_webrtc" / "deploy.sh").read_text(encoding="utf-8")

    assert "grep -Eq '^#?TURNSERVER_ENABLED='" in deploy
    assert "s/^#?TURNSERVER_ENABLED=.*/TURNSERVER_ENABLED=1/" in deploy
    assert "tee -a /etc/default/coturn" in deploy
    assert "s/#TURNSERVER_ENABLED=1/TURNSERVER_ENABLED=1/" not in deploy


def test_ec2_webrtc_deploy_honors_manual_external_ip() -> None:
    deploy = (REPO_ROOT / "examples" / "ec2_webrtc" / "deploy.sh").read_text(encoding="utf-8")

    assert 'EXTERNAL_IP="${EXTERNAL_IP:-$(detect_external_ip)}"' in deploy
    assert "--max-time 2" in deploy
    assert "export EXTERNAL_IP=1.2.3.4" in deploy
    assert "EXTERNAL_IP=$(curl" not in deploy


def test_ec2_webrtc_deploy_detects_external_ip_with_imdsv2_first() -> None:
    deploy = (REPO_ROOT / "examples" / "ec2_webrtc" / "deploy.sh").read_text(encoding="utf-8")

    assert "detect_external_ip()" in deploy
    assert "latest/api/token" in deploy
    assert "X-aws-ec2-metadata-token-ttl-seconds: 21600" in deploy
    assert "X-aws-ec2-metadata-token: $token" in deploy
    assert deploy.count("latest/meta-data/public-ipv4") == 2


def test_ec2_webrtc_deploy_does_not_copy_local_secret_or_cache_dirs() -> None:
    deploy = (REPO_ROOT / "examples" / "ec2_webrtc" / "deploy.sh").read_text(encoding="utf-8")

    assert 'sudo cp -a "$REPO_ROOT/."' not in deploy
    assert 'tar -C "$REPO_ROOT"' in deploy
    for excluded in (
        "./.agents",
        "./.claude",
        "./.codex",
        "./.coverage",
        "./.coverage.*",
        "./.easycat",
        "./.env",
        "./.env.*",
        "./.git",
        "./.hypothesis",
        "./.mypy_cache",
        "./.mutmut-cache",
        "./.pipecat-bench",
        "./.pytest_cache",
        "./.ruff_cache",
        "./.uv-cache",
        "./.venv",
        "./coverage.xml",
        "./htmlcov",
        "./mutants",
        "./site",
        "__pycache__",
        "*.key",
        "*.pem",
        "*.pyc",
        "*.pyo",
    ):
        assert f"--exclude='{excluded}'" in deploy


def test_examples_readme_fastest_path_verifies_environment_before_running() -> None:
    readme = (REPO_ROOT / "examples" / "README.md").read_text(encoding="utf-8")
    intro = readme.split("For the fastest local mic/speaker path:", 1)[0]
    normalized_intro = re.sub(r"\s+", " ", intro)
    fast_path = readme.split("For the fastest local mic/speaker path:", 1)[1]
    commands = fast_path.split("```bash", 1)[1].split("```", 1)[0].strip().splitlines()

    assert "uv run easycat docs" in intro
    assert "uv run easycat docs --json" in intro
    assert "maintained docs map" in intro
    assert "script or coding agent needs the same route map with command hints" in normalized_intro
    assert (
        "Replace uppercase or angle-bracket placeholders such as `PATH` or `<session_id>` "
        "before running those hints"
    ) in normalized_intro
    assert "uv run easycat explain json-schema" in intro
    assert "JSON envelope and field contract" in normalized_intro
    assert "uv run easycat doctor --json" in intro
    assert "uv run easycat doctor --env-file .env --json" in intro
    assert "script or coding agent needs parseable first-run environment checks" in (
        normalized_intro
    )
    assert "uv run easycat init --list-templates" in intro
    assert "uv run easycat init my-agent" in intro
    assert "uv run easycat init --list-templates --json" in intro
    assert "same template catalog" in normalized_intro
    assert "copyable create/preflight/check/fix/docs/json-schema/run commands" in intro
    assert "browser WebRTC" in normalized_intro
    assert "Twilio" in intro
    assert commands == [
        "uv sync --extra quickstart --group dev",
        'export OPENAI_API_KEY="your-api-key"',
        "uv run easycat doctor",
        "uv run python examples/openai_agents_voice.py",
    ]
    assert "uv run easycat doctor --env-file .env" in fast_path
    assert "uv run --env-file .env python examples/openai_agents_voice.py" in fast_path
    assert "After changing an example or using one as a starting point" in fast_path
    assert "uv run easycat validate quick" in fast_path
    assert "uv run easycat validate quick --json" in fast_path
    assert "uv run easycat validate report .easycat/validation/latest.json" in fast_path
    assert "uv run easycat validate report .easycat/validation/latest.json --json" in fast_path
    normalized_fast_path = re.sub(r"\s+", " ", fast_path)
    current_run_phrase = (
        "script or coding agent needs the current quick validation run inside "
        "the standard CLI envelope"
    )
    assert current_run_phrase in normalized_fast_path
    assert "re-emit the saved report in that same envelope" in normalized_fast_path


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
    normalized_table = re.sub(r"\s+", " ", table)
    rows = {row["link"]: row for row in _example_readme_rows()}
    linked_examples = set(re.findall(r"\[([^]]+\.py)\]\(([^)]+\.py)\)", table))
    linked_paths = {link for display, link in linked_examples}

    assert linked_paths <= set(rows), "Chooser links missing from example matrix"
    for display, link in linked_examples:
        assert display == link
    for phrase in (
        "No API keys",
        "First local voice bot",
        "Your preferred agent framework",
        "Browser or server transport",
        "Provider comparison",
        "Debugging and replay",
        "`quickstart` extra",
        "framework-specific bridge wiring",
        "browser/WebSocket/WebRTC surfaces",
        "provider extras and required API keys",
        "`RunBundle` export",
        "debugger UI",
    ):
        assert phrase in normalized_table
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
        "`uv sync --extra quickstart --group dev`; `langchain<1`, `langchain-openai`, "
        "`--extra ten-vad`, or `uv pip install krisp_audio` for optional backends"
    )

    assert _readme_install_packages(install) == {
        "krisp_audio",
        "langchain-openai",
        "langchain<1",
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
        "\n".join(
            [
                "import os",
                'require_env("REQUIRED_API_KEY")',
                'os.getenv("OPTIONAL_TOKEN")',
                'os.environ.get("OPTIONAL_HOST")',
                'os.environ["DIRECT_SECRET"]',
                '_env_flag("FEATURE_FLAG")',
            ]
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


def test_pydantic_ai_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.pydantic_ai_voice", framework="pydantic_ai")


def test_function_tools_openai_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.function_tools_openai", framework="agents")


def test_function_tools_pydantic_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.function_tools_pydantic", framework="pydantic_ai")


def test_smart_turn_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.smart_turn_demo", framework="agents")


def test_echo_cancellation_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.echo_cancellation", framework="agents")


def test_output_processors_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.output_processors", framework="agents")


def test_noise_reduction_backends_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.noise_reduction_backends", framework="agents")


def test_cartesia_voice_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(
        monkeypatch,
        "examples.cartesia_voice",
        framework="agents",
        env={"CARTESIA_API_KEY": "test-key"},
    )


def test_deepgram_voice_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(
        monkeypatch,
        "examples.deepgram_voice",
        framework="agents",
        env={"DEEPGRAM_API_KEY": "test-key"},
    )


def test_elevenlabs_voice_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(
        monkeypatch,
        "examples.elevenlabs_voice",
        framework="agents",
        env={"ELEVENLABS_API_KEY": "test-key"},
    )


def test_combined_providers_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(
        monkeypatch,
        "examples.combined_providers",
        framework="agents",
        env={"DEEPGRAM_API_KEY": "test-key", "ELEVENLABS_API_KEY": "test-key"},
    )


def test_responses_api_bridge_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(
        monkeypatch,
        "examples.responses_api_bridge",
        env={
            "EASYCAT_REMOTE_AGENT_BASE_URL": "https://example.com",
            "EASYCAT_REMOTE_AGENT_API_KEY": "test-key",
            "EASYCAT_REMOTE_AGENT_MODEL": "test-model",
        },
    )


def test_session_actions_openai_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.session_actions_openai", framework="agents")


def test_session_actions_pydantic_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.session_actions_pydantic", framework="pydantic_ai")


def test_pydantic_ai_workflow_voice_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.pydantic_ai_workflow_voice", framework="pydantic_ai")


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


# ── Examples that still use ``def main()`` ──────────────────────────


def test_ws_server_example_imports():
    import examples.ws_server as ws_server

    assert callable(ws_server.main)


def test_ws_server_uses_config_server_helper() -> None:
    source = (REPO_ROOT / "examples" / "ws_server.py").read_text(encoding="utf-8")

    assert "run_websocket_config_server" in source
    assert "create_session" not in source
    assert 'require_env("OPENAI_API_KEY")' in source


def test_ws_server_settings_default_to_loopback(monkeypatch: pytest.MonkeyPatch):
    from easycat.transports.websocket import websocket_session_server_config_from_env

    monkeypatch.delenv("EASYCAT_WS_HOST", raising=False)
    monkeypatch.delenv("EASYCAT_WS_PORT", raising=False)
    monkeypatch.delenv("EASYCAT_WS_TOKEN", raising=False)
    monkeypatch.delenv("EASYCAT_WS_MAX_SESSIONS", raising=False)

    settings = websocket_session_server_config_from_env()

    assert settings.host == "127.0.0.1"
    assert settings.port == 8765
    assert settings.auth_token is None
    assert settings.max_sessions == 10


def test_webtransport_server_example_imports():
    import examples.webtransport_server as webtransport_server

    assert callable(webtransport_server.main)


def test_webtransport_server_uses_config_server_helper() -> None:
    path = REPO_ROOT / "examples" / "webtransport_server.py"
    source = path.read_text(encoding="utf-8")

    assert _visible_code_line_count(path) <= 35
    assert "run_webtransport_config_server" in source
    assert "WebTransportTransportConfig" in source
    assert "WebTransportConnectionTransport" in source
    assert "create_session" not in source
    assert "SessionManager" not in source
    assert "attach_runtime_feedback" not in source
    assert "wait_for_shutdown_signal" not in source
    assert "asyncio.run(" not in source
    assert "await server.start()" not in source
    assert "await server.stop()" not in source


def test_ws_server_authorizes_bearer_or_query_token():
    from easycat.transports.websocket import websocket_server_authorized

    headers = Headers([("Authorization", "Bearer expected-token")])

    assert websocket_server_authorized(headers, "/", "expected-token")
    assert websocket_server_authorized(Headers(), "/?token=expected-token", "expected-token")
    assert not websocket_server_authorized(Headers(), "/", "expected-token")
    assert not websocket_server_authorized(
        Headers([("Authorization", "Bearer wrong")]), "/", "expected-token"
    )


def test_docker_compose_binds_ws_port_to_loopback_and_requires_token():
    compose = (REPO_ROOT / "docker" / "compose.yaml").read_text()

    assert "EASYCAT_WS_TOKEN: ${EASYCAT_WS_TOKEN:?set EASYCAT_WS_TOKEN" in compose
    assert '"127.0.0.1:8765:8765"' in compose
    assert '- "8765:8765"' not in compose


def test_docker_guide_serves_browser_client_from_localhost():
    guide = (REPO_ROOT / "docs" / "deployment" / "docker.md").read_text(encoding="utf-8")
    client = (REPO_ROOT / "examples" / "ws_browser_client.html").read_text(encoding="utf-8")

    assert "python -m http.server 8080 --directory examples" in guide
    assert "http://localhost:8080/ws_browser_client.html?token=<EASYCAT_WS_TOKEN>" in guide
    assert "`examples/ws_browser_client.html?token=" not in guide
    assert 'location.hostname + ":8765"' in client


def test_docker_env_secret_file_is_ignored_but_templates_are_allowed():
    guide = (REPO_ROOT / "docs" / "deployment" / "docker.md").read_text(encoding="utf-8")
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert "# docker/.env" in guide
    assert "docker compose --env-file docker/.env -f docker/compose.yaml up --build" in guide
    assert "picks it up automatically" not in guide
    assert ".env" in gitignore
    assert ".env.*" in gitignore
    assert "!.env.example" in gitignore
    assert "**/.env" in dockerignore
    assert "**/.env.*" in dockerignore
    assert "!**/.env.example" in dockerignore
    assert "**/*.pem" in dockerignore
    assert "**/*.key" in dockerignore
    assert "`**/*.pem` and `**/*.key`" in guide


def test_dockerignore_excludes_local_cache_and_agent_state() -> None:
    guide = (REPO_ROOT / "docs" / "deployment" / "docker.md").read_text(encoding="utf-8")
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    for pattern in (
        ".hypothesis/",
        ".mypy_cache/",
        ".pytest_cache/",
        ".ruff_cache/",
        ".uv-cache/",
        ".agents/",
        ".codex",
        ".codex/",
        ".claude/",
        ".pipecat-bench/",
        ".coverage",
        ".coverage.*",
        "coverage.xml",
        "htmlcov/",
        "site/",
        "mutants/",
        ".mutmut-cache",
    ):
        assert pattern in dockerignore
        assert f"`{pattern}`" in guide

    assert "local generated state is not uploaded" in guide
    assert "Generated reports and docs sites" in guide


def test_docker_guide_tracks_default_dockerfile_extras() -> None:
    guide = (REPO_ROOT / "docs" / "deployment" / "docker.md").read_text(encoding="utf-8")
    image_section = guide.split("## What the image contains", 1)[1].split("## ", 1)[0]
    extras = _dockerfile_default_extras()
    known_extras = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["optional-dependencies"]

    assert extras <= set(known_extras)
    assert "Dockerfile `EXTRAS` default" in image_section
    for extra in extras:
        assert f"`{extra}`" in image_section


def test_dockerfile_default_extras_cover_ws_server_golden_path() -> None:
    extras = _dockerfile_default_extras()
    ws_server = REPO_ROOT / "examples" / "ws_server.py"

    assert _uses_default_openai_providers(ws_server)
    assert "openai" in extras
    assert "openai-agents" in extras


def test_docker_provider_swap_guidance_uses_known_extras_and_easyconfig() -> None:
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    guide = (REPO_ROOT / "docs" / "deployment" / "docker.md").read_text(encoding="utf-8")
    swap_section = guide.split("## Swapping STT / TTS providers", 1)[1].split("## ", 1)[0]
    known_extras = set(
        tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
            "optional-dependencies"
        ]
    )

    build_arg_extra_sets = _docker_build_arg_extra_sets(dockerfile, swap_section)
    assert build_arg_extra_sets
    for extras in build_arg_extra_sets:
        assert extras <= known_extras

    assert "wire the providers into `EasyConfig`" in swap_section
    assert "wire the providers into `SessionConfig`" not in swap_section


def test_ws_supervisor_server_example_imports():
    import examples.ws_supervisor_server as ws_supervisor_server

    assert callable(ws_supervisor_server.main)


def test_ws_supervisor_server_uses_manager_feedback_lifecycle() -> None:
    path = REPO_ROOT / "examples" / "ws_supervisor_server.py"
    source = path.read_text(encoding="utf-8")

    assert _visible_code_line_count(path) <= 140
    assert "manager.connection(session_id, session, runtime_feedback=True)" in source
    assert "SessionAudioBroadcaster(session)" in source
    assert "serve_supervisor_websocket(" in source
    assert "supervisor_auth_token_from_env()" in source
    assert "create_shutdown_event()" in source
    assert "add_signal_handler" not in source
    assert "json.loads(raw)" not in source
    assert "hmac.compare_digest" not in source
    assert "attach_runtime_feedback" not in source


def test_ws_supervisor_client_supports_optional_token() -> None:
    html = (REPO_ROOT / "examples" / "ws_supervisor_client.html").read_text(encoding="utf-8")

    assert 'id="supervisorToken"' in html
    assert "auth_required" in html
    assert "subscribe.token = token" in html


def test_webrtc_observability_example_imports():
    pytest.importorskip("agents")
    import examples.webrtc_observability_server as webrtc_observability

    assert callable(webrtc_observability.main)


def test_webrtc_observability_debugger_url_is_validated():
    html = (REPO_ROOT / "examples/webrtc_static/webrtc_observability.html").read_text()

    assert "function safeDebuggerUrl(value)" in html
    assert 'parsed.protocol === "http:" || parsed.protocol === "https:"' in html
    assert "sameHost || loopbackPair" in html
    assert 'document.getElementById("debugger-frame").src = debuggerUrl' in html
    assert "const url = override ||" not in html


def test_webrtc_examples_default_signaling_to_loopback():
    server = (REPO_ROOT / "examples" / "webrtc_server.py").read_text(encoding="utf-8")
    observability = (REPO_ROOT / "examples" / "webrtc_observability_server.py").read_text(
        encoding="utf-8"
    )
    deploy = (REPO_ROOT / "examples" / "ec2_webrtc" / "deploy.sh").read_text(encoding="utf-8")

    assert "webrtc_transport_config_from_env()" in server
    assert "Bind address (default 127.0.0.1)" in server
    assert 'os.getenv("SIGNALING_HOST", "127.0.0.1")' not in server
    assert "_build_ice_servers" not in server
    assert "_env_flag" not in server
    assert "webrtc_transport_config_from_env(static_dir=_STATIC_DIR)" in observability
    assert 'os.getenv("SIGNALING_HOST", "127.0.0.1")' not in observability
    assert "_build_ice_servers" not in observability
    assert "_env_flag" not in observability
    assert "SIGNALING_HOST=0.0.0.0" in deploy


def test_browser_transport_examples_use_run_session_lifecycle():
    budgets = {
        "examples/ws_browser_example.py": 40,
        "examples/webrtc_server.py": 30,
        "examples/webrtc_observability_server.py": 60,
    }

    for relpath, budget in budgets.items():
        path = REPO_ROOT / relpath
        source = path.read_text(encoding="utf-8")

        assert _visible_code_line_count(path) <= budget
        assert "create_session(" in source
        assert "from easycat.helpers import run_session" in source
        assert "run_session(session)" in source
        assert "attach_runtime_feedback" not in source
        assert "wait_for_shutdown_signal" not in source
        assert "asyncio.run(" not in source
        assert "await session.start()" not in source
        assert "await session.stop()" not in source


def test_push_to_talk_example_imports():
    import examples.push_to_talk as push_to_talk

    assert callable(push_to_talk.main)


def test_push_to_talk_example_uses_scoped_lifecycle():
    path = REPO_ROOT / "examples/push_to_talk.py"
    source = path.read_text(encoding="utf-8")

    assert _visible_code_line_count(path) <= 35
    assert "TurnMode.PUSH_TO_TALK" in source
    assert "run_stdin_push_to_talk(session)" in source
    assert "await session.start_turn()" not in source
    assert "await session.end_turn()" not in source
    assert "async with create_session(config) as session:" in source
    assert "threading.Thread" not in source
    assert "loop.add_reader" not in source
    assert "await session.start()" not in source
    assert "await session.stop(" not in source
    assert "SessionConfig" not in source
    assert "Session(" not in source


def test_custom_tts_provider_example_imports():
    import examples.custom_tts_provider as custom_tts_provider

    assert callable(custom_tts_provider.main)


def test_custom_vad_provider_example_imports():
    import examples.custom_vad_provider as custom_vad_provider

    assert callable(custom_vad_provider.main)


def test_custom_stt_provider_example_imports():
    import examples.custom_stt_provider as custom_stt_provider

    assert callable(custom_stt_provider.main)


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


def test_agent_event_subscription_example_imports():
    pytest.importorskip("agents")
    import examples.agent_event_subscription as agent_event_subscription

    assert callable(agent_event_subscription.main)


def test_agent_event_subscription_example_uses_run_session():
    path = REPO_ROOT / "examples/agent_event_subscription.py"
    source = path.read_text(encoding="utf-8")

    assert _visible_code_line_count(path) <= 50
    assert "from easycat import" in source
    assert "run_session" in source
    assert "create_session(" in source
    assert "session.subscribe_agent_events(" in source
    assert "session.unsubscribe_handlers(registrations)" in source
    assert "attach_runtime_feedback" not in source
    assert "wait_for_shutdown_signal" not in source
    assert "asyncio.run(" not in source
    assert "await session.start()" not in source
    assert "await session.stop()" not in source


def test_vad_backends_example_imports():
    import examples.vad_backends as vad_backends

    assert callable(vad_backends.main)


def test_vad_backends_example_uses_easyconfig_provider_instance_surface():
    path = REPO_ROOT / "examples/vad_backends.py"
    source = path.read_text(encoding="utf-8")

    assert _visible_code_line_count(path) <= 30
    assert "vad = create_vad(VADConfig(backend=backend))" in source
    assert "EasyConfig.mic(" in source
    assert "vad=vad" in source
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


def test_reconnecting_ws_client_example_imports():
    import examples.reconnecting_ws_client as reconnecting_ws_client

    assert callable(reconnecting_ws_client.main)


def test_reconnecting_ws_client_uses_shared_shutdown_and_connect_helpers() -> None:
    path = REPO_ROOT / "examples" / "reconnecting_ws_client.py"
    source = path.read_text(encoding="utf-8")

    assert _visible_code_line_count(path) <= 75
    assert "create_shutdown_event()" in source
    assert "connect_until_stopped(ws, stop)" in source
    assert "add_signal_handler" not in source
    assert "signal.SIGINT" not in source


def test_telephony_helpers_example_imports():
    import examples.telephony_helpers as telephony_helpers

    assert callable(telephony_helpers.main)


def test_debug_bundle_example_imports():
    import examples.debug_bundle as debug_bundle

    assert callable(debug_bundle.main)


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


def test_journal_ui_example_imports():
    pytest.importorskip("agents")
    import examples.journal_ui as journal_ui

    assert callable(journal_ui.main)


def test_journal_ui_example_uses_run_session():
    path = REPO_ROOT / "examples/journal_ui.py"
    source = path.read_text(encoding="utf-8")

    assert _visible_code_line_count(path) <= 25
    assert "serve_session(session" in source
    assert "run_session(session)" in source
    assert 'debug="light"' in source
    assert "attach_runtime_feedback" not in source
    assert "wait_for_shutdown_signal" not in source
    assert "asyncio.run(" not in source
    assert "await session.start()" not in source
    assert "await session.stop()" not in source


# ── Subprocess smoke test ───────────────────────────────────────────

# Scripts that import an optional agent framework at module scope; the
# subprocess test must skip when the framework isn't installed,
# otherwise the script exits before reaching the OPENAI_API_KEY check.
_REQUIRES_AGENTS = frozenset(
    {
        "examples/openai_agents_voice.py",
        "examples/function_tools_openai.py",
        "examples/smart_turn_demo.py",
        "examples/combined_providers.py",
        "examples/cartesia_voice.py",
        "examples/deepgram_voice.py",
        "examples/elevenlabs_voice.py",
        "examples/output_processors.py",
        "examples/agent_event_subscription.py",
        "examples/noise_reduction_backends.py",
        "examples/echo_cancellation.py",
        "examples/session_actions_openai.py",
        "examples/journal_ui.py",
        "examples/debug_bundle.py",
        "examples/webrtc_observability_server.py",
    }
)
_REQUIRES_PYDANTIC_AI = frozenset(
    {
        "examples/pydantic_ai_voice.py",
        "examples/function_tools_pydantic.py",
        "examples/session_actions_pydantic.py",
        "examples/pydantic_ai_workflow_voice.py",
    }
)
_REQUIRES_LANGCHAIN_OPENAI = frozenset(
    {
        "examples/langchain_voice.py",
        "examples/function_tools_langchain.py",
        "examples/session_actions_langchain.py",
        "examples/langgraph_voice.py",
        "examples/function_tools_langgraph.py",
        "examples/session_actions_langgraph.py",
    }
)
_REQUIRES_LANGGRAPH = frozenset(
    {
        "examples/langgraph_voice.py",
        "examples/function_tools_langgraph.py",
        "examples/session_actions_langgraph.py",
    }
)
# The two AgentExecutor examples import ``langchain.agents`` — the full
# ``langchain`` package, not just ``langchain-openai``.  Mirrors the
# ``_skip_unless_langchain_v0()`` guard in their import tests so an env
# with ``langchain-openai`` but no ``langchain`` (or LangChain 1.x)
# skips them instead of running scripts that exit with the
# LangChain-install message and fail the stderr assertion below.
_REQUIRES_LANGCHAIN = frozenset(
    {
        "examples/function_tools_langchain.py",
        "examples/session_actions_langchain.py",
    }
)


def _skip_unless_langchain_v0() -> None:
    """Skip unless ``langchain`` is importable *and* <1.0.

    The two AgentExecutor examples build their agent with
    ``langchain.agents.create_tool_calling_agent``, which LangChain 1.x
    removed (its replacement ``langchain.agents.create_agent`` returns a
    LangGraph ``CompiledStateGraph`` — covered by the langgraph
    examples).  Their import guard therefore raises ``SystemExit`` under
    1.x and the scripts exit with the install message instead of the
    expected ``OPENAI_API_KEY`` / ``STT configuration`` error.
    ``importorskip`` alone only proves ``langchain`` is importable —
    1.x is — so add an explicit major-version check.
    """
    langchain = pytest.importorskip("langchain")
    raw = getattr(langchain, "__version__", "0")
    head = "".join(c for c in str(raw).split(".")[0] if c.isdigit())
    if int(head or "0") >= 1:
        pytest.skip(
            f"AgentExecutor examples require langchain<1 "
            f"(create_tool_calling_agent removed in 1.x); found {raw}"
        )


def test_langchain_voice_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.langchain_voice", framework="langchain_openai")


def test_langgraph_voice_example_imports(monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("langgraph")
    _load_slim_example(monkeypatch, "examples.langgraph_voice", framework="langchain_openai")


def test_function_tools_langchain_example_imports(monkeypatch: pytest.MonkeyPatch):
    _skip_unless_langchain_v0()
    _load_slim_example(
        monkeypatch, "examples.function_tools_langchain", framework="langchain_openai"
    )


def test_function_tools_langgraph_example_imports(monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("langgraph")
    _load_slim_example(
        monkeypatch, "examples.function_tools_langgraph", framework="langchain_openai"
    )


def test_session_actions_langchain_example_imports(monkeypatch: pytest.MonkeyPatch):
    _skip_unless_langchain_v0()
    _load_slim_example(
        monkeypatch, "examples.session_actions_langchain", framework="langchain_openai"
    )


def test_session_actions_langgraph_example_imports(monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("langgraph")
    _load_slim_example(
        monkeypatch, "examples.session_actions_langgraph", framework="langchain_openai"
    )


def _python_executable() -> str:
    candidate = sys.executable or ""
    if candidate:
        return candidate
    for name in ("python3", "python"):
        resolved = shutil.which(name)
        if resolved:
            return resolved
    pytest.skip("No python executable available for subprocess test")


@pytest.mark.parametrize(
    "script_path",
    [
        "examples/openai_agents_voice.py",
        "examples/ws_server.py",
        "examples/ws_supervisor_server.py",
        "examples/ws_browser_example.py",
        "examples/webrtc_server.py",
        "examples/webrtc_observability_server.py",
        "examples/pydantic_ai_voice.py",
        "examples/function_tools_openai.py",
        "examples/function_tools_pydantic.py",
        "examples/session_actions_openai.py",
        "examples/session_actions_pydantic.py",
        "examples/pydantic_ai_workflow_voice.py",
        "examples/push_to_talk.py",
        "examples/smart_turn_demo.py",
        "examples/combined_providers.py",
        "examples/cartesia_voice.py",
        "examples/deepgram_voice.py",
        "examples/elevenlabs_voice.py",
        "examples/debug_bundle.py",
        "examples/custom_stt_provider.py",
        "examples/custom_tts_provider.py",
        "examples/custom_vad_provider.py",
        "examples/output_processors.py",
        "examples/agent_event_subscription.py",
        "examples/vad_backends.py",
        "examples/noise_reduction_backends.py",
        "examples/responses_api_bridge.py",
        "examples/echo_cancellation.py",
        "examples/journal_ui.py",
        "examples/langchain_voice.py",
        "examples/langgraph_voice.py",
        "examples/function_tools_langchain.py",
        "examples/function_tools_langgraph.py",
        "examples/session_actions_langchain.py",
        "examples/session_actions_langgraph.py",
    ],
)
def test_examples_can_run_as_scripts_without_package_import_errors(script_path: str):
    if script_path in _REQUIRES_AGENTS:
        pytest.importorskip("agents")
    if script_path in _REQUIRES_PYDANTIC_AI:
        pytest.importorskip("pydantic_ai")
    if script_path in _REQUIRES_LANGCHAIN_OPENAI:
        pytest.importorskip("langchain_openai")
    if script_path in _REQUIRES_LANGCHAIN:
        _skip_unless_langchain_v0()
    if script_path in _REQUIRES_LANGGRAPH:
        pytest.importorskip("langgraph")

    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)

    completed = subprocess.run(
        [_python_executable(), script_path],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "ModuleNotFoundError" not in completed.stderr
    # Examples using ``easycat.run(...)`` / ``EasyConfig.mic|browser|phone()``
    # now route the no-key path through the error catalog, failing config
    # validation with ``EASYCAT_E203: Missing API key: OPENAI_API_KEY`` when
    # no provider env var is set. A few still surface the bare
    # "STT configuration is required." (e.g. an explicit non-key config gap),
    # and others call ``require_env`` and emit "OPENAI_API_KEY is required."
    # — accept any of the three.
    assert (
        "OPENAI_API_KEY is required." in completed.stderr
        or "STT configuration is required." in completed.stderr
        or "Missing API key: OPENAI_API_KEY" in completed.stderr
    )


def test_twilio_example_factory():
    if importlib.util.find_spec("fastapi") is None:
        pytest.skip("fastapi not installed")
    if importlib.util.find_spec("agents") is None:
        pytest.skip("openai-agents not installed")
    import examples.twilio_app as twilio_app

    app = twilio_app.create_app(api_key="test-key", stream_url="wss://example.com/stream")
    assert app is not None


def test_twilio_example_uses_manager_feedback_lifecycle():
    path = REPO_ROOT / "examples" / "twilio_app.py"
    source = path.read_text(encoding="utf-8")

    assert _visible_code_line_count(path) <= 180
    assert "manager.connection(key, session, runtime_feedback=True)" in source
    assert "CallAnswered" in source
    assert "twilio_form_items_from_request" in source
    assert "twilio_stream_parameters_from_form" in source
    assert "parse_qsl" not in source
    assert "validate_twilio_webhook_signature" not in source
    assert "attach_runtime_feedback" not in source


def test_twilio_example_missing_openai_key_is_actionable(monkeypatch: pytest.MonkeyPatch):
    import examples.twilio_app as twilio_app

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        twilio_app.create_app(stream_url="wss://example.com/stream")

    message = str(exc_info.value)
    assert "OPENAI_API_KEY is required." in message
    assert "uv run easycat doctor" in message
    assert "uv run easycat doctor --env-file .env" in message


def test_example_session_smoke():
    config = EasyConfig(
        openai_api_key="test-key",
        transport=WebSocketTransportConfig(),
        agent=_DummyAgent(),
    )
    try:
        session = create_session(config)
    except RuntimeError as exc:
        if "No VAD backend available" in str(exc):
            pytest.skip("No VAD backend available")
        raise
    assert session is not None
