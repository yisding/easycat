"""Location-free production-source fingerprints for WS0.3 ratchets."""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from tests.ratchets._ast_digest import ast_digest

RAW_TASK_EXEMPT = frozenset({"_concurrency.py", "runtime/scope.py"})
CANCELLING_EXEMPT = RAW_TASK_EXEMPT
UNCANCEL_EXEMPT = frozenset({"_concurrency.py"})
CANCEL_HANDLER_EXEMPT = frozenset({"_concurrency.py"})
SHIELD_LOOP_EXEMPT = frozenset({"_concurrency.py", "runtime/scope.py"})
EPOCH_EXEMPT = frozenset(
    {
        "_epoch.py",
        # These fields are STT audio-accounting watermarks, not identity fences.
        "stt/cartesia_provider.py",
        "stt/deepgram_provider.py",
        "stt/elevenlabs_provider.py",
        "stt/websocket_base.py",
    }
)

_LOOP_FACTORIES = frozenset(
    {
        "asyncio.get_event_loop",
        "asyncio.get_running_loop",
        "asyncio.new_event_loop",
    }
)


@dataclass(frozen=True, order=True, slots=True)
class Fingerprint:
    """One structural production-source occurrence without source locations."""

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
    def from_record(cls, record: str) -> Fingerprint:
        category, path, qualname, construct, ast_hash, occurrence = record.split("\t")
        return cls(
            category=category,
            path=path,
            qualname=qualname,
            construct=construct,
            ast_hash=ast_hash,
            occurrence=int(occurrence),
        )


@dataclass(frozen=True, slots=True)
class Finding:
    """A fingerprint plus an ephemeral line number for failure diagnostics."""

    fingerprint: Fingerprint
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


