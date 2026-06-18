"""Promote a recorded turn into a committed regression test.

Scaffolded in Milestone 10 (Workstream B); the hardened logic — redact-by-
default, ``--no-audio`` default, ``--allow-pii`` tripwire, hash/regex default
assertion, and pytest-skeleton generation — is implemented in Milestone 11
(Workstream C). The :func:`promote_turn_to_test` symbol exists here so the
``easycat.evals`` public surface is stable across both milestones; calling it
before M11 raises :class:`NotImplementedError`.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["promote_turn_to_test"]


def promote_turn_to_test(
    bundle_path: str | Path,
    turn_id: str,
    *,
    out: str | Path,
    name: str | None = None,
    include_audio: bool = False,
    allow_pii: bool = False,
    mode: str = "record-assertion",
    assert_on: str = "hash",
) -> Path:
    """Generate a pytest regression test from a recorded turn.

    Scaffold only (M10). The redact-by-default, tripwire-guarded implementation
    lands in M11; until then this raises :class:`NotImplementedError`.
    """
    raise NotImplementedError(
        "promote_turn_to_test is scaffolded in M10 and implemented in M11 "
        "(hardened, redact-by-default promotion)."
    )
