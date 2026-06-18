"""Replay a recorded production session as a regression test.

Scaffolded in Milestone 10 (Workstream B); implemented in Milestone 11
(Workstream C). Two modes are planned:

* **record-assertion** — inspect a bundle/journal without re-executing stages
  (already possible today via the re-exported ``load_bundle`` +
  ``assert_*`` helpers).
* **artifact-replay** — replay through ``ReplaySpec`` with safe side-effect
  defaults (``fidelity=ARTIFACT``, ``tool_policy=DENY``) so external tools are
  never executed unless the caller explicitly opts in.

The artifact-replay helper below is a scaffold; the hardened implementation
lands in M11.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["replay_bundle_as_test"]


def replay_bundle_as_test(bundle_path: str | Path, *, allow_tools: bool = False) -> None:
    """Replay a recorded bundle with tool execution denied by default.

    Scaffold only (M10). The artifact-replay implementation (denying tool
    side effects via ``ReplaySpec.tool_policy=DENY``) lands in M11; until then
    this raises :class:`NotImplementedError`.
    """
    raise NotImplementedError(
        "replay_bundle_as_test is scaffolded in M10 and implemented in M11 "
        "(artifact replay with tool execution denied by default)."
    )
