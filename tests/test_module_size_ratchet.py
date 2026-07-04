"""Module line-budget ratchet guard.

Large modules degrade agent edit accuracy, so this guard freezes their growth.
Any ``src/easycat/**/*.py`` module over the line budget must appear in the
``ALLOWLIST`` below at or under its recorded count.

Golden rule for ``ALLOWLIST``: shrink this dict, never grow it. The dict may
only lose entries or lower a recorded count -- it may never add a path or raise
a count. When a file legitimately drops to the budget or fewer lines, delete
its entry rather than track every intermediate shrink. See
``docs/architecture.md`` for the documented split seams that let these modules
shrink without breaking their public imports.
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
# Shrink this dict, never grow it. Delete an entry once the file is back within
# the line budget. See docs/architecture.md for the documented split seams.
ALLOWLIST: dict[str, int] = {
    "cli/debug/bundles.py": 2445,
    "config/easy.py": 1059,
    "debugger/server.py": 3006,
    "integrations/agents/langgraph.py": 1579,
    "integrations/agents/llama_agents.py": 1372,
    "integrations/agents/pydantic_ai.py": 1035,
    "runtime/journal_sql.py": 1029,
    "server/voice_server.py": 1059,
    "session/_session.py": 1504,
    "transports/twilio_media.py": 1368,
    "transports/webrtc.py": 1638,
    "transports/webtransport.py": 1660,
    "validation/runner.py": 1734,
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
                    f"{budget}. Split it per docs/architecture.md; never raise the "
                    "allowlist count."
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
