"""Module line-budget ratchet guard.

Large modules degrade agent edit accuracy, so this guard freezes their growth.
Any ``src/easycat/**/*.py`` module over the line budget must appear in the
``ALLOWLIST`` below at or under its recorded count.

Golden rule for ``ALLOWLIST``: the recorded counts are a high-water baseline
that must *trend down*. Never add a new path (split the file instead), and
never raise a count for casual churn. The one sanctioned way a count goes up is
a *reviewed* change to an already-oversized module that unavoidably adds lines
(e.g. an audited bug fix landed before the module's split); re-baseline that one
entry in the same change and keep it rare. When a file legitimately drops to the
budget or fewer lines, delete its entry rather than track every intermediate
shrink. See ``docs/architecture.md`` for the documented split seams (QS2/QS3/
QS5/QS6) that let these modules shrink without breaking their public imports.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src" / "easycat"

# Modules must stay at or below this many lines unless they are allowlisted.
LINE_BUDGET = 1000

# Generated scaffold sources are not hand-maintained modules; skip them.
EXCLUDED_PREFIX = "cli/scaffold/templates/"

# Grandfathered offenders (package-relative path -> current line count).
# Trend this dict down; delete an entry once the file is back within the line
# budget. Raise a count only for a reviewed change to an already-listed module
# that unavoidably adds lines. See docs/architecture.md for the documented split
# seams. Several counts below were re-baselined after audited Wave 1 bug fixes
# and are expected to drop sharply once the QS2/QS3/QS5/QS6 splits land.
ALLOWLIST: dict[str, int] = {
    "config/easy.py": 1074,
    "debugger/server.py": 1793,
    "integrations/agents/langgraph.py": 1588,
    "integrations/agents/llama_agents.py": 1362,
    "integrations/agents/pydantic_ai.py": 1021,
    "runtime/journal_sql.py": 1033,
    "server/voice_server.py": 1059,
    "session/_session.py": 1514,
    "transports/twilio_media.py": 1291,
    "transports/webrtc.py": 1487,
    "transports/webtransport.py": 1640,
    "validation/runner.py": 1625,
}


def _iter_source_modules() -> list[tuple[str, int]]:
    """Yield (package-relative path, line count) for maintained source modules."""
    modules: list[tuple[str, int]] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        key = path.relative_to(SOURCE_ROOT).as_posix()
        if key.startswith(EXCLUDED_PREFIX):
            continue
        count = len(path.read_text(encoding="utf-8").splitlines())
        modules.append((key, count))
    return modules


def test_no_module_exceeds_line_budget_unless_allowlisted() -> None:
    """Fail on new offenders and on any allowlisted module growing past its budget."""
    violations: list[str] = []

    for key, count in _iter_source_modules():
        if key in ALLOWLIST:
            budget = ALLOWLIST[key]
            if count > budget:
                violations.append(
                    f"{key} grew to {count} lines, over its allowlisted budget of "
                    f"{budget}. Split it per docs/architecture.md; only re-baseline "
                    "this count for a reviewed, unavoidable change to this module."
                )
            elif count <= LINE_BUDGET:
                violations.append(
                    f"{key} is now {count} lines, within the budget. Delete its "
                    "ALLOWLIST entry -- the dict may only shrink."
                )
        elif count > LINE_BUDGET:
            violations.append(
                f"{key} is {count} lines, over the budget. Split it per "
                "docs/architecture.md, or (only if unavoidable) add an ALLOWLIST "
                "entry with its exact count."
            )

    assert not violations, "Module line-budget ratchet violations:\n" + "\n".join(violations)


def test_allowlist_has_no_stale_entries() -> None:
    """Every allowlist entry must exist on disk and still be over the budget."""
    counts = dict(_iter_source_modules())
    stale: list[str] = []

    for key in sorted(ALLOWLIST):
        if key not in counts:
            stale.append(f"{key} no longer exists; remove its ALLOWLIST entry.")
        elif counts[key] <= LINE_BUDGET:
            stale.append(
                f"{key} is now {counts[key]} lines, within the budget; remove its ALLOWLIST entry."
            )

    assert not stale, "Stale ALLOWLIST entries (the dict may only shrink):\n" + "\n".join(stale)
