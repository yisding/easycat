"""AST inventory for timeout budgets that can influence lifecycle paths."""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from tests.ratchets._ast_digest import ast_digest, canonical_dump

CLASSIFICATIONS = frozenset({"configurable", "lifecycle_budget", "not_teardown", "protocol_local"})
_BUDGET_WORDS = ("deadline", "timeout")
_LIFECYCLE_WORDS = (
    "aclose",
    "cancel",
    "cleanup",
    "close",
    "disconnect",
    "drain",
    "finalize",
    "reap",
    "shutdown",
    "stop",
    "teardown",
)
_TIMEOUT_APIS = frozenset(
    {
        "asyncio.timeout",
        "asyncio.timeout_at",
        "asyncio.wait_for",
        "hard_timeout",
    }
)
_BUDGET_HELPER_LEAVES = frozenset(
    {
        "_await_with_hard_timeout",
        "_deadline_after",
        "_remaining_timeout",
        "_validate_timeout",
        "hard_timeout",
        "timeout",
        "timeout_at",
        "wait_for_owned_future",
    }
)


@dataclass(frozen=True, order=True, slots=True)
class BudgetSite:
    """One location-free budget declaration or lifecycle call site."""

    kind: str
    path: str
    qualname: str
    construct: str
    ast_hash: str
    occurrence: int

    def as_record(self) -> str:
        return "\t".join(
            (
                self.kind,
                self.path,
                self.qualname,
                self.construct,
                self.ast_hash,
                str(self.occurrence),
            )
        )

    @classmethod
    def from_record(cls, record: str) -> BudgetSite:
        kind, path, qualname, construct, ast_hash, occurrence = record.split("\t")
        return cls(kind, path, qualname, construct, ast_hash, int(occurrence))


@dataclass(frozen=True, slots=True)
class BudgetFinding:
    site: BudgetSite
    line: int


@dataclass(frozen=True, slots=True)
class _Candidate:
    kind: str
    path: str
    qualname: str
    construct: str
    ast_hash: str
    line: int

    @property
    def identity(self) -> tuple[str, str, str, str, str]:
        return (self.kind, self.path, self.qualname, self.construct, self.ast_hash)


