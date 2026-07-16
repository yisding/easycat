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
    EXERCISE_COMPLETION_RE,
    EXERCISE_HINTS_RE,
    EXERCISE_PROTOCOL_RE,
    NAVIGATION_RE,
    OFFLINE_CHECKPOINT_RE,
    PRACTICE_HANDOFF_RE,
    PROGRESS_WORKSHEET,
    ROOT,
    SELF_CHECK_PROTOCOL_RE,
    TEACHING,
    Chapter,
    _ensure_exercise_completion,
    _ensure_exercise_hints,
    _ensure_navigation,
    _ensure_practice_handoff,
    _offline_checkpoint_for,
    _resolve_child_path,
    discover_chapters,
    regen_exercises,
    regen_readme,
    render_diff,
    render_exercise_completion,
    render_exercise_hints,
    render_exercise_navigation,
    render_exercise_protocol,
    render_navigation,
    render_offline_checkpoint,
    render_practice_handoff,
    render_progress_worksheet,
    render_self_check_protocol,
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


def test_progress_worksheet_tracks_every_chapter_and_checkpoint() -> None:
    worksheet = PROGRESS_WORKSHEET.read_text(encoding="utf-8")
    normalized_worksheet = re.sub(r"\s+", " ", worksheet)

    assert worksheet == render_progress_worksheet()
    assert worksheet.count("## Chapter ") == 16
    labels = ("Predict", "Prepare", "Run", "Find", "Reflect", "Practice", "Retrieve", "Replay")
    for label in labels:
        assert worksheet.count(f"- [ ] **{label}:**") == 16
    assert worksheet.count("- [ ]") == 130
    assert "- [x]" not in worksheet.lower()
    for chapter in discover_chapters():
        checkpoint = _offline_checkpoint_for(chapter)
        assert f"[Narrative](./{chapter.slug}/)" in worksheet
        assert f"[Exercises](./{chapter.slug}/EXERCISES.md)" in worksheet
        assert str(checkpoint["prediction"]) in normalized_worksheet
        assert str(checkpoint["setup_command"]) in worksheet
        assert str(checkpoint["command"]) in worksheet
        assert str(checkpoint["evidence"]) in normalized_worksheet
        assert str(checkpoint["reflection"]) in normalized_worksheet
        assert f"--through {checkpoint['chapter']} --jobs 4 --show-evidence" in worksheet


def test_render_navigation_handles_first_middle_and_last_chapters() -> None:
    chapters = discover_chapters()

    assert render_navigation(chapters[0]) == (
        "**Progress: 1 of 16** · [Ladder index](../) · "
        "[Exercises](./EXERCISES.md) · [Chapter 1 — Echo →](../01-echo/)"
    )
    assert render_navigation(chapters[8]) == (
        "**Progress: 9 of 16** · "
        "[← Chapter 7 — Tools, Mid-stream](../07-tools/) · "
        "[Ladder index](../) · [Exercises](./EXERCISES.md) · "
        "[Chapter 9 — Interruption / Barge-in →](../09-interruption/)"
    )
    assert render_navigation(chapters[-1]) == (
        "**Progress: 16 of 16** · "
        "[← Chapter 14 — Bring your own agent](../14-bring-your-own-agent/) · "
        "[Ladder index](../) · [Exercises](./EXERCISES.md)"
    )


def test_missing_navigation_is_inserted_immediately_after_h1() -> None:
    chapter = discover_chapters()[0]

    updated = _ensure_navigation(
        chapter,
        "# Temporary chapter\n\nIntro text.\n",
        render_navigation(chapter),
    )

    assert updated.startswith("# Temporary chapter\n\n<!-- BEGIN auto:navigation -->")
    assert updated.count("<!-- BEGIN auto:navigation -->") == 1
    assert updated.count("<!-- END auto:navigation -->") == 1
    assert updated.index("<!-- END auto:navigation -->") < updated.index("Intro text.")


