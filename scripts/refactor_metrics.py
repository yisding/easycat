"""Generate the pre-registered bug-resistant refactor outcome report."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPORT_SCHEMA_VERSION = 1
_RESULTS = {"pass", "fail", "insufficient_data"}


@dataclass(frozen=True, slots=True)
class ChangedFile:
    path: str
    additions: int
    deletions: int

    @property
    def changed_lines(self) -> int:
        return self.additions + self.deletions


@dataclass(frozen=True, slots=True)
class CommitRecord:
    sha: str
    committed_at: datetime
    files: tuple[ChangedFile, ...] = ()


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include a UTC offset: {value!r}")
    return parsed.astimezone(UTC)


def format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_first_parent_history(repo: Path) -> list[CommitRecord]:
    """Load normalized first-parent commits and numeric file churn from Git."""
    completed = subprocess.run(
        [
            "git",
            "log",
            "--first-parent",
            "--no-renames",
            "--format=%x1e%H%x1f%cI",
            "--numstat",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    records: list[CommitRecord] = []
    for raw_record in completed.stdout.split("\x1e"):
        raw_record = raw_record.strip()
        if not raw_record:
            continue
        header, *numstat_lines = raw_record.splitlines()
        sha, committed_at = header.split("\x1f", maxsplit=1)
        files: list[ChangedFile] = []
        for line in numstat_lines:
            parts = line.split("\t", maxsplit=2)
            if len(parts) != 3:
                continue
            additions_raw, deletions_raw, path = parts
            additions = int(additions_raw) if additions_raw.isdigit() else 0
            deletions = int(deletions_raw) if deletions_raw.isdigit() else 0
            files.append(ChangedFile(path=path, additions=additions, deletions=deletions))
        records.append(
            CommitRecord(
                sha=sha,
                committed_at=parse_utc(committed_at),
                files=tuple(files),
            )
        )
    return records


def _window(start: datetime, end: datetime) -> dict[str, str]:
    return {"start": format_utc(start), "end": format_utc(end)}


def _window_contains(window: dict[str, str], committed_at: datetime) -> bool:
    return parse_utc(window["start"]) <= committed_at < parse_utc(window["end"])


def _registered_paths(entries: list[dict[str, Any]]) -> set[str]:
    return {path for entry in entries for path in entry["paths"]}


def _entry_paths(entries: list[dict[str, Any]]) -> dict[str, set[str]]:
    return {entry["id"]: set(entry["paths"]) for entry in entries}


def _touch_details(
    commit: CommitRecord,
    entries: list[dict[str, Any]],
) -> tuple[set[str], int]:
    paths_by_entry = _entry_paths(entries)
    matched_ids = {
        entry_id
        for entry_id, paths in paths_by_entry.items()
        if any(changed.path in paths for changed in commit.files)
    }
    all_paths = _registered_paths(entries)
    changed_lines = sum(
        changed.changed_lines for changed in commit.files if changed.path in all_paths
    )
    return matched_ids, changed_lines


def _sorted_commits(commits: list[CommitRecord]) -> list[CommitRecord]:
    return sorted(commits, key=lambda commit: (commit.committed_at, commit.sha))


def _review_field_reasons(
    prefix: str,
    identifier: str,
    entry: dict[str, Any],
    *,
    as_of: datetime,
) -> list[str]:
    reasons = [
        f"{prefix}_missing_{field}:{identifier}"
        for field in ("evidence", "rationale", "reviewer", "reviewed_at")
        if not entry.get(field)
    ]
    if entry.get("reviewed_at"):
        try:
            reviewed_at = parse_utc(entry["reviewed_at"])
        except (TypeError, ValueError):
            reasons.append(f"{prefix}_invalid_reviewed_at:{identifier}")
        else:
            if reviewed_at > as_of:
                reasons.append(f"{prefix}_review_after_as_of:{identifier}")
    return reasons


def _validate_classification(
    entry: dict[str, Any],
    *,
    allowed_bug_classes: set[str],
    allowed_members: set[str],
    expected_reviewer: str,
    touching: dict[str, set[str]],
    as_of: datetime,
) -> list[str]:
    sha = entry["sha"]
    classification = entry.get("classification")
    bug_classes = entry.get("bug_classes")
    affected = entry.get("affected_members")
    reasons: list[str] = []
    if classification not in {"fix", "not_fix"}:
        return [f"classification_invalid:{sha}"]
    if not isinstance(bug_classes, list) or not isinstance(affected, list):
        return [f"classification_invalid:{sha}"]
    if classification == "fix":
        if not bug_classes or not affected:
            reasons.append(f"classification_incomplete_fix:{sha}")
        if not set(bug_classes) <= allowed_bug_classes:
            reasons.append(f"classification_unknown_bug_class:{sha}")
        if not set(affected) <= allowed_members:
            reasons.append(f"classification_unknown_member:{sha}")
        if not set(affected) <= touching.get(sha, set()):
            reasons.append(f"classification_member_not_touched:{sha}")
    elif bug_classes or affected:
        reasons.append(f"classification_not_fix_has_assignments:{sha}")
    reasons.extend(_review_field_reasons("classification", sha, entry, as_of=as_of))
    if entry.get("reviewer") and entry["reviewer"] != expected_reviewer:
        reasons.append(f"classification_wrong_reviewer:{sha}")
    return reasons


def _classification_index(
    cohort: dict[str, Any],
    adjudications: dict[str, Any],
    touching: dict[str, set[str]],
    as_of: datetime,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    allowed_bug_classes = set(cohort["bug_classes"])
    allowed_members = {entry["id"] for entry in [*cohort["members"], *cohort["controls"]]}
    indexed: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []
    entries = [
        entry
        for entry in adjudications.get("commit_classifications", [])
        if entry.get("cohort_id") == cohort["id"] and entry.get("sha") in touching
    ]
    for entry in entries:
        sha = entry.get("sha")
        if not isinstance(sha, str):
            reasons.append("classification_invalid_sha")
            continue
        if sha in indexed:
            reasons.append(f"classification_duplicate:{sha}")
            continue
        indexed[sha] = entry
        reasons.extend(
            _validate_classification(
                entry,
                allowed_bug_classes=allowed_bug_classes,
                allowed_members=allowed_members,
                expected_reviewer=cohort["attribution_reviewer"],
                touching=touching,
                as_of=as_of,
            )
        )
    for sha in sorted(touching):
        if sha not in indexed:
            reasons.append(f"classification_missing:{sha}")
    return indexed, reasons


def _group_metrics(
    commits: list[CommitRecord],
    entries: list[dict[str, Any]],
    classifications: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    touched: list[tuple[CommitRecord, set[str]]] = []
    changed_lines = 0
    for commit in _sorted_commits(commits):
        matched, lines = _touch_details(commit, entries)
        if matched:
            touched.append((commit, matched))
            changed_lines += lines
    touching_shas = [commit.sha for commit, _matched in touched]
    fix_shas = [
        commit.sha
        for commit, matched in touched
        if classifications.get(commit.sha, {}).get("classification") == "fix"
        and set(classifications[commit.sha]["affected_members"]) & matched
    ]
    denominator = len(touching_shas)
    density = len(fix_shas) / denominator if denominator else None
    return {
        "touching_commits": touching_shas,
        "touching_commit_count": denominator,
        "changed_lines": changed_lines,
        "fix_commits": fix_shas,
        "fix_commit_count": len(fix_shas),
        "fix_density": round(density, 6) if density is not None else None,
        "fixes_per_kloc": round(len(fix_shas) * 1000 / changed_lines, 6)
        if changed_lines
        else None,
    }


def _candidate_id(
    cohort_id: str,
    window_name: str,
    bug_class: str,
    shas: list[str],
) -> str:
    source = "\0".join((cohort_id, window_name, bug_class, *shas)).encode()
    digest = hashlib.sha256(source).hexdigest()[:16]
    return f"recurrence-{cohort_id}-{window_name}-{bug_class}-{digest}"


def _recurrence_candidates(
    cohort: dict[str, Any],
    window_name: str,
    commits: list[CommitRecord],
    classifications: dict[str, dict[str, Any]],
    recurrence_days: int,
) -> list[dict[str, Any]]:
    member_ids = {entry["id"] for entry in cohort["members"]}
    commits_by_sha = {commit.sha: commit for commit in commits}
    candidates: list[dict[str, Any]] = []
    for bug_class in sorted(cohort["bug_classes"]):
        fixes = [
            commit
            for commit in _sorted_commits(commits)
            if classifications.get(commit.sha, {}).get("classification") == "fix"
            and bug_class in classifications[commit.sha]["bug_classes"]
            and set(classifications[commit.sha]["affected_members"]) & member_ids
        ]
        cursor = 0
        while cursor < len(fixes):
            first = fixes[cursor]
            deadline = first.committed_at + timedelta(days=recurrence_days)
            cluster = [first]
            cursor += 1
            while cursor < len(fixes) and fixes[cursor].committed_at <= deadline:
                cluster.append(fixes[cursor])
                cursor += 1
            affected = sorted(
                {
                    member
                    for commit in cluster
                    for member in classifications[commit.sha]["affected_members"]
                    if member in member_ids
                }
            )
            if len(cluster) < 2 or len(affected) < 2:
                continue
            shas = [commit.sha for commit in cluster]
            candidates.append(
                {
                    "candidate_id": _candidate_id(cohort["id"], window_name, bug_class, shas),
                    "window": window_name,
                    "bug_class": bug_class,
                    "commit_shas": shas,
                    "affected_members": affected,
                    "first_committed_at": format_utc(first.committed_at),
                    "last_committed_at": format_utc(commits_by_sha[shas[-1]].committed_at),
                    "verdict": None,
                }
            )
    return candidates


def _apply_recurrence_adjudications(
    cohort_id: str,
    expected_reviewer: str,
    candidates: list[dict[str, Any]],
    adjudications: dict[str, Any],
    as_of: datetime,
) -> list[str]:
    reasons: list[str] = []
    candidate_ids = {candidate["candidate_id"] for candidate in candidates}
    entries = [
        entry
        for entry in adjudications.get("recurrence_adjudications", [])
        if entry.get("cohort_id") == cohort_id and entry.get("candidate_id") in candidate_ids
    ]
    by_id: dict[str, dict[str, Any]] = {}
    for entry in entries:
        candidate_id = entry.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id in by_id:
            reasons.append(f"recurrence_adjudication_duplicate:{candidate_id}")
            continue
        by_id[candidate_id] = entry
    for candidate in candidates:
        candidate_id = candidate["candidate_id"]
        entry = by_id.get(candidate_id)
        if entry is None:
            reasons.append(f"recurrence_adjudication_missing:{candidate_id}")
            continue
        if (
            entry.get("bug_class") != candidate["bug_class"]
            or entry.get("commit_shas") != candidate["commit_shas"]
            or entry.get("verdict") not in {"same_fix", "not_same_fix"}
        ):
            reasons.append(f"recurrence_adjudication_mismatch:{candidate_id}")
            continue
        reasons.extend(
            _review_field_reasons(
                "recurrence_adjudication",
                candidate_id,
                entry,
                as_of=as_of,
            )
        )
        if entry.get("reviewer") and entry["reviewer"] != expected_reviewer:
            reasons.append(f"recurrence_adjudication_wrong_reviewer:{candidate_id}")
        candidate["verdict"] = entry["verdict"]
    return reasons


def _exposure_reasons(
    cohort: dict[str, Any],
    windows: dict[str, dict[str, Any]],
) -> list[str]:
    minimum = cohort["minimum_exposure"]
    reasons: list[str] = []
    checks = {
        "treated_touching_commits": ("treated", "touching_commit_count"),
        "treated_changed_lines": ("treated", "changed_lines"),
        "pooled_control_touching_commits": ("pooled_control", "touching_commit_count"),
        "pooled_control_changed_lines": ("pooled_control", "changed_lines"),
    }
    for window_name in ("pre", "post"):
        for threshold_name, (group, metric) in checks.items():
            if windows[window_name][group][metric] < minimum[threshold_name]:
                reasons.append(f"underexposed:{window_name}:{threshold_name}")
        for group in ("treated", "pooled_control"):
            if windows[window_name][group]["touching_commit_count"] == 0:
                reasons.append(f"zero_denominator:{window_name}:{group}")
    return reasons


def _pending_cohort_report(cohort: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "cohort_id": cohort["id"],
        "observation": cohort["observation"],
        "anchor_status": cohort["anchor"]["status"],
        "result": "insufficient_data",
        "reasons": [reason],
        "windows": {"pre": None, "post": None},
        "deltas": None,
        "recurrence_candidates": [],
    }


def _active_anchor_inputs(
    manifest: dict[str, Any],
    cohort: dict[str, Any],
    history: list[CommitRecord],
    as_of: datetime,
) -> tuple[dict[str, dict[str, str]], set[str], list[str]] | None:
    anchor = cohort["anchor"]
    try:
        completion = parse_utc(anchor["completion_date"])
    except (KeyError, TypeError, ValueError):
        return None
    windows = {
        "pre": _window(
            completion - timedelta(days=manifest["method"]["pre_window_days"]),
            completion,
        ),
        "post": _window(
            completion,
            completion + timedelta(days=manifest["method"]["post_window_days"]),
        ),
    }
    reasons = [
        f"anchor_{name}_window_mismatch"
        for name, window in windows.items()
        if anchor.get(f"{name}_window") != window
    ]
    history_shas = {commit.sha for commit in history}
    if anchor.get("completion_sha") not in history_shas:
        reasons.append("anchor_unreachable")
    else:
        completion_commit = next(
            commit for commit in history if commit.sha == anchor["completion_sha"]
        )
        if completion_commit.committed_at != completion:
            reasons.append("anchor_completion_date_mismatch")
    if completion > as_of:
        reasons.append("anchor_after_as_of")
    migration_shas = set(anchor.get("migration_commits", []))
    reasons.extend(
        f"migration_commit_missing:{sha}" for sha in sorted(migration_shas - history_shas)
    )
    if as_of < parse_utc(windows["post"]["end"]):
        reasons.append("post_window_open")
    reasons.extend(
        f"control_invalidated:{control['id']}:{control['invalidated_by']}"
        for control in cohort["controls"]
        if control.get("invalidated_by")
    )
    return windows, migration_shas, reasons


def _commits_in_windows(
    history: list[CommitRecord],
    windows: dict[str, dict[str, str]],
    excluded_shas: set[str],
    as_of: datetime,
) -> dict[str, list[CommitRecord]]:
    return {
        name: [
            commit
            for commit in history
            if commit.sha not in excluded_shas
            and commit.committed_at <= as_of
            and _window_contains(window, commit.committed_at)
        ]
        for name, window in windows.items()
    }


def _touching_index(
    cohort: dict[str, Any],
    window_commits: dict[str, list[CommitRecord]],
) -> dict[str, set[str]]:
    touching: dict[str, set[str]] = defaultdict(set)
    for commits in window_commits.values():
        for commit in commits:
            member_ids, _member_lines = _touch_details(commit, cohort["members"])
            control_ids, _control_lines = _touch_details(commit, cohort["controls"])
            if member_ids or control_ids:
                touching[commit.sha].update(member_ids | control_ids)
    return touching


def _window_metrics(
    cohort: dict[str, Any],
    window_commits: dict[str, list[CommitRecord]],
    windows: dict[str, dict[str, str]],
    classifications: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        name: {
            **window,
            "treated": _group_metrics(commits, cohort["members"], classifications),
            "pooled_control": _group_metrics(
                commits,
                cohort["controls"],
                classifications,
            ),
        }
        for name, commits in window_commits.items()
        for window in (windows[name],)
    }


def _cohort_recurrences(
    manifest: dict[str, Any],
    cohort: dict[str, Any],
    window_commits: dict[str, list[CommitRecord]],
    classifications: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for window_name, commits in window_commits.items():
        treated_commits = [
            commit for commit in commits if _touch_details(commit, cohort["members"])[0]
        ]
        candidates.extend(
            _recurrence_candidates(
                cohort,
                window_name,
                treated_commits,
                classifications,
                manifest["method"]["recurrence_window_days"],
            )
        )
    return candidates


def _density_deltas(
    cohort: dict[str, Any],
    windows: dict[str, dict[str, Any]],
) -> dict[str, float] | None:
    densities = {
        (window_name, group): windows[window_name][group]["fix_density"]
        for window_name in ("pre", "post")
        for group in ("treated", "pooled_control")
    }
    if any(value is None for value in densities.values()):
        return None
    return {
        "treated": round(densities[("post", "treated")] - densities[("pre", "treated")], 6),
        "pooled_control": round(
            densities[("post", "pooled_control")] - densities[("pre", "pooled_control")],
            6,
        ),
        "epsilon": cohort["non_inferiority_epsilon"],
    }


def _cohort_failure_reasons(
    windows: dict[str, dict[str, Any]],
    deltas: dict[str, float],
    candidates: list[dict[str, Any]],
) -> list[str]:
    reasons: list[str] = []
    if any(
        candidate["window"] == "post" and candidate["verdict"] == "same_fix"
        for candidate in candidates
    ):
        reasons.append("post_multi_member_recurrence")
    if deltas["treated"] > deltas["pooled_control"] + deltas["epsilon"]:
        reasons.append("non_inferiority_failed")
    pre_treated = windows["pre"]["treated"]
    post_treated = windows["post"]["treated"]
    if pre_treated["fix_density"] > 0:
        if post_treated["fix_density"] >= pre_treated["fix_density"]:
            reasons.append("treated_density_not_strictly_lower")
    elif post_treated["fix_commit_count"] != 0:
        reasons.append("zero_baseline_has_post_fixes")
    return sorted(reasons)


def _cohort_report(
    manifest: dict[str, Any],
    cohort: dict[str, Any],
    adjudications: dict[str, Any],
    history: list[CommitRecord],
    as_of: datetime,
) -> dict[str, Any]:
    anchor = cohort["anchor"]
    inactive_reason = {
        "pending": "anchor_pending",
        "blocked": "peer_decision_blocked",
    }.get(anchor["status"], "anchor_not_active")
    if anchor["status"] != "active":
        return _pending_cohort_report(cohort, inactive_reason)

    active_inputs = _active_anchor_inputs(manifest, cohort, history, as_of)
    if active_inputs is None:
        return _pending_cohort_report(cohort, "anchor_invalid_completion_date")
    registered_windows, migration_shas, reasons = active_inputs
    window_commits = _commits_in_windows(
        history,
        registered_windows,
        migration_shas,
        as_of,
    )
    touching = _touching_index(cohort, window_commits)
    classifications, classification_reasons = _classification_index(
        cohort,
        adjudications,
        touching,
        as_of,
    )
    reasons.extend(classification_reasons)
    windows = _window_metrics(cohort, window_commits, registered_windows, classifications)
    reasons.extend(_exposure_reasons(cohort, windows))
    candidates = _cohort_recurrences(manifest, cohort, window_commits, classifications)
    reasons.extend(
        _apply_recurrence_adjudications(
            cohort["id"],
            cohort["attribution_reviewer"],
            candidates,
            adjudications,
            as_of,
        )
    )
    deltas = _density_deltas(cohort, windows)
    reasons = sorted(set(reasons))
    failure_reasons = _cohort_failure_reasons(windows, deltas, candidates) if deltas else []
    if reasons:
        result = "insufficient_data"
        decision_reasons = reasons
    elif failure_reasons:
        result = "fail"
        decision_reasons = sorted(failure_reasons)
    else:
        result = "pass"
        decision_reasons = ["all_registered_conditions_passed"]
    return {
        "cohort_id": cohort["id"],
        "observation": cohort["observation"],
        "anchor_status": anchor["status"],
        "result": result,
        "reasons": decision_reasons,
        "windows": windows,
        "deltas": deltas,
        "recurrence_candidates": candidates,
    }


def build_report(
    manifest: dict[str, Any],
    adjudications: dict[str, Any],
    history: list[CommitRecord],
    *,
    as_of: datetime,
) -> dict[str, Any]:
    """Build a deterministic tri-state report from normalized inputs."""
    normalized_as_of = as_of.astimezone(UTC)
    cohorts = [
        _cohort_report(manifest, cohort, adjudications, history, normalized_as_of)
        for cohort in manifest["cohorts"]
    ]
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "as_of": format_utc(normalized_as_of),
        "cohorts": cohorts,
    }
    assert all(cohort["result"] in _RESULTS for cohort in cohorts)
    return report


def render_report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def render_report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Refactor outcome report",
        "",
        f"As of `{report['as_of']}`. Generated; do not edit by hand.",
        "",
        "This report is observational and never blocks refactor sequencing.",
        "",
        "| Cohort | Observation | Result | Decision |",
        "|---|---|---|---|",
    ]
    for cohort in report["cohorts"]:
        reasons = "; ".join(cohort["reasons"])
        lines.append(
            f"| `{cohort['cohort_id']}` | `{cohort['observation']}` | "
            f"**{cohort['result']}** | {reasons} |"
        )
    lines.extend(["", "## Cohort details", ""])
    for cohort in report["cohorts"]:
        lines.extend(
            [
                f"### {cohort['cohort_id']}",
                "",
                f"- Anchor: `{cohort['anchor_status']}`",
                f"- Result: **{cohort['result']}**",
                f"- Reasons: {', '.join(f'`{reason}`' for reason in cohort['reasons'])}",
            ]
        )
        if cohort["deltas"] is not None:
            lines.extend(
                [
                    f"- Treated density delta: `{cohort['deltas']['treated']}`",
                    f"- Pooled-control density delta: `{cohort['deltas']['pooled_control']}`",
                    f"- Epsilon: `{cohort['deltas']['epsilon']}`",
                ]
            )
        lines.append("")
    return "\n".join(lines)


def _write_or_check(path: Path, content: str, *, check: bool) -> bool:
    if check:
        return path.is_file() and path.read_text(encoding="utf-8") == content
    path.write_text(content, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    repo_default = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=repo_default)
    parser.add_argument("--as-of", required=True, help="UTC RFC 3339 observation timestamp")
    parser.add_argument("--check", action="store_true", help="fail if checked reports drift")
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    metrics_dir = repo / "plan" / "metrics"
    report = build_report(
        _load_json(metrics_dir / "refactor-families.json"),
        _load_json(metrics_dir / "adjudications.json"),
        load_first_parent_history(repo),
        as_of=parse_utc(args.as_of),
    )
    outputs = {
        metrics_dir / "report.json": render_report_json(report),
        metrics_dir / "report.md": render_report_markdown(report),
    }
    clean = all(
        _write_or_check(path, content, check=args.check) for path, content in outputs.items()
    )
    if not clean:
        parser.error("generated refactor metric reports are stale")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
