"""Location-free inventory of production listener bind capabilities."""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from tests.ratchets._ast_digest import ast_digest

CLASSIFICATIONS = frozenset(
    {
        "authorized_capability",
        "generated_template",
        "guarded_embedding",
        "loopback_probe",
        "pending_migration",
    }
)

_WEBSOCKET_SERVE_APIS = frozenset(
    {
        "websockets.serve",
        "websockets.asyncio.server.serve",
        "websockets.legacy.server.serve",
    }
)


@dataclass(frozen=True, order=True, slots=True)
class BindSite:
    """One location-free listener backend invocation."""

    backend: str
    path: str
    qualname: str
    construct: str
    ast_hash: str
    occurrence: int

    def as_record(self) -> str:
        return "\t".join(
            (
                self.backend,
                self.path,
                self.qualname,
                self.construct,
                self.ast_hash,
                str(self.occurrence),
            )
        )

    @classmethod
    def from_record(cls, record: str) -> BindSite:
        backend, path, qualname, construct, ast_hash, occurrence = record.split("\t")
        return cls(backend, path, qualname, construct, ast_hash, int(occurrence))


@dataclass(frozen=True, slots=True)
class BindFinding:
    site: BindSite
    line: int


@dataclass(frozen=True, slots=True)
class _Candidate:
    backend: str
    path: str
    qualname: str
    construct: str
    ast_hash: str
    line: int

    @property
    def identity(self) -> tuple[str, str, str, str, str]:
        return (self.backend, self.path, self.qualname, self.construct, self.ast_hash)


def scan_bind_sites(source_root: Path) -> list[BindFinding]:
    """Inventory socket-opening backends while following simple lexical aliases."""
    candidates: list[_Candidate] = []
    for path in sorted(source_root.rglob("*.py")):
        relative_path = path.relative_to(source_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _BindVisitor(relative_path)
        visitor.visit(tree)
        candidates.extend(visitor.candidates)

    occurrences: Counter[tuple[str, str, str, str, str]] = Counter()
    findings: list[BindFinding] = []
    for candidate in sorted(candidates, key=_candidate_sort_key):
        identity = candidate.identity
        occurrence = occurrences[identity]
        occurrences[identity] += 1
        findings.append(
            BindFinding(
                BindSite(
                    backend=candidate.backend,
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
    expected: set[BindSite],
    actual: set[BindSite],
) -> tuple[list[BindSite], list[BindSite]]:
    return sorted(actual - expected), sorted(expected - actual)


def format_delta(
    added: list[BindSite],
    removed: list[BindSite],
    *,
    findings: Sequence[BindFinding] = (),
) -> str:
    lines = {finding.site: finding.line for finding in findings}
    sections: list[str] = []
    if added:
        sections.append(
            "unclassified production bind sites:\n  "
            + "\n  ".join(_format_site(site, line=lines.get(site)) for site in added)
        )
    if removed:
        sections.append(
            "stale or structurally changed bind sites:\n  "
            + "\n  ".join(_format_site(site) for site in removed)
        )
    return "\n".join(sections)


def _candidate_sort_key(candidate: _Candidate) -> tuple[object, ...]:
    return (*candidate.identity, candidate.line)


def _format_site(site: BindSite, *, line: int | None = None) -> str:
    location = f"{site.path}:{line}" if line is not None else site.path
    return (
        f"{location} [{site.backend}] {site.qualname} {site.construct} "
        f"{site.ast_hash}#{site.occurrence}"
    )


class _BindVisitor(ast.NodeVisitor):
    """Recognize listener APIs without treating every method named ``bind`` as a socket."""

    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.candidates: list[_Candidate] = []
        self._aliases: list[dict[str, str]] = [{}]
        self._qualnames: list[str] = []
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
        with self._scope(node.name):
            self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        with self._scope(node.name, arguments=node.args):
            self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        with self._scope(node.name, arguments=node.args):
            self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        alias = self._alias_for_value(node.value)
        for target in node.targets:
            self._update_alias(target, alias)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.generic_visit(node)
        if node.value is not None:
            self._update_alias(node.target, self._alias_for_value(node.value))

    def visit_Call(self, node: ast.Call) -> None:
        resolved = self._resolve(node.func)
        backend = _bind_backend(resolved)
        if backend is not None:
            self._record(backend, f"call {resolved}", node)
        self.generic_visit(node)

    @contextmanager
    def _scope(
        self,
        name: str,
        *,
        arguments: ast.arguments | None = None,
    ) -> Iterator[None]:
        aliases = dict(self._aliases[-1])
        if arguments is not None:
            for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs):
                if _is_socket_annotation(self._resolve(argument.annotation)):
                    aliases[argument.arg] = "@socket"
        self._aliases.append(aliases)
        self._qualnames.append(name)
        try:
            yield
        finally:
            self._qualnames.pop()
            self._aliases.pop()

    def _resolve(self, node: ast.AST | None) -> str:
        if isinstance(node, ast.Name):
            return self._aliases[-1].get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self._resolve(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        if isinstance(node, ast.Call) and self._resolve(node.func) == "socket.socket":
            return "@socket"
        return ""

    def _alias_for_value(self, value: ast.AST) -> str | None:
        if isinstance(value, (ast.Name, ast.Attribute)):
            return self._resolve(value) or None
        if isinstance(value, ast.Call) and self._resolve(value.func) == "socket.socket":
            return "@socket"
        return None

    def _update_alias(self, target: ast.AST, alias: str | None) -> None:
        if not isinstance(target, ast.Name):
            return
        if alias is None:
            self._aliases[-1].pop(target.id, None)
        else:
            self._aliases[-1][target.id] = alias

    def _record(self, backend: str, construct: str, node: ast.Call) -> None:
        surrounding = self._statements[-1] if self._statements else node
        self.candidates.append(
            _Candidate(
                backend=backend,
                path=self.relative_path,
                qualname=".".join(self._qualnames) or "<module>",
                construct=construct,
                ast_hash=ast_digest(surrounding),
                line=node.lineno,
            )
        )


def _bind_backend(resolved: str) -> str | None:
    if resolved in _WEBSOCKET_SERVE_APIS or (
        resolved.startswith("websockets.") and resolved.endswith(".serve")
    ):
        return "websockets_serve"
    if resolved.endswith(".TCPSite"):
        return "aiohttp_tcp_site"
    if resolved.endswith(".run_app"):
        return "aiohttp_run_app"
    if resolved == "@socket.bind" or _looks_like_socket_bind(resolved):
        return "socket_bind"
    base, separator, leaf = resolved.rpartition(".")
    if (
        separator
        and leaf == "serve"
        and (
            base in {"aioquic.asyncio", "aioquic.asyncio.server"}
            or base.rsplit(".", maxsplit=1)[-1] == "aioquic_server"
        )
    ):
        return "aioquic_serve"
    return None


def _is_socket_annotation(resolved: str) -> bool:
    return resolved in {"socket.socket", "socket.SocketType"}


def _looks_like_socket_bind(resolved: str) -> bool:
    base, separator, leaf = resolved.rpartition(".")
    if not separator or leaf != "bind":
        return False
    receiver = base.rsplit(".", maxsplit=1)[-1].lower()
    return receiver in {"probe", "sock", "socket"} or receiver.endswith(
        ("_probe", "_sock", "_socket")
    )