def test_teaching_readmes_have_one_generated_navigation_block() -> None:
    missing_or_duplicated: list[str] = []

    for chapter in discover_chapters():
        readme = chapter.path / "README.md"
        text = readme.read_text(encoding="utf-8")
        if text.count("<!-- BEGIN auto:navigation -->") != 1:
            missing_or_duplicated.append(chapter.slug)
        if text.count("<!-- END auto:navigation -->") != 1:
            missing_or_duplicated.append(chapter.slug)

    assert not missing_or_duplicated, (
        "Teaching chapter navigation markers are missing or duplicated: "
        + ", ".join(sorted(set(missing_or_duplicated)))
    )


def test_each_chapter_has_one_current_generated_navigation_block() -> None:
    for chapter in discover_chapters():
        readme = (chapter.path / "README.md").read_text(encoding="utf-8")
        matches = list(NAVIGATION_RE.finditer(readme))

        assert len(matches) == 1, chapter.slug
        assert matches[0].group("body").strip() == render_navigation(chapter).strip()
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
        "\n> **Hardware-free checkpoint:** prove `blocking first-audio gap` without "
        "a microphone,\n"
        "> speakers, or provider credentials:\n"
        ">\n"
        "> **Predict first:** Which sub-gap dominates first-audio latency, and does full TTS "
        "enqueue\n"
        "> define when the user first hears audio?\n"
        ">\n"
        "> ```bash\n"
        "> uv run python docs/teaching/05-blocking-agent/gap_decomposition_probe.py\n"
        "> ```\n"
        ">\n"
        "> **Evidence to find:** 1,200 ms agent plus 450 ms TTS equals 1,650 ms total; "
        "full enqueue takes\n"
        "> 800 ms.\n"
        ">\n"
        "> **Explain the result:** Point to the milestone that defines first audio and explain "
        "why full\n"
        "> enqueue is not it.\n"
        ">\n"
        "> [See all 16 checkpoints](../#hardware-free-checkpoint-spine).\n"
    )


def test_each_chapter_has_one_current_generated_practice_handoff() -> None:
    for chapter in discover_chapters():
        readme = (chapter.path / "README.md").read_text(encoding="utf-8")
        matches = list(PRACTICE_HANDOFF_RE.finditer(readme))
        closing_heading = re.search(
            r"^## (?:What's next|The ladder, complete \(really\))$",
            readme,
            re.MULTILINE,
        )

        assert len(matches) == 1, chapter.slug
        assert matches[0].group("body").strip() == render_practice_handoff()
        assert matches[0].start() > readme.index("## Try breaking it")
        assert closing_heading is not None
        assert matches[0].end() < closing_heading.start()


def test_practice_handoff_is_moved_before_the_closing_section() -> None:
    chapter = discover_chapters()[0]
    misplaced = (
        "<!-- BEGIN auto:practice-handoff -->\n"
        f"{render_practice_handoff()}\n"
        "<!-- END auto:practice-handoff -->"
    )
    text = (
        f"# Temporary chapter\n\n{misplaced}\n\n"
        "## Try breaking it\n\nTry this.\n\n"
        "## What's next\n\nContinue.\n"
    )

    updated = _ensure_practice_handoff(chapter, text)

    assert updated.count("<!-- BEGIN auto:practice-handoff -->") == 1
    assert updated.index("## Try breaking it") < updated.index(misplaced)
    assert updated.index(misplaced) < updated.index("## What's next")


def test_render_exercise_navigation_handles_first_middle_and_last_chapters() -> None:
    chapters = discover_chapters()

    assert render_exercise_navigation(chapters[0]) == (
        "[← Back to chapter](./README.md) · "
        "[Ladder index](../) · "
        "[Chapter 1 — Echo →](../01-echo/)"
    )
    assert render_exercise_navigation(chapters[8]) == (
        "[← Back to chapter](./README.md) · "
        "[Ladder index](../) · "
        "[Chapter 9 — Interruption / Barge-in →](../09-interruption/)"
    )
    assert render_exercise_navigation(chapters[-1]) == (
        "[← Back to chapter](./README.md) · [Ladder index](../)"
    )


