"""Conversation simulation strategies for eval scenarios.

The eval runner starts with **text-first simulation**: each scenario turn is a
fixed user message replayed against a text-mode session (see
:class:`~easycat.evals.runner.EvalRunner`). Audio simulation and synthetic
callers follow once text scenarios are stable and the audio-simulation runtime
metrics land.

This module names the simulation modes so the runner and CLI can refer to a
stable vocabulary; the audio/synthetic strategies are intentionally not yet
implemented.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["SimulationMode"]


class SimulationMode(StrEnum):
    """How an eval scenario drives its turns.

    * :attr:`TEXT` — the only no-API-key mode; replays fixed user text through
      ``Session.send_text`` (implemented).
    * :attr:`AUDIO` — replays synthesized audio through the full pipeline
      (follow-up work; raises until the audio-simulation runner lands).
    * :attr:`SYNTHETIC_CALLER` — an LLM-driven caller persona (follow-up work).
    """

    TEXT = "text"
    AUDIO = "audio"
    SYNTHETIC_CALLER = "synthetic_caller"
