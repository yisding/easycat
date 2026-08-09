from __future__ import annotations

# ruff: noqa: F401
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

REPO_ROOT = Path(__file__).resolve().parents[2]

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


def _example_run_command_problems(row: dict[str, str]) -> list[str]:  # noqa: C901
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
    """Import a slim example that starts an EasyCat app at module scope.

    Stubs both ``easycat.run`` and ``VoiceApp.run`` so importing doesn't block,
    sets any env vars the example consumes at module scope (via
    ``require_env`` or EasyConfig string shortcuts), and evicts any cached
    copy so the fresh import sees the monkeypatched ``run``.
    """
    if framework:
        pytest.importorskip(framework)

    import easycat

    monkeypatch.setattr(easycat, "run", lambda config: None)
    monkeypatch.setattr(easycat.VoiceApp, "run", lambda self, *args, **kwargs: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)

    sys.modules.pop(module_name, None)
    __import__(module_name)


_REQUIRES_AGENTS = frozenset(
    {
        "examples/voice_app.py",
        "examples/voice_app_twilio.py",
        "examples/openai_agents_voice.py",
        "examples/ws_browser_example.py",
        "examples/webrtc_server.py",
        "examples/function_tools_openai.py",
        "examples/smart_turn_demo.py",
        "examples/combined_providers.py",
        "examples/cartesia_voice.py",
        "examples/deepgram_voice.py",
        "examples/elevenlabs_voice.py",
        "examples/output_processors.py",
        "examples/agent_event_subscription.py",
        "examples/vad_backends.py",
        "examples/noise_reduction_backends.py",
        "examples/echo_cancellation.py",
        "examples/session_actions_openai.py",
        "examples/push_to_talk.py",
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
            "AgentExecutor examples require the langchain-v0 extra "
            f"(create_tool_calling_agent was removed in 1.x); found {raw}"
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