def scan_production_source(source_root: Path) -> list[Finding]:
    """Return deterministic fingerprints for every guarded production construct."""
    candidates: list[_Candidate] = []
    for path in sorted(source_root.rglob("*.py")):
        relative_path = path.relative_to(source_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _InventoryVisitor(relative_path)
        visitor.visit(tree)
        candidates.extend(visitor.candidates)

    occurrences: Counter[tuple[str, str, str, str, str]] = Counter()
    findings: list[Finding] = []
    for candidate in sorted(candidates, key=_candidate_sort_key):
        identity = candidate.identity
        occurrence = occurrences[identity]
        occurrences[identity] += 1
        findings.append(
            Finding(
                Fingerprint(
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


def inventory_counts(fingerprints: set[Fingerprint]) -> dict[str, int]:
    """Summarize the reviewed baseline by category."""
    return dict(sorted(Counter(item.category for item in fingerprints).items()))


def inventory_delta(
    expected: set[Fingerprint],
    actual: set[Fingerprint],
) -> tuple[list[Fingerprint], list[Fingerprint]]:
    """Return added and removed fingerprints in stable order."""
    return sorted(actual - expected), sorted(expected - actual)


def format_delta(
    added: list[Fingerprint],
    removed: list[Fingerprint],
    *,
    actual_findings: list[Finding] = (),
) -> str:
    """Render a compact baseline drift report with current lines when available."""
    lines_by_fingerprint = {item.fingerprint: item.line for item in actual_findings}
    sections: list[str] = []
    if added:
        rendered = [
            _format_fingerprint(item, line=lines_by_fingerprint.get(item)) for item in added
        ]
        sections.append("new production constructs:\n  " + "\n  ".join(rendered))
    if removed:
        rendered = [_format_fingerprint(item) for item in removed]
        sections.append(
            "removed or structurally changed baseline entries:\n  " + "\n  ".join(rendered)
        )
    return "\n".join(sections)


def _candidate_sort_key(candidate: _Candidate) -> tuple[object, ...]:
    return (*candidate.identity, candidate.line)


def _format_fingerprint(item: Fingerprint, *, line: int | None = None) -> str:
    location = f"{item.path}:{line}" if line is not None else item.path
    return (
        f"{location} [{item.category}] {item.qualname} {item.construct} "
        f"{item.ast_hash}#{item.occurrence}"
    )


class _InventoryVisitor(ast.NodeVisitor):
    """Collect guarded constructs while resolving straightforward lexical aliases."""

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
            bound_name = alias.asname or alias.name.split(".", maxsplit=1)[0]
            self._aliases[-1][bound_name] = alias.name

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module is None:
            return
        for alias in node.names:
            bound_name = alias.asname or alias.name
            self._aliases[-1][bound_name] = f"{node.module}.{alias.name}"

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        with self._scope(node.name, kind="class"):
            self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        with self._scope(node.name, kind="function", arguments=node.args):
            self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        with self._scope(node.name, kind="function", arguments=node.args):
            self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        self._record_epoch_assignment(node, node.targets, node.value)
        alias = self._alias_for_value(node.value)
        for target in node.targets:
            self._update_alias(target, alias)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if not self._qualnames and self._is_task_set_annotation(node.annotation):
            self._record("module_task_set", "set[asyncio.Task/Future]", node)
        self.generic_visit(node)
        if self._is_integer_annotation(node.annotation):
            self._record_epoch_targets(node, [node.target])
        if node.value is not None:
            self._update_alias(node.target, self._alias_for_value(node.value))

    def visit_Call(self, node: ast.Call) -> None:
        resolved = self._resolve(node.func)
        if self.relative_path not in RAW_TASK_EXEMPT:
            spawn_shape = self._spawn_shape(resolved)
            if spawn_shape is not None:
                self._record("raw_task_spawn", spawn_shape, node)
        if self.relative_path not in CANCELLING_EXEMPT and resolved.endswith(".cancelling"):
            self._record("task_cancelling", "Task.cancelling", node)
        if self.relative_path not in UNCANCEL_EXEMPT and resolved.endswith(".uncancel"):
            self._record("task_uncancel", "Task.uncancel", node)
        if self._is_asyncio_call(resolved, "gather") and self._has_true_keyword(
            node, "return_exceptions"
        ):
            self._record(
                "gather_return_exceptions",
                "asyncio.gather(return_exceptions=True)",
                node,
            )
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if (
            self.relative_path not in CANCEL_HANDLER_EXEMPT
            and node.type is not None
            and self._contains_cancelled_error(node.type)
        ):
            self._record("cancelled_error_handler", "except asyncio.CancelledError", node)
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        if self.relative_path not in SHIELD_LOOP_EXEMPT and self._is_shield_loop(node):
            self._record("shield_loop", "while Task.done + asyncio.shield", node)
        self.generic_visit(node)

    @contextmanager
    def _scope(
        self,
        name: str,
        *,
        kind: str,
        arguments: ast.arguments | None = None,
    ) -> Iterator[None]:
        aliases = dict(self._aliases[-1])
        if arguments is not None:
            for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs):
                if self._is_event_loop_annotation(argument.annotation):
                    aliases[argument.arg] = "@loop"
        self._aliases.append(aliases)
        self._qualnames.append(name)
        self._scope_kinds.append(kind)
        try:
            yield
        finally:
            self._scope_kinds.pop()
            self._qualnames.pop()
            self._aliases.pop()

    def _resolve(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return self._aliases[-1].get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self._resolve(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return ""

    def _alias_for_value(self, value: ast.AST) -> str | None:
        if isinstance(value, (ast.Name, ast.Attribute)):
            resolved = self._resolve(value)
            return resolved or None
        if isinstance(value, ast.Call) and self._resolve(value.func) in _LOOP_FACTORIES:
            return "@loop"
        return None

    def _update_alias(self, target: ast.AST, alias: str | None) -> None:
        if not isinstance(target, ast.Name):
            return
        if alias is None:
            self._aliases[-1].pop(target.id, None)
        else:
            self._aliases[-1][target.id] = alias

    @staticmethod
    def _spawn_shape(resolved: str) -> str | None:
        if resolved == "asyncio.create_task":
            return "asyncio.create_task"
        if resolved == "asyncio.ensure_future":
            return "asyncio.ensure_future"
        base, separator, attribute = resolved.rpartition(".")
        if separator and attribute == "create_task" and _looks_like_loop(base):
            return "loop.create_task"
        return None

    @staticmethod
    def _is_asyncio_call(resolved: str, name: str) -> bool:
        return resolved == f"asyncio.{name}"

    @staticmethod
    def _has_true_keyword(node: ast.Call, name: str) -> bool:
        return any(
            keyword.arg == name
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        )

    def _contains_cancelled_error(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Tuple):
            return any(self._contains_cancelled_error(item) for item in node.elts)
        return self._resolve(node) == "asyncio.CancelledError"

    def _is_task_set_annotation(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Subscript):
            return False
        if self._resolve(node.value) not in {"set", "typing.Set"}:
            return False
        return any(
            self._resolve(child) in {"asyncio.Future", "asyncio.Task"}
            for child in ast.walk(node.slice)
            if isinstance(child, (ast.Name, ast.Attribute))
        )

    def _is_integer_annotation(self, node: ast.AST) -> bool:
        if isinstance(node, (ast.Name, ast.Attribute)):
            return self._resolve(node) == "int"
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return self._is_integer_annotation(node.left) or self._is_integer_annotation(
                node.right
            )
        if isinstance(node, ast.Subscript):
            annotation = self._resolve(node.value)
            if annotation in {"Optional", "typing.Optional"}:
                return self._is_integer_annotation(node.slice)
            if annotation in {"Annotated", "typing.Annotated"} and isinstance(
                node.slice, ast.Tuple
            ):
                return self._is_integer_annotation(node.slice.elts[0])
        return False

    def _is_event_loop_annotation(self, node: ast.AST | None) -> bool:
        if node is None:
            return False
        return self._resolve(node) in {
            "asyncio.AbstractEventLoop",
            "asyncio.BaseEventLoop",
        }

    def _is_shield_loop(self, node: ast.While) -> bool:
        has_done_check = any(
            isinstance(child, ast.Call) and self._resolve(child.func).endswith(".done")
            for child in ast.walk(node.test)
        )
        if not has_done_check:
            return False
        return any(
            isinstance(child, ast.Call)
            and self._is_asyncio_call(self._resolve(child.func), "shield")
            for statement in node.body
            for child in ast.walk(statement)
        )

    def _record_epoch_assignment(
        self,
        surrounding: ast.stmt,
        targets: list[ast.expr],
        value: ast.AST,
    ) -> None:
        if self.relative_path in EPOCH_EXEMPT or not _is_integer_field_initial_value(value):
            return
        self._record_epoch_targets(surrounding, targets)

    def _record_epoch_targets(
        self,
        surrounding: ast.stmt,
        targets: list[ast.expr],
    ) -> None:
        if self.relative_path in EPOCH_EXEMPT:
            return
        for target in targets:
            if not self._is_field_declaration_target(target):
                continue
            for leaf_name in _assigned_leaf_names(target):
                if "generation" in leaf_name or "epoch" in leaf_name:
                    self._record("epoch_field", f"integer field {leaf_name}", surrounding)

    def _is_field_declaration_target(self, target: ast.AST) -> bool:
        if isinstance(target, ast.Name):
            return not self._scope_kinds or self._scope_kinds[-1] == "class"
        return (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and bool(self._qualnames)
            and self._qualnames[-1] == "__init__"
        )

    def _record(self, category: str, construct: str, node: ast.AST) -> None:
        surrounding = node if isinstance(node, ast.ExceptHandler) else self._surrounding(node)
        self.candidates.append(
            _Candidate(
                category=category,
                path=self.relative_path,
                qualname=".".join(self._qualnames) or "<module>",
                construct=construct,
                ast_hash=_normalized_hash(surrounding),
                line=getattr(node, "lineno", 0),
            )
        )

    def _surrounding(self, node: ast.AST) -> ast.AST:
        return self._statements[-1] if self._statements else node


def _looks_like_loop(base: str) -> bool:
    if base == "@loop":
        return True
    leaf = base.rsplit(".", maxsplit=1)[-1]
    return leaf == "loop" or leaf.endswith("_loop")


def _is_integer_field_initial_value(value: ast.AST) -> bool:
    if isinstance(value, ast.Constant):
        return value.value is None or (
            isinstance(value.value, int) and not isinstance(value.value, bool)
        )
    return (
        isinstance(value, ast.UnaryOp)
        and isinstance(value.op, (ast.USub, ast.UAdd))
        and isinstance(value.operand, ast.Constant)
        and isinstance(value.operand.value, int)
    )


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
