"""Location-free inventory of turn-scoped commit effects around suspensions."""

from __future__ import annotations

import ast
import hashlib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

TARGETS = frozenset(
    {
        "session/_stt_committer.py",
        "session/_tts_scheduler.py",
        "session/_turn_runner.py",
    }
)

_CALL_CATEGORIES = {
    "_clear_turn": "identity_commit",
    "_reset_turn_state": "identity_commit",
    "_reset_turn_manager_preserving_token": "activity_commit",
    "begin_application_turn": "activity_commit",
    "bot_started_speaking": "activity_commit",
    "bot_stopped_speaking": "activity_commit",
    "_start_segment_commit": "provider_commit",
    "begin_synthesis_with_bot_start": "provider_commit",
    "commit_segment": "provider_commit",
    "finalize_speaking_turn": "provider_commit",
    "prepare_preemptive": "provider_commit",
    "run_streaming_agent": "provider_commit",
    "synthesize": "provider_commit",
    "_emit": "public_observation_commit",
    "_emit_text_tool_event": "public_observation_commit",
    "_emit_turn_started_observation": "public_observation_commit",
    "emit_tool_event": "public_observation_commit",
    "_stop": "session_lifecycle_commit",
}

_TURN_FIELD_COMMITS = frozenset(
    {
        "end_time",
        "first_tts_audio_time",
        "stt_final_time",
        "stt_has_uncommitted_audio",
    }
)


@dataclass(frozen=True, order=True, slots=True)
class TurnCommitSite:
    category: str
    path: str
    qualname: str
    construct: str
    ast_hash: str
    occurrence: int

    def as_record(self) -> str:
        return "\t".join(
            (
                self.category,
                self.path,
                self.qualname,
                self.construct,
                self.ast_hash,
                str(self.occurrence),
            )
        )

    @classmethod
    def from_record(cls, record: str) -> TurnCommitSite:
        category, path, qualname, construct, ast_hash, occurrence = record.split("\t")
        return cls(category, path, qualname, construct, ast_hash, int(occurrence))


@dataclass(frozen=True, slots=True)
class TurnCommitFinding:
    site: TurnCommitSite
    line: int


@dataclass(frozen=True, slots=True)
class _Candidate:
    category: str
    path: str
    qualname: str
    construct: str
    ast_hash: str
    line: int

    @property
    def identity(self) -> tuple[str, str, str, str, str]:
        return (self.category, self.path, self.qualname, self.construct, self.ast_hash)


def scan_turn_commits(source_root: Path) -> list[TurnCommitFinding]:
    candidates: list[_Candidate] = []
    for relative_path in sorted(TARGETS):
        path = source_root / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = _parent_map(tree)
        visitor = _TurnCommitVisitor(relative_path, parents=parents)
        visitor.visit(tree)
        candidates.extend(visitor.candidates)

    occurrences: Counter[tuple[str, str, str, str, str]] = Counter()
    findings: list[TurnCommitFinding] = []
    for candidate in sorted(candidates, key=_candidate_sort_key):
        identity = candidate.identity
        occurrence = occurrences[identity]
        occurrences[identity] += 1
        findings.append(
            TurnCommitFinding(
                site=TurnCommitSite(
                    category=candidate.category,
                    path=candidate.path,
                    qualname=candidate.qualname,
                    construct=candidate.construct,
                    ast_hash=candidate.ast_hash,
                    occurrence=occurrence,
                ),
                line=candidate.line,
            )
        )
    return findings


def inventory_delta(
    expected: set[TurnCommitSite],
    actual: set[TurnCommitSite],
) -> tuple[list[TurnCommitSite], list[TurnCommitSite]]:
    return sorted(actual - expected), sorted(expected - actual)


def format_delta(
    added: list[TurnCommitSite],
    removed: list[TurnCommitSite],
    *,
    findings: Sequence[TurnCommitFinding] = (),
) -> str:
    lines = {finding.site: finding.line for finding in findings}
    sections: list[str] = []
    if added:
        sections.append(
            "new turn commit sites:\n  "
            + "\n  ".join(_format_site(site, line=lines.get(site)) for site in added)
        )
    if removed:
        sections.append(
            "removed or structurally changed turn commit sites:\n  "
            + "\n  ".join(_format_site(site) for site in removed)
        )
    return "\n".join(sections)


def _candidate_sort_key(candidate: _Candidate) -> tuple[object, ...]:
    return (*candidate.identity, candidate.line)


def _format_site(site: TurnCommitSite, *, line: int | None = None) -> str:
    location = f"{site.path}:{line}" if line is not None else site.path
    return (
        f"{location} [{site.category}] {site.qualname} {site.construct} "
        f"{site.ast_hash}#{site.occurrence}"
    )


def _parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    return {
        id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
    }


class _TurnCommitVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str, *, parents: dict[int, ast.AST]) -> None:
        self.relative_path = relative_path
        self.parents = parents
        self.candidates: list[_Candidate] = []
        self._scope: list[str] = []
        self._await_lines: list[tuple[int, ...]] = []

    @property
    def qualname(self) -> str:
        return ".".join(self._scope) or "<module>"

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:
        callee = _expression_path(node.func)
        leaf = callee.rsplit(".", 1)[-1]
        category = _CALL_CATEGORIES.get(leaf)
        if callee in {"self._turn.begin", "self._turn.set"}:
            category = "identity_commit"
        if category is not None:
            self._record(category, f"call {callee}", node)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._record_assignment(target, node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._record_assignment(node.target, node)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._record_assignment(node.target, node)
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        collector = _AwaitLineCollector()
        for statement in node.body:
            collector.visit(statement)
        self._scope.append(node.name)
        self._await_lines.append(tuple(sorted(collector.lines)))
        self.generic_visit(node)
        self._await_lines.pop()
        self._scope.pop()

    def _record_assignment(self, target: ast.AST, surrounding: ast.AST) -> None:
        path = _expression_path(target)
        leaf = path.rsplit(".", 1)[-1]
        if leaf == "_preemptive_finalized_generation" and not self.qualname.endswith(".__init__"):
            self._record("phase_latch_commit", f"assign {path}", surrounding)
        elif leaf in _TURN_FIELD_COMMITS and ("turn" in path or path.startswith("self._turn")):
            self._record("turn_field_commit", f"assign {path}", surrounding)

    def _record(self, category: str, effect: str, node: ast.AST) -> None:
        suspension = self._suspension_kind(node)
        normalized = ast.dump(node, annotate_fields=True, include_attributes=False)
        ast_hash = hashlib.sha256(normalized.encode()).hexdigest()[:16]
        self.candidates.append(
            _Candidate(
                category=category,
                path=self.relative_path,
                qualname=self.qualname,
                construct=f"{suspension} {effect}",
                ast_hash=ast_hash,
                line=getattr(node, "lineno", 0),
            )
        )

    def _suspension_kind(self, node: ast.AST) -> str:
        current: ast.AST | None = node
        while current is not None:
            if isinstance(current, ast.Await):
                return "awaited"
            current = self.parents.get(id(current))
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                break
        line = getattr(node, "lineno", 0)
        if self._await_lines and any(await_line < line for await_line in self._await_lines[-1]):
            return "post_await"
        return "synchronous"


class _AwaitLineCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.lines: list[int] = []

    def visit_Await(self, node: ast.Await) -> None:
        self.lines.append(node.lineno)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return None


def _expression_path(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _expression_path(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ast.unparse(node)
