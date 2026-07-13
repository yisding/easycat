from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from easycat.events import (
    SessionActionCompleted,
    SessionActionFailed,
    SessionActionRequested,
    SessionActionStarted,
    ToolCallDelta,
    ToolCallResult,
    ToolCallStarted,
)
from easycat.session._journal_sink import _SIMPLE_EVENT_RECORDS
from scripts.regen_teaching_chapters import (
    NAVIGATION_RE,
    OFFLINE_CHECKPOINT_RE,
    ROOT,
    TEACHING,
    Chapter,
    _resolve_child_path,
    discover_chapters,
    regen_exercises,
    regen_readme,
    render_diff,
    render_exercise_navigation,
    render_navigation,
    render_offline_checkpoint,
)

SOURCE_PATH_RE = re.compile(
    r"`(?P<path>src/easycat/[A-Za-z0-9_./-]+\.py)"
    r"(?:::(?P<symbol>[A-Za-z_][A-Za-z0-9_]*)(?:\(\))?)?`"
)


def _add_assignment_target_symbols(target: ast.AST, symbols: set[str]) -> None:
    if isinstance(target, ast.Name):
        symbols.add(target.id)
    elif isinstance(target, ast.Tuple | ast.List):
        for item in target.elts:
            _add_assignment_target_symbols(item, symbols)


def _collect_defined_symbols(nodes: list[ast.stmt], symbols: set[str]) -> None:
    for node in nodes:
        if isinstance(node, ast.ClassDef):
            symbols.add(node.name)
            _collect_defined_symbols(node.body, symbols)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            symbols.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                _add_assignment_target_symbols(target, symbols)
        elif isinstance(node, ast.AnnAssign):
            _add_assignment_target_symbols(node.target, symbols)


def _defined_symbols(source_path: Path) -> set[str]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=source_path.as_posix())
    symbols: set[str] = set()
    _collect_defined_symbols(tree.body, symbols)
    return symbols


def test_teaching_readmes_match_regenerated_auto_blocks() -> None:
    stale_readmes: list[str] = []

    for chapter in discover_chapters():
        readme = chapter.path / "README.md"
        if not readme.exists():
            continue
        original, updated = regen_readme(chapter)
        if original != updated:
            stale_readmes.append(readme.relative_to(ROOT).as_posix())

    assert not stale_readmes, (
        "Teaching README auto blocks are stale. Run "
        "`uv run python scripts/regen_teaching_chapters.py`: " + ", ".join(stale_readmes)
    )


def test_render_navigation_handles_first_middle_and_last_chapters() -> None:
    chapters = discover_chapters()

    assert render_navigation(chapters[0]) == (
        "[Teaching ladder](../) · [Exercises](./EXERCISES.md) · [Chapter 1 — Echo →](../01-echo/)"
    )
    assert render_navigation(chapters[8]) == (
        "[← Chapter 7 — Tools, Mid-stream](../07-tools/) · "
        "[Teaching ladder](../) · "
        "[Exercises](./EXERCISES.md) · "
        "[Chapter 9 — Interruption / Barge-in →](../09-interruption/)"
    )
    assert render_navigation(chapters[-1]) == (
        "[← Chapter 14 — Bring your own agent](../14-bring-your-own-agent/) · "
        "[Teaching ladder](../) · [Exercises](./EXERCISES.md)"
    )


def test_each_chapter_has_one_current_generated_navigation_block() -> None:
    for chapter in discover_chapters():
        readme = (chapter.path / "README.md").read_text(encoding="utf-8")
        matches = list(NAVIGATION_RE.finditer(readme))

        assert len(matches) == 1, chapter.slug
        assert matches[0].group("body").strip() == render_navigation(chapter)
        assert matches[0].start() > readme.index("# ")
        assert matches[0].start() < readme.find("\n## ")


def test_each_chapter_has_one_current_generated_offline_checkpoint() -> None:
    for chapter in discover_chapters():
        readme = (chapter.path / "README.md").read_text(encoding="utf-8")
        matches = list(OFFLINE_CHECKPOINT_RE.finditer(readme))

        assert len(matches) == 1, chapter.slug
        assert matches[0].group("body") == render_offline_checkpoint(chapter)
        assert matches[0].start() > readme.index("<!-- END auto:navigation -->")
        assert matches[0].end() < readme.find("\n## Prerequisites")


