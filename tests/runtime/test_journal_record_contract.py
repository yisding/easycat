from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from easycat.runtime.record_contracts import (
    BUILTIN_JOURNAL_RECORD_CONTRACTS,
    validate_builtin_record,
)
from easycat.runtime.records import JournalRecordKind

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src"
PACKAGE_ROOT = SOURCE_ROOT / "easycat"
JOURNAL_REFERENCE = REPO_ROOT / "docs" / "reference" / "journal-records.md"

_RECORD_CALLS = frozenset(
    {
        "append",
        "append_record",
        "append_record_async",
        "journal_append_event",
        "journal_append_event_async",
        "_append",
    }
)
_TYPED_RECORD_NAMES = {
    "BufferOverflow": "buffer_overflow",
    "JournalDegraded": "journal_degraded",
    "RecoveredSessionMarker": "recovered_session",
}
_CATALOG_SHA256 = "37aaa836e8e46b1c64021741f2872c74679bb9e0cfaf0400e741d4d55c19ca3c"


@dataclass(frozen=True)
class _DocumentedContract:
    kinds: frozenset[str]
    required_keys: frozenset[str]
    normalized_row: str


def _producer_paths() -> list[Path]:
    registry = PACKAGE_ROOT / "runtime" / "record_contracts.py"
    return [path for path in sorted(PACKAGE_ROOT.rglob("*.py")) if path != registry]


def _module_name(path: Path) -> str:
    relative = path.relative_to(SOURCE_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if (
            isinstance(target, ast.Name)
            and target.id.isupper()
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and value.value
        ):
            constants[target.id] = value.value
    return constants


def _imported_module(current: str, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = current.split(".")[:-1]
    keep = len(package) - node.level + 1
    prefix = package[: max(keep, 0)]
    return ".".join([*prefix, *(node.module or "").split(".")]).rstrip(".")


def _local_bindings(
    *,
    module: str,
    tree: ast.Module,
    module_constants: Mapping[str, Mapping[str, str]],
) -> tuple[dict[str, str], dict[str, str]]:
    constants = dict(module_constants[module])
    module_aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            imported_module = _imported_module(module, node)
            for alias in node.names:
                local_name = alias.asname or alias.name
                value = module_constants.get(imported_module, {}).get(alias.name)
                if value is not None:
                    constants[local_name] = value
                else:
                    child_module = f"{imported_module}.{alias.name}".strip(".")
                    if child_module in module_constants:
                        module_aliases[local_name] = child_module
    return constants, module_aliases


def _literal_string(
    node: ast.expr | None,
    constants: Mapping[str, str],
    module_aliases: Mapping[str, str],
    module_constants: Mapping[str, Mapping[str, str]],
) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value:
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        module = module_aliases.get(node.value.id)
        if module is not None:
            return module_constants.get(module, {}).get(node.attr)
    return None


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return ""


def _record_name_from_call(
    call: ast.Call,
    constants: Mapping[str, str],
    module_aliases: Mapping[str, str],
    module_constants: Mapping[str, Mapping[str, str]],
) -> str | None:
    function_name = _call_name(call)
    if function_name in _TYPED_RECORD_NAMES:
        return _TYPED_RECORD_NAMES[function_name]
    candidate: ast.expr | None = None
    if function_name == "_EventRecordSpec" and len(call.args) >= 3:
        candidate = call.args[2]
    elif function_name == "_make_event_handler" and len(call.args) >= 2:
        candidate = call.args[1]
    elif function_name in _RECORD_CALLS:
        keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
        if function_name != "append" or {"kind", "name", "session_id"} <= keywords.keys():
            candidate = keywords.get("name")
    return _literal_string(candidate, constants, module_aliases, module_constants)


def _source_record_names() -> frozenset[str]:
    paths = _producer_paths()
    trees = {
        _module_name(path): ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in paths
    }
    module_constants = {module: _module_string_constants(tree) for module, tree in trees.items()}
    names: set[str] = set()
    for module, tree in trees.items():
        constants, aliases = _local_bindings(
            module=module,
            tree=tree,
            module_constants=module_constants,
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _record_name_from_call(node, constants, aliases, module_constants)
                if name is not None:
                    names.add(name)
    return frozenset(names)


def _documented_record_contracts() -> dict[str, _DocumentedContract]:
    text = JOURNAL_REFERENCE.read_text(encoding="utf-8")
    catalog = text.split("## Pipeline Records", 1)[1].split("## Contract Guard", 1)[0]
    contracts: dict[str, _DocumentedContract] = {}
    for line in catalog.splitlines():
        if not re.match(r"^\| `[a-z][a-z0-9_]*` \|", line):
            continue
        cells = tuple(cell.strip() for cell in line.strip().strip("|").split("|"))
        assert len(cells) == 4, f"malformed catalog row: {line}"
        name = cells[0].strip("`")
        assert name not in contracts, f"duplicate journal catalog row: {name}"
        contracts[name] = _DocumentedContract(
            kinds=frozenset(re.findall(r"`([A-Z_]+)`", cells[1])),
            required_keys=frozenset(re.findall(r"`([a-z][a-z0-9_]*):", cells[2])),
            normalized_row="|".join(cells),
        )
    return contracts


def test_builtin_journal_record_names_match_runtime_registry() -> None:
    assert _source_record_names() == frozenset(BUILTIN_JOURNAL_RECORD_CONTRACTS)


def test_journal_record_reference_matches_runtime_contracts() -> None:
    documented = _documented_record_contracts()
    assert frozenset(documented) == frozenset(BUILTIN_JOURNAL_RECORD_CONTRACTS)
    for name, contract in BUILTIN_JOURNAL_RECORD_CONTRACTS.items():
        row = documented[name]
        assert row.kinds == frozenset(kind.name for kind in contract.kinds)
        assert row.required_keys == contract.required_data_keys


def test_journal_record_catalog_schema_snapshot() -> None:
    rows = "\n".join(
        contract.normalized_row for _, contract in sorted(_documented_record_contracts().items())
    )
    assert hashlib.sha256(rows.encode()).hexdigest() == _CATALOG_SHA256


def test_runtime_contract_rejects_wrong_kind_and_missing_keys() -> None:
    with pytest.raises(ValueError, match="must use journal kind"):
        validate_builtin_record(
            name="text_turn_latency_ms",
            kind=JournalRecordKind.EVENT,
            data={"value": 1.0, "surface": "text"},
        )
    with pytest.raises(ValueError, match="missing required data keys: surface"):
        validate_builtin_record(
            name="text_turn_latency_ms",
            kind=JournalRecordKind.METRIC,
            data={"value": 1.0},
        )
    validate_builtin_record(
        name="application_defined",
        kind=JournalRecordKind.SPAN_START,
        data=None,
    )
