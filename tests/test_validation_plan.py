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


def test_validation_plan_index_tracks_current_cli_and_contract_state() -> None:
    index = (REPO_ROOT / "plan/validation/README.md").read_text(encoding="utf-8")
    tasks = (REPO_ROOT / "plan/validation/tasks.md").read_text(encoding="utf-8")
    current_state = index.split("## Current State", 1)[1].split(
        "## Recent Review Gaps",
        1,
    )[0]
    remaining_backlog = index.split("Remaining backlog:", 1)[1].split(
        "## Recent Review Gaps",
        1,
    )[0]
    normalized_backlog = " ".join(remaining_backlog.split())
    historical_scope = index.split("## Historical First Implementation PR", 1)[1]

    assert "Snapshot: maintenance update on 2026-06-06." in current_state
    for subcommand in ("quick", "socket", "stress", "contracts", "latency", "live", "release"):
        assert f"`{subcommand}`" in current_state
        assert f"`{subcommand}`" in tasks
    assert "`report`" in current_state
    assert "Checked-in HTTP/SSE/WebSocket proof cassettes" in normalized_backlog
    assert "schema fingerprint helper" in normalized_backlog
    assert "full generated provider cassette/schema registry coverage" in normalized_backlog
    assert "HTTP/WebSocket provider cassettes and schema drift fingerprints are still not" not in (
        remaining_backlog
    )
    for token in (
        "tests/cassettes/http/openai-stt.json",
        "tests/cassettes/sse/remote-responses-api.json",
        "tests/cassettes/ws/openai-realtime-stt.json",
        "tests/contracts/schema_fingerprints.py",
    ):
        assert token in tasks
    assert "historical first PR scope" in historical_scope
    assert "public CLI shipped" in historical_scope
    assert "public CLI already exists" not in historical_scope