def test_offline_checkpoint_comes_from_the_spine_manifest() -> None:
    chapter = discover_chapters()[5]

    assert render_offline_checkpoint(chapter) == (
        "\n> **Hardware-free checkpoint:** prove `first-audio outcomes` without a microphone,\n"
        "> speakers, or provider credentials:\n"
        ">\n"
        "> ```bash\n"
        "> uv run python docs/teaching/05-blocking-agent/tts_outcome_probe.py\n"
        "> ```\n"
        ">\n"
        "> [See all 16 checkpoints](../#hardware-free-checkpoint-spine).\n"
    )


def test_render_exercise_navigation_handles_first_middle_and_last_chapters() -> None:
    chapters = discover_chapters()

    assert render_exercise_navigation(chapters[0]) == (
        "[← Chapter narrative](./README.md) · "
        "[Teaching ladder](../) · "
        "[Chapter 1 — Echo →](../01-echo/)"
    )
    assert render_exercise_navigation(chapters[8]) == (
        "[← Chapter narrative](./README.md) · "
        "[Teaching ladder](../) · "
        "[Chapter 9 — Interruption / Barge-in →](../09-interruption/)"
    )
    assert render_exercise_navigation(chapters[-1]) == (
        "[← Chapter narrative](./README.md) · [Teaching ladder](../)"
    )


def test_each_exercise_page_has_one_current_generated_navigation_block() -> None:
    for chapter in discover_chapters():
        exercises = (chapter.path / "EXERCISES.md").read_text(encoding="utf-8")
        matches = list(NAVIGATION_RE.finditer(exercises))

        assert len(matches) == 1, chapter.slug
        assert matches[0].group("body").strip() == render_exercise_navigation(chapter)
        assert matches[0].start() > exercises.index("# ")
        assert matches[0].start() < exercises.find("\n## ")


def test_teaching_exercises_match_regenerated_navigation() -> None:
    stale_exercises: list[str] = []

    for chapter in discover_chapters():
        exercises = chapter.path / "EXERCISES.md"
        if not exercises.exists():
            continue
        original, updated = regen_exercises(chapter)
        if original != updated:
            stale_exercises.append(exercises.relative_to(ROOT).as_posix())

    assert not stale_exercises, (
        "Teaching exercise navigation is stale. Run "
        "`uv run python scripts/regen_teaching_chapters.py`: " + ", ".join(stale_exercises)
    )


def test_resolve_child_path_rejects_traversal_outside_base() -> None:
    with pytest.raises(ValueError, match="prev_src=.*escapes docs/teaching/00-hello-audio"):
        _resolve_child_path(
            TEACHING / "00-hello-audio",
            "../../../../../etc/hostname",
            "prev_src",
        )


def test_render_diff_rejects_traversed_prev_src_before_reading() -> None:
    chapter = Chapter(TEACHING / "01-echo")

    with pytest.raises(ValueError, match="prev_src=.*escapes docs/teaching/00-hello-audio"):
        render_diff(
            chapter,
            {
                "prev": "00-hello-audio",
                "prev_src": "../../../../../etc/hostname",
                "src": "main.py",
            },
        )


def test_render_diff_still_allows_chapter_local_prev_src() -> None:
    chapter = Chapter(TEACHING / "03-parrot-naive")

    rendered = render_diff(
        chapter,
        {"prev": "02-transcribe", "prev_src": "streaming.py", "src": "main.py"},
    )

    assert "docs/teaching/02-transcribe/streaming.py" in rendered
    assert "docs/teaching/03-parrot-naive/main.py" in rendered


def test_render_diff_can_trim_markdown_blank_context_whitespace() -> None:
    chapter = Chapter(TEACHING / "14-bring-your-own-agent")

    rendered = render_diff(
        chapter,
        {
            "prev": "13-swap-providers-and-transports",
            "src": "main.py",
            "trim_blank_context": "true",
        },
    )

    assert not any(line == " " for line in rendered.splitlines())


def test_teaching_plan_source_path_mentions_resolve() -> None:
    """Keep teaching-plan code-span source pointers from drifting after refactors."""
    docs = sorted((ROOT / "docs" / "teaching").rglob("*.md"))
    plans = sorted((ROOT / "plan" / "teaching" / "chapter-plans").glob("*.md"))
    missing: list[str] = []
    symbols_by_path: dict[str, set[str]] = {}

    for doc in docs + plans:
        for line_number, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            for match in SOURCE_PATH_RE.finditer(line):
                path_text = match.group("path")
                symbol = match.group("symbol")
                source_path = ROOT / path_text
                rel = doc.relative_to(ROOT).as_posix()
                if not source_path.exists():
                    missing.append(f"{rel}:{line_number}: `{path_text}`")
                    continue
                if symbol is not None:
                    defined_symbols = symbols_by_path.setdefault(
                        path_text,
                        _defined_symbols(source_path),
                    )
                    if symbol not in defined_symbols:
                        missing.append(f"{rel}:{line_number}: `{path_text}::{symbol}`")

    assert not missing, "Teaching docs reference missing source files or symbols:\n" + "\n".join(
        missing
    )