def test_render_exercise_protocol_defines_completion_evidence() -> None:
    assert render_exercise_protocol() == (
        "> **Completion evidence for every task**\n"
        ">\n"
        "> 1. **Before hints:** keep your initial prediction or plan.\n"
        "> 2. **After the attempt:** keep the exact command or change and one observed field,\n"
        ">    measurement, or behavior.\n"
        "> 3. **Before moving on:** explain in one sentence why the evidence supports or changes\n"
        ">    your model.\n"
        ">\n"
        "> A task is complete when all three are present. Keep a wrong first answer visible;\n"
        "> it is evidence to explain after revealing hints, not an answer to rewrite."
    )


def test_render_self_check_protocol_requires_evidence_backed_retrieval() -> None:
    assert render_self_check_protocol() == (
        "> **Closed-book retrieval gate**\n"
        ">\n"
        "> 1. Close the chapter narrative and every hint disclosure.\n"
        "> 2. Answer each outcome below from memory, aloud or in writing.\n"
        "> 3. Support the answer with at least one observed field, measurement, or behavior\n"
        ">    from your attempt record.\n"
        ">\n"
        "> If an answer needs notes, reopen only the section that owns the weak concept,\n"
        "> correct your explanation, close it, and retry. Continue only when you can answer\n"
        "> without looking."
    )


def test_render_exercise_completion_handles_first_middle_and_last_chapters() -> None:
    chapters = discover_chapters()

    assert render_exercise_completion(chapters[0]) == (
        "---\nSelf-check complete? Prepare the cumulative spine, then replay it through "
        "this chapter:\n\n"
        "```bash\n"
        "uv sync --extra local --group dev\n"
        "uv run python docs/teaching/offline_spine.py --run "
        "--through 0 --jobs 4 --show-evidence\n"
        "```\n\n"
        "- [Review the chapter narrative](./README.md)\n"
        "- [Continue to Chapter 1 — Echo →](../01-echo/)"
    )
    assert render_exercise_completion(chapters[8]) == (
        "---\nSelf-check complete? Prepare the cumulative spine, then replay it through "
        "this chapter:\n\n"
        "```bash\n"
        "uv sync --extra quickstart --group dev\n"
        "uv run python docs/teaching/offline_spine.py --run "
        "--through 8 --jobs 4 --show-evidence\n"
        "```\n\n"
        "- [Review the chapter narrative](./README.md)\n"
        "- [Continue to Chapter 9 — Interruption / Barge-in →](../09-interruption/)"
    )
    assert render_exercise_completion(chapters[-1]) == (
        "---\nSelf-check complete? Prepare the cumulative spine, then replay it through "
        "this chapter:\n\n"
        "```bash\n"
        "uv sync --extra quickstart --group dev\n"
        "uv run python docs/teaching/offline_spine.py --run "
        "--through 15 --jobs 4 --show-evidence\n"
        "```\n\n"
        "- [Review the chapter narrative](./README.md)\n"
        "- [Return to the teaching ladder](../)"
    )


def test_each_exercise_page_has_one_current_generated_navigation_block() -> None:
    for chapter in discover_chapters():
        exercises = (chapter.path / "EXERCISES.md").read_text(encoding="utf-8")
        matches = list(NAVIGATION_RE.finditer(exercises))

        assert len(matches) == 1, chapter.slug
        assert matches[0].group("body").strip() == render_exercise_navigation(chapter)
        assert matches[0].start() > exercises.index("# ")
        assert matches[0].start() < exercises.find("\n## ")


def test_each_exercise_page_sets_completion_evidence_before_its_first_task() -> None:
    for chapter in discover_chapters():
        exercises = (chapter.path / "EXERCISES.md").read_text(encoding="utf-8")
        matches = list(EXERCISE_PROTOCOL_RE.finditer(exercises))
        navigation = NAVIGATION_RE.search(exercises)
        first_task = re.search(r"^## (?:\d+\.|Bonus\b)", exercises, re.MULTILINE)

        assert len(matches) == 1, chapter.slug
        assert matches[0].group("body").strip() == render_exercise_protocol()
        assert navigation is not None
        assert first_task is not None
        assert navigation.end() < matches[0].start() < first_task.start()


