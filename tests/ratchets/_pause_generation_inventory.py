"""Location-free inventory of pause-generation ownership and correlation."""

from __future__ import annotations

import ast
import hashlib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

TARGETS = frozenset({"session/_stt_committer.py", "turn_manager.py"})


@dataclass(frozen=True, order=True, slots=True)
class PauseGenerationSite:
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
    def from_record(cls, record: str) -> PauseGenerationSite:
        category, path, qualname, construct, ast_hash, occurrence = record.split("\t")
        return cls(category, path, qualname, construct, ast_hash, int(occurrence))


@dataclass(frozen=True, slots=True)
class PauseGenerationFinding:
    site: PauseGenerationSite
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


def scan_pause_generation(source_root: Path) -> list[PauseGenerationFinding]:
    candidates: list[_Candidate] = []
    for relative_path in sorted(TARGETS):
        path = source_root / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _PauseGenerationVisitor(relative_path)
        visitor.visit(tree)
        candidates.extend(visitor.candidates)

    occurrences: Counter[tuple[str, str, str, str, str]] = Counter()
    findings: list[PauseGenerationFinding] = []
    for candidate in sorted(candidates, key=_candidate_sort_key):
        identity = candidate.identity
        occurrence = occurrences[identity]
        occurrences[identity] += 1
        findings.append(
            PauseGenerationFinding(
                site=PauseGenerationSite(
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
    expected: set[PauseGenerationSite],
    actual: set[PauseGenerationSite],
) -> tuple[list[PauseGenerationSite], list[PauseGenerationSite]]:
    return sorted(actual - expected), sorted(expected - actual)


def format_delta(
    added: list[PauseGenerationSite],
    removed: list[PauseGenerationSite],
    *,
    findings: Sequence[PauseGenerationFinding] = (),
) -> str:
    lines = {finding.site: finding.line for finding in findings}
    sections: list[str] = []
    if added:
        sections.append(
            "new pause-generation sites:\n  "
            + "\n  ".join(_format_site(site, line=lines.get(site)) for site in added)
        )
    if removed:
        sections.append(
            "removed or structurally changed pause-generation sites:\n  "
            + "\n  ".join(_format_site(site) for site in removed)
        )
    return "\n".join(sections)


def _candidate_sort_key(candidate: _Candidate) -> tuple[object, ...]:
    return (*candidate.identity, candidate.line)


def _format_site(site: PauseGenerationSite, *, line: int | None = None) -> str:
    location = f"{site.path}:{line}" if line is not None else site.path
    return (
        f"{location} [{site.category}] {site.qualname} {site.construct} "
        f"{site.ast_hash}#{site.occurrence}"
    )


class _PauseGenerationVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.candidates: list[_Candidate] = []
        self._scope: list[str] = []

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

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._record_assignment(target, node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._record_assignment(node.target, node)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if _expression_path(node.target) == "self._pause_generation":
            self._record("owner_write", "increment self._pause_generation", node)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            path = _expression_path(node)
            if path == "self._pause_generation":
                self._record("owner_read", path, node)
            elif path.endswith(".pause_generation"):
                self._record("api_read", path, node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "pop"
            and _expression_path(node.func.value) == "self._pause_generation_by_future"
        ):
            self._record("future_map_take", "pop future correlation", node)
        self.generic_visit(node)

    def visit_keyword(self, node: ast.keyword) -> None:
        if node.arg == "pause_generation":
            self._record("generation_handoff", "keyword pause_generation", node)
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._scope.append(node.name)
        for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            if argument.arg == "pause_generation":
                self._record("generation_receiver", "parameter pause_generation", argument)
        self.generic_visit(node)
        self._scope.pop()

    def _record_assignment(self, target: ast.AST, surrounding: ast.AST) -> None:
        path = _expression_path(target)
        if path == "self._pause_generation":
            self._record("owner_write", "assign self._pause_generation", surrounding)
        elif path == "self._pause_generation_by_future":
            self._record("future_map_owner", "assign future correlation map", surrounding)
        elif (
            isinstance(target, ast.Subscript)
            and _expression_path(target.value) == "self._pause_generation_by_future"
        ):
            self._record("future_map_write", "store future correlation", surrounding)

    def _record(self, category: str, construct: str, node: ast.AST) -> None:
        normalized = ast.dump(node, annotate_fields=True, include_attributes=False)
        self.candidates.append(
            _Candidate(
                category=category,
                path=self.relative_path,
                qualname=self.qualname,
                construct=construct,
                ast_hash=hashlib.sha256(normalized.encode()).hexdigest()[:16],
                line=getattr(node, "lineno", 0),
            )
        )


def _expression_path(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _expression_path(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ast.unparse(node)
