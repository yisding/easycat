"""Location-free inventory of synchronous turn-liveness predicates."""

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


@dataclass(frozen=True, order=True, slots=True)
class TurnPredicateSite:
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
    def from_record(cls, record: str) -> TurnPredicateSite:
        category, path, qualname, construct, ast_hash, occurrence = record.split("\t")
        return cls(category, path, qualname, construct, ast_hash, int(occurrence))


@dataclass(frozen=True, slots=True)
class TurnPredicateFinding:
    site: TurnPredicateSite
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


def scan_turn_predicates(source_root: Path) -> list[TurnPredicateFinding]:
    candidates: list[_Candidate] = []
    for relative_path in sorted(TARGETS):
        path = source_root / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _TurnPredicateVisitor(relative_path)
        visitor.visit(tree)
        candidates.extend(visitor.candidates)

    occurrences: Counter[tuple[str, str, str, str, str]] = Counter()
    findings: list[TurnPredicateFinding] = []
    for candidate in sorted(candidates, key=_candidate_sort_key):
        identity = candidate.identity
        occurrence = occurrences[identity]
        occurrences[identity] += 1
        findings.append(
            TurnPredicateFinding(
                site=TurnPredicateSite(
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
    expected: set[TurnPredicateSite],
    actual: set[TurnPredicateSite],
) -> tuple[list[TurnPredicateSite], list[TurnPredicateSite]]:
    return sorted(actual - expected), sorted(expected - actual)


def format_delta(
    added: list[TurnPredicateSite],
    removed: list[TurnPredicateSite],
    *,
    findings: Sequence[TurnPredicateFinding] = (),
) -> str:
    lines = {finding.site: finding.line for finding in findings}
    sections: list[str] = []
    if added:
        sections.append(
            "new turn-predicate sites:\n  "
            + "\n  ".join(_format_site(site, line=lines.get(site)) for site in added)
        )
    if removed:
        sections.append(
            "removed or structurally changed turn-predicate sites:\n  "
            + "\n  ".join(_format_site(site) for site in removed)
        )
    return "\n".join(sections)


def _candidate_sort_key(candidate: _Candidate) -> tuple[object, ...]:
    return (*candidate.identity, candidate.line)


def _format_site(site: TurnPredicateSite, *, line: int | None = None) -> str:
    location = f"{site.path}:{line}" if line is not None else site.path
    return (
        f"{location} [{site.category}] {site.qualname} {site.construct} "
        f"{site.ast_hash}#{site.occurrence}"
    )


class _TurnPredicateVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.candidates: list[_Candidate] = []
        self._scope: list[str] = []

    @property
    def qualname(self) -> str:
        return ".".join(self._scope) or "<module>"

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node.name, node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node.name, node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node.name, node)

    def visit_Compare(self, node: ast.Compare) -> None:
        expression = ast.unparse(node)
        category = _compare_category(expression)
        if category is not None:
            self._record(category, expression, node)
        # Relevant comparison attributes are classified as one atomic site;
        # do not recurse and double-count their component reads.

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "is_cancelled":
            self._record("token_cancellation_predicate", ast.unparse(node), node)
        self.generic_visit(node)

    def _visit_scope(
        self,
        name: str,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        self._scope.append(name)
        self.generic_visit(node)
        self._scope.pop()

    def _record(self, category: str, construct: str, surrounding: ast.AST) -> None:
        normalized = ast.dump(surrounding, annotate_fields=True, include_attributes=False)
        ast_hash = hashlib.sha256(normalized.encode()).hexdigest()[:16]
        self.candidates.append(
            _Candidate(
                category=category,
                path=self.relative_path,
                qualname=self.qualname,
                construct=construct,
                ast_hash=ast_hash,
                line=getattr(surrounding, "lineno", 0),
            )
        )


def _compare_category(expression: str) -> str | None:
    if "_preemptive_finalized_generation" in expression:
        return "phase_latch_predicate"
    if "_turn_manager.state" in expression:
        return "activity_state_predicate"
    if ".generation" in expression or "_turn.generation" in expression:
        return "identity_generation_predicate"
    if "_turn.current" in expression or "_current_turn()" in expression:
        return "identity_pointer_predicate"
    if "cancel_token" in expression or ".token" in expression:
        return "token_ownership_predicate"
    if "_no_turn" in expression or _is_optional_turn_comparison(expression):
        return "null_object_predicate"
    return None


def _is_optional_turn_comparison(expression: str) -> bool:
    return expression in {
        "turn is None",
        "turn is not None",
        "st.turn is None",
        "st.turn is not None",
    }
