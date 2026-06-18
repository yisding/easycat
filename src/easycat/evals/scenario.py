"""Eval scenario data model.

A scenario is a named list of :class:`EvalTurn` exchanges plus an optional set
of latency/cost budgets. :class:`EvalScenario` is consumed by
:class:`~easycat.evals.runner.EvalRunner`, which replays each turn against a
text-mode session and evaluates the budgets through the shared
:func:`~easycat.budgets.build_budget_report` evaluator.

``CostBudget`` is imported from :mod:`easycat.budgets` (M9) — it is NOT redefined
here. ``LatencyBudget`` is the existing
:class:`easycat.validation.latency.LatencyBudget`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from easycat.budgets import CostBudget, LatencyBudget

__all__ = ["EvalScenario", "EvalTurn", "load_scenario"]


@dataclass
class EvalTurn:
    """One user message and the expectations placed on the bot reply."""

    user: str
    expect_response_regex: str | None = None
    expect_tools: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvalTurn:
        """Build a turn from a scenario-file ``turns[]`` entry.

        Accepts both the flat shape (``expect_response_regex``/``expect_tools``)
        and the nested ``expect: {response_regex, tools}`` shape used in the
        documented YAML schema.
        """
        expect = payload.get("expect")
        regex = payload.get("expect_response_regex")
        tools = payload.get("expect_tools")
        if isinstance(expect, Mapping):
            regex = expect.get("response_regex", regex)
            tools = expect.get("tools", tools)
        return cls(
            user=str(payload["user"]),
            expect_response_regex=str(regex) if regex is not None else None,
            expect_tools=[str(tool) for tool in tools] if tools else [],
        )


@dataclass
class EvalScenario:
    """A named conversation scenario with optional latency/cost budgets."""

    name: str
    turns: list[EvalTurn]
    budgets: list[LatencyBudget | CostBudget] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvalScenario:
        """Build a scenario from a parsed YAML/JSON mapping.

        The ``budgets`` block mirrors the documented schema::

            budgets:
              latency:
                - {stage: total_ms, max_ms: 1500}
              cost:
                max_session_cost_usd: 0.05
        """
        name = str(payload.get("name") or "")
        if not name:
            raise ValueError("scenario file must define a non-empty 'name'")
        turns_raw = payload.get("turns")
        if not isinstance(turns_raw, Sequence) or not turns_raw:
            raise ValueError(f"scenario {name!r} must define at least one turn")
        turns = [EvalTurn.from_dict(turn) for turn in turns_raw]
        budgets = _parse_budgets(payload.get("budgets"))
        return cls(name=name, turns=turns, budgets=budgets)


def _parse_budgets(payload: Any) -> list[LatencyBudget | CostBudget]:
    budgets: list[LatencyBudget | CostBudget] = []
    if not isinstance(payload, Mapping):
        return budgets
    latency = payload.get("latency")
    if isinstance(latency, Sequence):
        for entry in latency:
            if not isinstance(entry, Mapping):
                continue
            budgets.append(
                LatencyBudget(
                    stage=str(entry["stage"]),
                    max_ms=float(entry["max_ms"]),
                    percentile=str(entry.get("percentile", "p95")),
                )
            )
    cost = payload.get("cost")
    if isinstance(cost, Mapping):
        budgets.append(CostBudget.from_dict(dict(cost)))
    return budgets


def load_scenario(path: str | Path) -> EvalScenario:
    """Load one :class:`EvalScenario` from a ``.yaml``/``.yml``/``.json`` file.

    JSON is parsed with the stdlib; YAML requires ``pyyaml`` and raises a clear
    error when it is missing.
    """
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    if file_path.suffix.lower() in (".yaml", ".yml"):
        payload = _load_yaml(text, file_path)
    else:
        payload = json.loads(text)
    if not isinstance(payload, Mapping):
        raise ValueError(f"scenario file {file_path} must contain a mapping")
    return EvalScenario.from_dict(payload)


def _load_yaml(text: str, file_path: Path) -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - depends on optional dep
        raise RuntimeError(
            f"reading YAML scenario {file_path} requires pyyaml; "
            "install it or use a .json scenario file."
        ) from exc
    return yaml.safe_load(text)
