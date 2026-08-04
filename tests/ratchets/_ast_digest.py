"""Interpreter-stable AST digests for the reviewed ratchet baselines.

Every manifest in this package pins an ``ast_hash`` per classified source site,
so the digest has to be byte-identical on each interpreter in the support
matrix (3.11 through 3.14). Plain ``ast.dump`` is not, and CI runs the same
manifests on 3.11 and 3.14:

* 3.12 added PEP 695 ``type_params`` to ``FunctionDef``, ``AsyncFunctionDef``,
  and ``ClassDef``, so those nodes grew a field mid-matrix.
* 3.13 started omitting fields that equal their default, so ``args=[]`` and
  friends disappear from the dump on newer interpreters.

Either one shifts every hash for the affected nodes and fails the ratchets with
a wall of "removed or structurally changed" sites that no commit caused. This
module pins one canonical serialization instead of inheriting the stdlib's, and
``tests/ratchets/test_ast_digest.py`` locks the behaviour down.

The format deliberately reproduces ``ast.dump(node, annotate_fields=True,
include_attributes=False)`` as emitted by 3.11 — including its rule of dropping
an optional field that is ``None`` by default — so the baselines reviewed
against that output stay valid.
"""

from __future__ import annotations

import ast
import hashlib

# ``type_params`` only exists on 3.12+, and is always empty in source that must
# still import on 3.11, so it carries no signal worth ratcheting.
_VERSION_DEPENDENT_FIELDS = frozenset({"type_params"})


def canonical_dump(node: ast.AST) -> str:
    """Serialize ``node`` identically on every supported interpreter."""
    return _format(node)


def ast_digest(node: ast.AST) -> str:
    """Return the 16-hex-character digest the ratchet manifests record."""
    return hashlib.sha256(canonical_dump(node).encode("utf-8")).hexdigest()[:16]


def _format(value: object) -> str:
    if isinstance(value, ast.AST):
        fields = []
        for name in value._fields:
            if name in _VERSION_DEPENDENT_FIELDS:
                continue
            try:
                field = getattr(value, name)
            except AttributeError:
                # An unset optional field, exactly as ``ast.dump`` treats it.
                continue
            if field is None and getattr(type(value), name, object()) is None:
                continue
            fields.append(f"{name}={_format(field)}")
        return f"{type(value).__name__}({', '.join(fields)})"
    if isinstance(value, list):
        return f"[{', '.join(_format(item) for item in value)}]"
    return repr(value)
