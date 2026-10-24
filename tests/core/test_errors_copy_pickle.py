"""EasyCatError must survive copy and pickle round-trips.

Exceptions raised in ``ProcessPoolExecutor`` / ``multiprocessing`` workers are
pickled back to the parent. ``EasyCatError.__init__`` takes
``(error_code, message, *, fix, **context)`` but passes only the rendered
string to ``Exception.__init__``, so ``args`` alone cannot rebuild the
instance; ``__reduce__`` restores it from instance state instead (gh 1261).
"""

from __future__ import annotations

import copy
import pickle

import pytest

from easycat import errors
from easycat.errors import EasyCatError


def _roundtrips() -> list:
    return [
        pytest.param(copy.copy, id="copy"),
        pytest.param(copy.deepcopy, id="deepcopy"),
        pytest.param(lambda e: pickle.loads(pickle.dumps(e)), id="pickle"),
    ]


@pytest.mark.parametrize("roundtrip", _roundtrips())
def test_factory_error_survives_roundtrip(roundtrip) -> None:
    original = errors.EASYCAT_E101(target="x")

    clone = roundtrip(original)

    assert type(clone) is type(original)
    assert clone.code == original.code
    assert clone.message == original.message
    assert clone.context == original.context
    assert str(clone) == str(original)


@pytest.mark.parametrize("roundtrip", _roundtrips())
def test_hand_built_error_keeps_fix_override(roundtrip) -> None:
    original = EasyCatError("EASYCAT_E999", "boom", fix="do the thing", detail=1)

    clone = roundtrip(original)

    assert clone.code == "EASYCAT_E999"
    assert clone.context == {"detail": 1}
    assert clone.rendered_fix() == original.rendered_fix()


@pytest.mark.parametrize("roundtrip", _roundtrips())
def test_subclass_errors_survive_roundtrip(roundtrip) -> None:
    from easycat.errors import EasyConfigError
    from easycat.timeouts import STTTimeoutError

    for original in (EasyConfigError("bad config"), STTTimeoutError("deepgram", 1.5)):
        clone = roundtrip(original)

        assert type(clone) is type(original)
        assert clone.__dict__ == original.__dict__
        assert str(clone) == str(original)
