from __future__ import annotations

import ast
import re
from pathlib import Path

from easycat import EasyConfig
from easycat.cli._app import _docs_entries
from easycat.stt import OpenAIRealtimeSTTConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
FEATURE_LADDER = REPO_ROOT / "docs" / "using-easycat"
AVAILABLE_ROW_RE = re.compile(
    r"^\| (?P<number>\d+) "
    r"\| \[`(?P<name>[^`]+)`\]\(\./(?P<link>[^)]+)/\) "
    r"\| (?P<features>[^|]+) "
    r"\| Available \|$",
    flags=re.MULTILINE,
)
API_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9_]*_API_KEY\b")
UV_EXTRA_RE = re.compile(r"--extra\s+(?P<extra>[A-Za-z0-9_.-]+)")


def _chapter_dirs() -> list[Path]:
    return sorted(FEATURE_LADDER.glob("[0-9][0-9]-*"))


def _prerequisites(readme: str) -> str:
    return readme.split("## Prerequisites", 1)[1].split("\n## ", 1)[0]


def _required_env_vars(tree: ast.AST) -> set[str]:
    required: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "require_env":
            continue
        first_arg = node.args[0]
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            required.add(first_arg.value)
    return required


def test_feature_ladder_available_rows_match_published_chapters() -> None:
    readme = (FEATURE_LADDER / "README.md").read_text(encoding="utf-8")
    rows = [match.groupdict() for match in AVAILABLE_ROW_RE.finditer(readme)]
    chapter_dirs = [path.name for path in _chapter_dirs()]

    assert [row["link"] for row in rows] == chapter_dirs
    assert [row["name"] for row in rows] == chapter_dirs
    assert [int(row["number"]) for row in rows] == list(range(len(chapter_dirs)))


def test_feature_ladder_declares_complete_feature_journey() -> None:
    readme = (FEATURE_LADDER / "README.md").read_text(encoding="utf-8")

    for feature in (
        "VoiceApp",
        "Browser, WebSocket, local, and Twilio",
        "STT/TTS provider specs",
        "VAD, smart turn, interruption, push-to-talk",
        "tools, fillers, session actions, and pronunciation",
        "OpenAI Agents, PydanticAI, LangChain, LangGraph, LlamaAgents",
        "`EasyConfig`, `Session`, events, text turns, and lifecycle",
        "Journals, bundles, inspect, replay, diff, and the debugger",
        "Offline turns, assertions, evals, and latency budgets",
        "Per-connection factories, authentication, limits, and supervision",
        "Twilio streams, outbound calls, screening, IVR, and call control",
        "Validation, deployment, durability, metrics, and production teardown",
    ):
        assert feature in readme

    assert "how voice AI works" in readme
    assert "what EasyCat can do and how to use it" in readme
    assert "[voice-pipeline ladder](../teaching/)" in readme


def test_feature_chapters_have_self_contained_reader_entrypoints() -> None:
    missing: list[str] = []

    for chapter in _chapter_dirs():
        for filename in ("README.md", "EXERCISES.md", "main.py"):
            if not (chapter / filename).exists():
                missing.append(f"{chapter.name}/{filename}")
        if not (chapter / "README.md").exists():
            continue
        readme = (chapter / "README.md").read_text(encoding="utf-8")
        command = f"uv run python docs/using-easycat/{chapter.name}/main.py"
        if command not in readme:
            missing.append(f"{chapter.name}: documented `{command}`")

    assert not missing, "Feature chapters missing reader entrypoints: " + ", ".join(missing)


def test_feature_chapter_prerequisites_cover_script_requirements() -> None:
    stale: list[str] = []

    for chapter in _chapter_dirs():
        readme = (chapter / "README.md").read_text(encoding="utf-8")
        prerequisites = _prerequisites(readme)
        script = chapter / "main.py"
        source = script.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=script.as_posix())
        docstring = ast.get_docstring(tree) or ""
        script_keys = set(API_KEY_RE.findall(docstring))
        script_env_vars = _required_env_vars(tree)
        script_extras = set(UV_EXTRA_RE.findall(docstring))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                script_keys.update(API_KEY_RE.findall(node.value))

        missing_keys = sorted(script_keys - set(API_KEY_RE.findall(prerequisites)))
        missing_env_vars = sorted(
            script_env_vars - set(re.findall(r"\b[A-Z][A-Z0-9_]+\b", prerequisites))
        )
        missing_extras = sorted(script_extras - set(UV_EXTRA_RE.findall(prerequisites)))
        if missing_keys or missing_env_vars or missing_extras:
            stale.append(
                f"{chapter.name}: missing keys {missing_keys or '-'}, "
                f"env vars {missing_env_vars or '-'}, "
                f"extras {missing_extras or '-'}"
            )
        if script_env_vars and "uv run easycat doctor --env-file .env" not in prerequisites:
            stale.append(f"{chapter.name}: missing .env doctor preflight")
        if script_env_vars and "add `--env-file .env` after `uv run`" not in prerequisites:
            stale.append(f"{chapter.name}: missing .env runtime guidance")

    assert not stale, "Feature chapter prerequisites drifted:\n" + "\n".join(stale)