def scan_teardown_budgets(source_root: Path) -> list[BudgetFinding]:
    """Inventory budget declarations and timeout calls in lifecycle closures."""
    candidates: list[_Candidate] = []
    for path in sorted(source_root.rglob("*.py")):
        relative_path = path.relative_to(source_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _BudgetVisitor(relative_path)
        visitor.visit(tree)
        candidates.extend(visitor.candidates)

    occurrence_counts: Counter[tuple[str, str, str, str, str]] = Counter()
    findings: list[BudgetFinding] = []
    for candidate in sorted(candidates, key=_candidate_sort_key):
        identity = candidate.identity
        occurrence = occurrence_counts[identity]
        occurrence_counts[identity] += 1
        findings.append(
            BudgetFinding(
                BudgetSite(
                    candidate.kind,
                    candidate.path,
                    candidate.qualname,
                    candidate.construct,
                    candidate.ast_hash,
                    occurrence,
                ),
                candidate.line,
            )
        )
    return findings


def inventory_delta(
    expected: set[BudgetSite],
    actual: set[BudgetSite],
) -> tuple[list[BudgetSite], list[BudgetSite]]:
    return sorted(actual - expected), sorted(expected - actual)


def format_delta(
    added: list[BudgetSite],
    removed: list[BudgetSite],
    *,
    findings: list[BudgetFinding],
) -> str:
    lines = {finding.site: finding.line for finding in findings}
    sections: list[str] = []
    if added:
        sections.append(
            "unclassified teardown-budget sites:\n  "
            + "\n  ".join(_format_site(item, line=lines.get(item)) for item in added)
        )
    if removed:
        sections.append(
            "stale teardown-budget manifest sites:\n  "
            + "\n  ".join(_format_site(item) for item in removed)
        )
    return "\n".join(sections)


def _candidate_sort_key(candidate: _Candidate) -> tuple[object, ...]:
    return (*candidate.identity, candidate.line)


def _format_site(site: BudgetSite, *, line: int | None = None) -> str:
    location = f"{site.path}:{line}" if line is not None else site.path
    return (
        f"{location} [{site.kind}] {site.qualname} {site.construct} "
        f"{site.ast_hash}#{site.occurrence}"
    )


class _BudgetVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.candidates: list[_Candidate] = []
        self._aliases: list[dict[str, str]] = [{}]
        self._qualnames: list[str] = []
        self._scope_kinds: list[str] = []
        self._statements: list[ast.stmt] = []

    def visit(self, node: ast.AST) -> object:
        if not isinstance(node, ast.stmt):
            return super().visit(node)
        self._statements.append(node)
        try:
            return super().visit(node)
        finally:
            self._statements.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name.split(".", maxsplit=1)[0]
            self._aliases[-1][bound] = alias.name

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module is None:
            return
        for alias in node.names:
            self._aliases[-1][alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        with self._scope(node.name, kind="class"):
            self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record_function_defaults(node)
        with self._scope(node.name, kind="function"):
            self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record_function_defaults(node)
        with self._scope(node.name, kind="function"):
            self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if not self._inside_function:
            self._record_named_declarations(node, node.targets)
        self.generic_visit(node)
        alias = self._alias_for_value(node.value)
        for target in node.targets:
            self._update_alias(target, alias)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if not self._inside_function:
            self._record_named_declarations(node, [node.target])
        self.generic_visit(node)
        if node.value is not None:
            self._update_alias(node.target, self._alias_for_value(node.value))

    def visit_Call(self, node: ast.Call) -> None:
        resolved = self._resolve(node.func)
        if self._in_lifecycle_closure and _is_budget_call(node, resolved):
            # The fallback label lands in the manifest, so it needs the same
            # interpreter-stable serialization as the hashes.
            construct = resolved or canonical_dump(node.func)
            self._record("lifecycle_call", f"call {construct}", node)
        self.generic_visit(node)

    @property
    def _inside_function(self) -> bool:
        return "function" in self._scope_kinds

    @property
    def _in_lifecycle_closure(self) -> bool:
        return any(_contains_word(name, _LIFECYCLE_WORDS) for name in self._qualnames)

    @contextmanager
    def _scope(self, name: str, *, kind: str) -> Iterator[None]:
        self._aliases.append(dict(self._aliases[-1]))
        self._qualnames.append(name)
        self._scope_kinds.append(kind)
        try:
            yield
        finally:
            self._scope_kinds.pop()
            self._qualnames.pop()
            self._aliases.pop()

    def _record_function_defaults(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        qualname = ".".join((*self._qualnames, node.name))
        positional = (*node.args.posonlyargs, *node.args.args)
        first_default = len(positional) - len(node.args.defaults)
        pairs = [
            (argument, default)
            for argument, default in zip(
                positional[first_default:], node.args.defaults, strict=True
            )
        ]
        pairs.extend(
            (argument, default)
            for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True)
            if default is not None
        )
        for argument, default in pairs:
            if _contains_word(argument.arg, _BUDGET_WORDS):
                self._record(
                    "config_default",
                    f"default {argument.arg}",
                    default,
                    qualname=qualname,
                    surrounding=default,
                )

    def _record_named_declarations(self, node: ast.stmt, targets: list[ast.expr]) -> None:
        for target in targets:
            for name in _assigned_leaf_names(target):
                if _contains_word(name, _BUDGET_WORDS):
                    self._record("named_constant", f"constant {name}", node)

    def _resolve(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return self._aliases[-1].get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self._resolve(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return ""

    def _alias_for_value(self, value: ast.AST) -> str | None:
        if not isinstance(value, (ast.Name, ast.Attribute)):
            return None
        return self._resolve(value) or None

    def _update_alias(self, target: ast.AST, alias: str | None) -> None:
        if not isinstance(target, ast.Name):
            return
        if alias is None:
            self._aliases[-1].pop(target.id, None)
        else:
            self._aliases[-1][target.id] = alias

    def _record(
        self,
        kind: str,
        construct: str,
        node: ast.AST,
        *,
        qualname: str | None = None,
        surrounding: ast.AST | None = None,
    ) -> None:
        if surrounding is None:
            surrounding = self._statements[-1] if self._statements else node
        self.candidates.append(
            _Candidate(
                kind,
                self.relative_path,
                qualname or ".".join(self._qualnames) or "<module>",
                construct,
                _normalized_hash(surrounding),
                getattr(node, "lineno", 0),
            )
        )


def _is_budget_call(node: ast.Call, resolved: str) -> bool:
    if resolved in _TIMEOUT_APIS:
        return True
    if resolved in {"asyncio.wait", "reap"}:
        return len(node.args) >= 2 or any(keyword.arg == "timeout" for keyword in node.keywords)
    leaf = resolved.rsplit(".", maxsplit=1)[-1]
    if leaf in _BUDGET_HELPER_LEAVES:
        return True
    if any(
        keyword.arg is not None and _contains_word(keyword.arg, _BUDGET_WORDS)
        for keyword in node.keywords
    ):
        return True
    return leaf == "join" and any(_expression_mentions_budget(argument) for argument in node.args)


def _expression_mentions_budget(node: ast.AST) -> bool:
    return any(
        _contains_word(child.id if isinstance(child, ast.Name) else child.attr, _BUDGET_WORDS)
        for child in ast.walk(node)
        if isinstance(child, (ast.Name, ast.Attribute))
    )


def _contains_word(value: str, words: tuple[str, ...]) -> bool:
    lowered = value.lower()
    return any(word in lowered for word in words)


def _assigned_leaf_names(target: ast.AST) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Attribute):
        return [target.attr]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for item in target.elts for name in _assigned_leaf_names(item)]
    return []


def _normalized_hash(node: ast.AST) -> str:
    return ast_digest(node)