def test_teaching_materials_use_current_beginner_config_name() -> None:
    """Keep teaching materials aligned with the public EasyConfig surface."""
    docs = sorted((ROOT / "docs" / "teaching").rglob("*.md"))
    plans = sorted((ROOT / "plan" / "teaching" / "chapter-plans").glob("*.md"))
    stale: list[str] = []

    for doc in docs + plans:
        for line_number, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            if "EasyCatConfig" in line:
                stale.append(f"{doc.relative_to(ROOT).as_posix()}:{line_number}: {line.strip()}")

    assert not stale, "Teaching materials should say EasyConfig, not EasyCatConfig:\n" + "\n".join(
        stale
    )


def test_teaching_easyconfig_examples_rely_on_openai_env_key() -> None:
    """EasyConfig teaching scripts preflight OPENAI_API_KEY but let config read it."""
    stale: list[str] = []

    for path in sorted((ROOT / "docs" / "teaching").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix())
        for node in ast.walk(tree):
            if not isinstance(node, ast.keyword) or node.arg != "openai_api_key":
                continue
            value = node.value
            if not (
                isinstance(value, ast.Subscript)
                and isinstance(value.value, ast.Attribute)
                and isinstance(value.value.value, ast.Name)
                and value.value.value.id == "os"
                and value.value.attr == "environ"
                and isinstance(value.slice, ast.Constant)
                and value.slice.value == "OPENAI_API_KEY"
            ):
                continue
            stale.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")

    assert not stale, (
        "Teaching EasyConfig examples should let EasyConfig read OPENAI_API_KEY:\n"
        + "\n".join(stale)
    )


def test_tools_teaching_plan_uses_current_agent_bridge_event_contract() -> None:
    """Keep the tools chapter plan aligned with the current bridge event surface."""
    plan = (ROOT / "plan" / "teaching" / "chapter-plans" / "teaching-07-tools.md").read_text(
        encoding="utf-8"
    )

    assert "easycat.integrations.agents.base.AgentBridgeEvent" in plan
    assert '"tool_started"' in plan
    assert '"tool_delta"' in plan
    assert '"tool_result"' in plan
    assert "_legacy_types.AgentStreamEventType" not in plan
    assert "AgentStreamEventType." not in plan


def test_tools_teaching_plan_tracks_journal_sink_ownership() -> None:
    """Keep the tools chapter plan aligned with production tool-call journaling."""
    plan = (ROOT / "plan" / "teaching" / "chapter-plans" / "teaching-07-tools.md").read_text(
        encoding="utf-8"
    )

    assert "SessionJournalSink" in plan
    assert "src/easycat/session/_journal_sink.py::SessionJournalSink" in plan
    registered_records = {spec.event_type: spec.name for spec in _SIMPLE_EVENT_RECORDS}
    tool_call_events = (ToolCallStarted, ToolCallDelta, ToolCallResult)
    assert {registered_records[event_type] for event_type in tool_call_events} == {
        "tool_call_started",
        "tool_call_delta",
        "tool_call_result",
    }
    assert all(registered_records[event_type] in plan for event_type in tool_call_events)
    assert "tool name and call id" in plan
    assert "tool name and args" not in plan
    assert "session/_session.py" not in plan
    assert "_sub(ToolCallStarted" not in plan
    assert "Session._subscribe_journal_sink" not in plan

    # SessionAction lifecycle events are now journaled in the same declarative
    # registry as tool calls; the plan must document this instead of
    # claiming a journaling gap.
    session_action_events = (
        SessionActionRequested,
        SessionActionStarted,
        SessionActionCompleted,
        SessionActionFailed,
    )
    assert {registered_records[event_type] for event_type in session_action_events} == {
        "session_action_requested",
        "session_action_started",
        "session_action_completed",
        "session_action_failed",
    }
    assert all(registered_records[event_type] in plan for event_type in session_action_events)
    assert "*not* currently journaled" not in plan
    assert "`SessionAction` flows are journaled too" in plan