def test_first_feature_chapter_uses_only_the_public_easycat_app_surface() -> None:
    script = FEATURE_LADDER / "00-first-voice-app" / "main.py"
    tree = ast.parse(script.read_text(encoding="utf-8"), filename=script.as_posix())
    easycat_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("easycat")
    ]

    assert len(easycat_imports) == 1
    assert easycat_imports[0].module == "easycat"
    assert {alias.name for alias in easycat_imports[0].names} == {"VoiceApp", "require_env"}
    assert 'app.run("local")' in script.read_text(encoding="utf-8")


def test_first_feature_chapter_names_the_registered_realtime_stt() -> None:
    exercises = (FEATURE_LADDER / "00-first-voice-app" / "EXERCISES.md").read_text(
        encoding="utf-8"
    )

    assert 'stt="openai-realtime"' in exercises
    assert 'stt="openai/realtime"' not in exercises
    default_config = EasyConfig(openai_api_key="test-key")
    explicit_config = EasyConfig(openai_api_key="test-key", stt="openai-realtime")
    assert isinstance(default_config.stt, OpenAIRealtimeSTTConfig)
    assert explicit_config.stt == default_config.stt


def test_runtime_modes_chapter_covers_every_voice_app_mode_and_boundary() -> None:
    chapter = FEATURE_LADDER / "01-runtime-modes"
    script = (chapter / "main.py").read_text(encoding="utf-8")
    readme = (chapter / "README.md").read_text(encoding="utf-8")

    for mode in ("local", "browser", "websocket", "twilio"):
        assert f'"{mode}"' in script
        assert f"main.py {mode}" in readme
    for extra in ("quickstart", "webrtc", "telephony"):
        assert f"--extra {extra}" in _prerequisites(readme)
    for env_var in ("OPENAI_API_KEY", "TWILIO_STREAM_URL", "TWILIO_AUTH_TOKEN"):
        assert env_var in _prerequisites(readme)
    for concept in (
        "fresh session per connection",
        "config_factory",
        "`run()` versus `serve()`",
        "non-loopback browser or WebSocket bind without authentication",
    ):
        assert concept in readme


def test_feature_scripts_do_not_import_easycat_internals() -> None:
    internal_imports: list[str] = []

    for script in FEATURE_LADDER.glob("[0-9][0-9]-*/*.py"):
        tree = ast.parse(script.read_text(encoding="utf-8"), filename=script.as_posix())
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom | ast.Import) or not node.lineno:
                continue
            modules = (
                [node.module]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
            )
            for module in modules:
                if (
                    module
                    and module.startswith("easycat.")
                    and any(part.startswith("_") for part in module.split("."))
                ):
                    internal_imports.append(
                        f"{script.relative_to(REPO_ROOT).as_posix()}:{node.lineno}: {module}"
                    )

    assert not internal_imports, "Feature lessons imported internals:\n" + "\n".join(
        internal_imports
    )


def test_feature_ladder_is_discoverable_from_public_docs_surfaces() -> None:
    root_readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    docs_readme = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    entries = {entry["path"]: entry for entry in _docs_entries()}

    assert "[EasyCat feature ladder](docs/using-easycat/)" in root_readme
    assert "[EasyCat feature ladder](using-easycat/)" in docs_readme
    assert entries["docs/using-easycat/"]["diataxis"] == "tutorial"
    assert entries["docs/using-easycat/"]["audience"] == "learners"
    assert (
        "uv run python docs/using-easycat/00-first-voice-app/main.py"
        in entries["docs/using-easycat/"]["commands"]
    )
    assert entries["docs/using-easycat/00-first-voice-app/"]["diataxis"] == "tutorial"
    runtime_modes = entries["docs/using-easycat/01-runtime-modes/"]
    assert runtime_modes["diataxis"] == "tutorial"
    assert runtime_modes["audience"] == "learners"
    assert (
        "uv run python docs/using-easycat/01-runtime-modes/main.py browser"
        in runtime_modes["commands"]
    )
