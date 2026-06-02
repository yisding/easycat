from __future__ import annotations

import pytest

from scripts.regen_teaching_chapters import TEACHING, Chapter, _resolve_child_path, render_diff


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
