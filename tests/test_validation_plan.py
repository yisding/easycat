from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TASK_HEADING_RE = re.compile(r"^### (?P<title>V\d+\.\d+ .+)$", re.MULTILINE)


def _validation_task_sections() -> dict[str, str]:
    text = (REPO_ROOT / "plan/validation/tasks.md").read_text(encoding="utf-8")
    matches = list(TASK_HEADING_RE.finditer(text))
    sections: dict[str, str] = {}

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group("title")] = text[match.start() : end]

    assert sections, "plan/validation/tasks.md did not contain validation task sections"
    return sections


def test_completed_validation_plan_tasks_include_current_verified_state() -> None:
    missing = [
        title
        for title, section in _validation_task_sections().items()
        if re.search(r"^Status: completed\b", section, flags=re.MULTILINE)
        and "Current verified state:" not in section
    ]

    assert not missing, (
        "Completed validation tasks must record their current verified state: "
        + ", ".join(missing)
    )
