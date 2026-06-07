from __future__ import annotations

import re

import pytest

from scripts.regen_teaching_chapters import (
    ROOT,
    TEACHING,
    Chapter,
    _resolve_child_path,
    discover_chapters,
    regen_readme,
    render_diff,
)

SOURCE_PATH_RE = re.compile(
    r"`(?P<path>src/easycat/[A-Za-z0-9_./-]+\.py)(?:::[A-Za-z_][A-Za-z0-9_]*)?`"
)


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


def test_teaching_plan_source_path_mentions_resolve() -> None:
    """Keep teaching-plan code-span source pointers from drifting after refactors."""
    docs = sorted((ROOT / "docs" / "teaching").rglob("*.md"))
    plans = sorted((ROOT / "plan" / "teaching" / "chapter-plans").glob("*.md"))
    missing: list[str] = []

    for doc in docs + plans:
        for line_number, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            for match in SOURCE_PATH_RE.finditer(line):
                path_text = match.group("path")
                if not (ROOT / path_text).exists():
                    rel = doc.relative_to(ROOT).as_posix()
                    missing.append(f"{rel}:{line_number}: `{path_text}`")

    assert not missing, "Teaching docs reference missing source files:\n" + "\n".join(missing)


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
