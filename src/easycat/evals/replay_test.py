"""Replay a recorded production session as a regression test (M11).

Two modes are supported:

* **record-assertion** — inspect a bundle/journal without re-executing stages
  (use the re-exported ``load_bundle`` + ``assert_*`` helpers from
  :mod:`easycat.evals`).
* **artifact-replay** — :func:`replay_bundle_as_test` replays a bundle through
  :class:`~easycat.runtime.replay.ReplaySpec` with safe side-effect defaults
  (``fidelity=ARTIFACT``, ``tool_policy=DENY``) so external tools are NEVER
  executed unless the caller explicitly opts in with ``allow_tools=True``.
"""

from __future__ import annotations

from pathlib import Path

from easycat.debug.bundle import RunBundle
from easycat.runtime.replay import (
    ReplayFidelity,
    ReplayResult,
    ReplaySpec,
    ToolReplayPolicy,
)

__all__ = ["replay_bundle_as_test"]


def replay_bundle_as_test(
    bundle_path: str | Path,
    *,
    allow_tools: bool = False,
) -> ReplayResult:
    """Replay a recorded bundle with tool execution denied by default.

    Loads the bundle and replays it at ARTIFACT fidelity. Tool side effects are
    denied unless ``allow_tools=True``, so a promoted production replay never
    hits a live tool by accident. When the bundle carries a non-committable
    side-effecting frame and tools are denied, the replay raises
    :class:`~easycat.runtime.replay.ReplaySideEffectBlocked`.
    """
    bundle = RunBundle.load(Path(bundle_path))
    spec = ReplaySpec(
        fidelity=ReplayFidelity.ARTIFACT,
        tool_policy=ToolReplayPolicy.ALLOW if allow_tools else ToolReplayPolicy.DENY,
        timing="fast",
    )
    return bundle.replay(spec)
