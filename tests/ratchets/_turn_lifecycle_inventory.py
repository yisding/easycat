"""Location-free fingerprints for turn identity and activity writers."""

from __future__ import annotations

import ast
import hashlib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, order=True, slots=True)
class TurnLifecycleSite:
    """One structural turn-lifecycle occurrence without a source location."""

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
    def from_record(cls, record: str) -> TurnLifecycleSite:
        category, path, qualname, construct, ast_hash, occurrence = record.split("\t")
        return cls(category, path, qualname, construct, ast_hash, int(occurrence))


@dataclass(frozen=True, slots=True)
class TurnLifecycleFinding:
    """A stable site plus an ephemeral line for failure diagnostics."""

    site: TurnLifecycleSite
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


def scan_turn_lifecycle(source_root: Path) -> list[TurnLifecycleFinding]:
    """Find every guarded turn-identity, activity, and TurnStarted source site."""
    candidates: list[_Candidate] = []
    for path in sorted(source_root.rglob("*.py")):
        relative_path = path.relative_to(source_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _TurnLifecycleVisitor(relative_path)
        visitor.visit(tree)
        candidates.extend(visitor.candidates)

    occurrences: Counter[tuple[str, str, str, str, str]] = Counter()
    findings: list[TurnLifecycleFinding] = []
    for candidate in sorted(candidates, key=_candidate_sort_key):
        identity = candidate.identity
        occurrence = occurrences[identity]
        occurrences[identity] += 1
        findings.append(
            TurnLifecycleFinding(
                site=TurnLifecycleSite(
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
    expected: set[TurnLifecycleSite],
    actual: set[TurnLifecycleSite],
) -> tuple[list[TurnLifecycleSite], list[TurnLifecycleSite]]:
    """Return added and removed sites in stable order."""
    return sorted(actual - expected), sorted(expected - actual)


def format_delta(
    added: list[TurnLifecycleSite],
    removed: list[TurnLifecycleSite],
    *,
    findings: Sequence[TurnLifecycleFinding] = (),
) -> str:
    """Render inventory drift with current line numbers when available."""
    lines = {finding.site: finding.line for finding in findings}
    sections: list[str] = []
    if added:
        sections.append(
            "new turn-lifecycle sites:\n  "
            + "\n  ".join(_format_site(site, line=lines.get(site)) for site in added)
        )
    if removed:
        sections.append(
            "removed or structurally changed turn-lifecycle sites:\n  "
            + "\n  ".join(_format_site(site) for site in removed)
        )
    return "\n".join(sections)


def _candidate_sort_key(candidate: _Candidate) -> tuple[object, ...]:
    return (*candidate.identity, candidate.line)


def _format_site(site: TurnLifecycleSite, *, line: int | None = None) -> str:
    location = f"{site.path}:{line}" if line is not None else site.path
    return (
        f"{location} [{site.category}] {site.qualname} {site.construct} "
        f"{site.ast_hash}#{site.occurrence}"
    )


class _TurnLifecycleVisitor(ast.NodeVisitor):
    """Collect direct state writers, lifecycle seams, and event topology."""

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

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._record_assignment_target(target, node, operation="assign")
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._record_assignment_target(node.target, node, operation="assign")
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._record_assignment_target(node.target, node, operation="update")
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._record_assignment_target(target, node, operation="delete")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        callee = _expression_path(node.func)
        leaf = callee.rsplit(".", 1)[-1]

        if leaf == "TurnStarted":
            self._record("turn_started_producer", "construct TurnStarted", node)
        if (
            leaf in {"subscribe", "_subscribe_owned"}
            and node.args
            and _expression_path(node.args[0]).rsplit(".", 1)[-1] == "TurnStarted"
        ):
            self._record(
                "turn_started_subscription",
                f"call {callee}",
                node,
            )

        if self.relative_path == "turn_manager.py" and callee == "self._transition":
            state = ast.unparse(node.args[0]) if node.args else "<missing>"
            reason = next(
                (
                    ast.unparse(keyword.value)
                    for keyword in node.keywords
                    if keyword.arg == "reason"
                ),
                "<missing>",
            )
            self._record(
                "activity_transition_call",
                f"transition {state} reason={reason}",
                node,
            )
        activity_target = callee.rsplit(".", 1)[0]
        activity_owner, _, activity_attribute = activity_target.rpartition(".")
        if (
            leaf == "bump"
            and activity_attribute == "_activity"
            and self._is_turn_manager_owner(activity_owner)
        ):
            self._record(
                "activity_epoch_bump",
                "bump self._activity",
                node,
            )

        reset_owner = callee.rsplit(".", 1)[0]
        if (
            callee == "self.reset"
            and self.relative_path == "turn_manager.py"
            or leaf == "reset"
            and "turn_manager" in reset_owner.rsplit(".", 1)[-1]
        ):
            self._record("activity_reset_call", f"call {callee}", node)

        call_owner = callee.rsplit(".", 1)[0]
        if (
            callee in {"self._turn.begin", "self._turn.set"}
            or (leaf == "begin_turn" and "session" in call_owner.rsplit(".", 1)[-1])
            or leaf == "publish_identity"
        ):
            self._record("identity_publish_call", f"call {callee}", node)
        elif leaf in {"_clear_turn", "_reset_turn_state", "clear_identity"}:
            self._record("identity_clear_call", f"call {callee}", node)

        self._record_dynamic_writer(node, callee)
        self.generic_visit(node)

    def _visit_scope(
        self,
        name: str,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        self._scope.append(name)
        self.generic_visit(node)
        self._scope.pop()

    def _record_assignment_target(
        self,
        target: ast.expr,
        surrounding: ast.AST,
        *,
        operation: str,
    ) -> None:
        target_path = _expression_path(target)
        category = self._assignment_category(target_path)
        if category is not None:
            self._record(category, f"{operation} {target_path}", surrounding)

    def _assignment_category(self, target_path: str) -> str | None:
        owner, _, attribute = target_path.rpartition(".")
        owner_leaf = owner.rsplit(".", 1)[-1]
        scope_owner = self.qualname.split(".", 1)[0]
        if attribute == "_turn" and (
            "session" in owner_leaf or owner == "self" and scope_owner == "Session"
        ):
            return "identity_pointer_assignment"
        if attribute == "_turn_generation" and (
            "session" in owner_leaf or owner == "self" and scope_owner == "Session"
        ):
            return "identity_carrier_assignment"
        if self.relative_path == "_turn_context.py" and (
            (self.qualname == "TurnContext" and target_path == "_generation_counter")
            or target_path in {"TurnContext._generation_counter", "self.generation"}
        ):
            return "identity_carrier_assignment"
        if self.relative_path == "session/_turn_lifecycle.py":
            if target_path == "self._identity":
                return "identity_owner_assignment"
            if target_path == "turn.generation":
                return "identity_carrier_assignment"
        if attribute == "_state" and self._is_turn_manager_owner(owner):
            return "activity_state_assignment"
        if attribute == "_activity" and self._is_turn_manager_owner(owner):
            return "activity_owner_assignment"
        return None

    def _is_turn_manager_owner(self, owner: str) -> bool:
        owner_leaf = owner.rsplit(".", 1)[-1]
        scope_owner = self.qualname.split(".", 1)[0]
        return "turn_manager" in owner_leaf or (
            owner == "self"
            and self.relative_path == "turn_manager.py"
            and scope_owner == "TurnManager"
        )

    def _record_dynamic_writer(self, node: ast.Call, callee: str) -> None:
        if callee != "setattr" or len(node.args) < 2:
            return
        attribute = node.args[1]
        if not isinstance(attribute, ast.Constant) or not isinstance(attribute.value, str):
            return
        owner = _expression_path(node.args[0])
        target = f"{owner}.{attribute.value}"
        category = self._assignment_category(target)
        if category is not None:
            self._record(category, f"setattr {target}", node)

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


def _expression_path(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _expression_path(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""