def test_each_exercise_page_ends_with_current_generated_completion() -> None:
    for chapter in discover_chapters():
        exercises = (chapter.path / "EXERCISES.md").read_text(encoding="utf-8")
        matches = list(EXERCISE_COMPLETION_RE.finditer(exercises))

        assert len(matches) == 1, chapter.slug
        assert matches[0].group("body").strip() == render_exercise_completion(chapter)
        assert matches[0].start() > exercises.index("## Self-check")
        assert exercises.rstrip().endswith("<!-- END auto:exercise-completion -->")


def test_each_self_check_starts_with_current_closed_book_retrieval_gate() -> None:
    for chapter in discover_chapters():
        exercises = (chapter.path / "EXERCISES.md").read_text(encoding="utf-8")
        matches = list(SELF_CHECK_PROTOCOL_RE.finditer(exercises))
        heading = re.search(r"^## Self-check$", exercises, re.MULTILINE)

        assert len(matches) == 1, chapter.slug
        assert matches[0].group("body").strip() == render_self_check_protocol()
        assert heading is not None
        assert not exercises[heading.end() : matches[0].start()].strip()
        assert matches[0].end() < exercises.index("<!-- BEGIN auto:exercise-completion -->")


def test_exercise_hint_wrapper_preserves_content_and_is_idempotent() -> None:
    source = (
        "## 1. Try it\n\n"
        "**Task.** Make a prediction.\n\n"
        "**Hints**\n\n"
        "1. First clue.\n"
        "2. Second clue.\n\n"
        "## Self-check\n\n"
        "Recall the result.\n"
    )

    wrapped = _ensure_exercise_hints(source)

    assert render_exercise_hints("1. First clue.\n2. Second clue.") in wrapped
    assert '<details markdown="1">' in wrapped
    assert "<summary>Reveal hints after your first attempt</summary>" in wrapped
    assert wrapped.index("<!-- END auto:exercise-hints -->") < wrapped.index("## Self-check")
    assert _ensure_exercise_hints(wrapped) == wrapped


def test_every_exercise_hint_is_concealed_until_after_an_attempt() -> None:
    for chapter in discover_chapters():
        exercises = (chapter.path / "EXERCISES.md").read_text(encoding="utf-8")
        matches = list(EXERCISE_HINTS_RE.finditer(exercises))

        assert matches, chapter.slug
        assert len(matches) == exercises.count("**Hints**"), chapter.slug
        for match in matches:
            assert match.group("body").strip(), chapter.slug
            assert "Reveal hints after your first attempt" in match.group(0)


def test_exercise_completion_is_moved_after_the_self_check() -> None:
    chapter = discover_chapters()[0]
    misplaced = (
        "<!-- BEGIN auto:exercise-completion -->\n"
        f"{render_exercise_completion(chapter)}\n"
        "<!-- END auto:exercise-completion -->"
    )
    text = f"# Exercises\n\n{misplaced}\n\n## Self-check\n\nAnswer from memory.\n"

    updated = _ensure_exercise_completion(chapter, text)

    assert updated.count("<!-- BEGIN auto:exercise-completion -->") == 1
    assert updated.index("## Self-check") < updated.index(misplaced)
    assert updated.rstrip().endswith("<!-- END auto:exercise-completion -->")


def test_teaching_exercises_match_regenerated_auto_blocks() -> None:
    stale_exercises: list[str] = []

    for chapter in discover_chapters():
        exercises = chapter.path / "EXERCISES.md"
        if not exercises.exists():
            continue
        original, updated = regen_exercises(chapter)
        if original != updated:
            stale_exercises.append(exercises.relative_to(ROOT).as_posix())

    assert not stale_exercises, (
        "Teaching exercise auto blocks are stale. Run "
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
