"""Pin the ratchet digest so it cannot drift with the interpreter.

Every manifest in this package records an ``ast_hash``, and CI verifies the same
manifests on 3.11 and 3.14. The digest therefore has to be a property of the
source, not of the running interpreter's ``ast.dump``. These tests fail on the
interpreter where a regression appears, which is what makes the cross-version
matrix meaningful instead of a coin flip.
"""

from __future__ import annotations

import ast
import hashlib

from tests.ratchets._ast_digest import ast_digest, canonical_dump

_SOURCE = """
class Worker:
    @staticmethod
    async def run(self, retries=3):
        await self._turn.commit()
        label = "done"
        return label
"""

# Reviewed against 3.11's ``ast.dump(annotate_fields=True,
# include_attributes=False)``, which is the output every existing baseline in
# this directory was classified against.
_EXPECTED_CANONICAL = (
    "ClassDef(name='Worker', bases=[], keywords=[], body=[AsyncFunctionDef(name='run', "
    "args=arguments(posonlyargs=[], args=[arg(arg='self'), arg(arg='retries')], "
    "kwonlyargs=[], kw_defaults=[], defaults=[Constant(value=3)]), "
    "body=[Expr(value=Await(value=Call(func=Attribute(value=Attribute("
    "value=Name(id='self', ctx=Load()), attr='_turn', ctx=Load()), attr='commit', "
    "ctx=Load()), args=[], keywords=[]))), Assign(targets=[Name(id='label', "
    "ctx=Store())], value=Constant(value='done')), Return(value=Name(id='label', "
    "ctx=Load()))], decorator_list=[Name(id='staticmethod', ctx=Load())])], "
    "decorator_list=[])"
)
_EXPECTED_DIGEST = "acaf37b8d0b7233f"


def _class_node() -> ast.ClassDef:
    node = ast.parse(_SOURCE).body[0]
    assert isinstance(node, ast.ClassDef)
    return node


def test_canonical_dump_matches_the_reviewed_baseline_serialization() -> None:
    assert canonical_dump(_class_node()) == _EXPECTED_CANONICAL


def test_ast_digest_is_the_pinned_hash_of_the_canonical_dump() -> None:
    node = _class_node()
    assert ast_digest(node) == _EXPECTED_DIGEST
    assert (
        ast_digest(node) == hashlib.sha256(canonical_dump(node).encode("utf-8")).hexdigest()[:16]
    )


def test_canonical_dump_keeps_empty_fields_that_ast_dump_drops_on_3_13() -> None:
    # 3.13 taught ``ast.dump`` to omit fields equal to their default, which
    # silently rewrote every hash for call-bearing nodes.
    call = ast.parse("self._turn.commit()").body[0]
    assert canonical_dump(call) == (
        "Expr(value=Call(func=Attribute(value=Attribute(value=Name(id='self', ctx=Load()), "
        "attr='_turn', ctx=Load()), attr='commit', ctx=Load()), args=[], keywords=[]))"
    )


def test_canonical_dump_omits_type_params_added_in_3_12() -> None:
    # PEP 695 gave function and class nodes a ``type_params`` field on 3.12+.
    for source in ("def f(): pass", "async def f(): pass", "class C: pass"):
        dumped = canonical_dump(ast.parse(source).body[0])
        assert "type_params" not in dumped, source


def test_canonical_dump_omits_optional_fields_that_default_to_none() -> None:
    # ``ast.dump`` skips an optional field whose class default is ``None``;
    # emitting ``kind=None`` here would invalidate every reviewed baseline.
    assert canonical_dump(ast.parse("'text'").body[0]) == "Expr(value=Constant(value='text'))"
    assert canonical_dump(ast.parse("return").body[0]) == "Return()"


def test_canonical_dump_still_separates_structurally_different_sources() -> None:
    # The stability work must not flatten real differences into one hash.
    digests = {
        ast_digest(ast.parse(source).body[0])
        for source in (
            "self._turn.commit()",
            "self._turn.begin()",
            "self._turn.commit(force=True)",
        )
    }
    assert len(digests) == 3
