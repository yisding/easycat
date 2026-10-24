"""EasyCatError must survive copy and pickle round-trips.

Exceptions raised in ``ProcessPoolExecutor`` / ``multiprocessing`` workers are
pickled back to the parent. ``EasyCatError.__init__`` takes
``(error_code, message, *, fix, **context)`` but passes only the rendered
string to ``Exception.__init__``, so ``args`` cannot rebuild the instance.
"""

from __future__ import annotations

import copy
import pickle

import pytest

from easycat import errors
from easycat.errors import EasyCatError

_XFAIL = pytest.mark.xfail(
    strict=True,
    raises=TypeError,
    reason="EasyCatError.args holds only the rendered string, so copy/pickle cannot rebuild it",
)


def _roundtrips() -> list:
    return [
        pytest.param(copy.copy, id="copy"),
        pytest.param(copy.deepcopy, id="deepcopy"),
        pytest.param(lambda e: pickle.loads(pickle.dumps(e)), id="pickle"),
    ]


@_XFAIL
@pytest.mark.parametrize("roundtrip", _roundtrips())
def test_factory_error_survives_roundtrip(roundtrip) -> None:
    original = errors.EASYCAT_E101(target="x")

    clone = roundtrip(original)

    assert type(clone) is type(original)
    assert clone.code == original.code
    assert clone.message == original.message
    assert clone.context == original.context
    assert str(clone) == str(original)


@_XFAIL
@pytest.mark.parametrize("roundtrip", _roundtrips())
def test_hand_built_error_keeps_fix_override(roundtrip) -> None:
    original = EasyCatError("EASYCAT_E999", "boom", fix="do the thing", detail=1)

    clone = roundtrip(original)

    assert clone.code == "EASYCAT_E999"
    assert clone.context == {"detail": 1}
    assert clone.rendered_fix() == original.rendered_fix()
