"""Text-first eval scenario runner.

:class:`EvalRunner` replays an :class:`~easycat.evals.scenario.EvalScenario`
against a text-mode session built with
:func:`~easycat.config.create_text_session`, driving each turn through the
existing :func:`easycat.debug.testing.run_text_turn` path. It returns a
journal-backed :class:`ScenarioResult` whose :meth:`ScenarioResult.records` is
compatible with the bundle assertion helpers re-exported from
:mod:`easycat.evals`.

Budget handling (TEST-4)
------------------------
Text turns emit ONLY ``stage="total_ms"`` runtime latency records; the runtime
budget monitor is push-based, so a provider-stage latency budget (e.g.
``tts_ttfb_ms`` / ``llm_ttft_ms``) attached to a text-mode scenario would
evaluate against ZERO samples and pass VACUOUSLY. To avoid silently green-
lighting a regression, the runner DETECTS such a budget before running and
RAISES a clear ``"no samples for stage X"`` error. Text scenarios may therefore
assert only turn-total (``total_ms``) latency budgets plus cost budgets.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from easycat.budgets import (
    BudgetReport,
    LatencyBudget,
    build_budget_report,
    normalize_budget_stage,
)
from easycat.evals.scenario import EvalScenario, EvalTurn

__all__ = ["EvalRunner", "ScenarioResult", "TurnRecord"]

# The only latency stage a text-mode scenario can sample. Anything else is a
# provider-stage budget that text mode cannot observe (TEST-4).
_TEXT_MODE_LATENCY_STAGE = "total_ms"


@dataclass(frozen=True)
class TurnRecord:
    """The captured outcome of one replayed scenario turn."""

    user: str
    response: str
    turn_id: str
    latency_ms: float
    journal_records: tuple[dict[str, Any], ...] = field(default=())
    assertion_errors: tuple[str, ...] = field(default=())

    @property
    def passed(self) -> bool:
        return not self.assertion_errors


@dataclass(frozen=True)
class ScenarioResult:
    """Journal-backed result of running one scenario.

    :meth:`records` yields the flat journal-record dicts captured across every
    turn, in the same shape as ``RunBundle.records()`` / ``TurnResult.records()``
    so the re-exported ``assert_*`` helpers accept a ``ScenarioResult`` directly.
    """

    name: str
    turns: tuple[TurnRecord, ...] = field(default=())
    budget_report: BudgetReport = field(default_factory=BudgetReport)

    def records(self) -> Iterable[dict[str, Any]]:
        """Iterate every journal record captured across all turns."""
        for turn in self.turns:
            yield from turn.journal_records

    @property
    def assertion_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        for turn in self.turns:
            errors.extend(turn.assertion_errors)
        return tuple(errors)

    @property
    def passed(self) -> bool:
        return not self.assertion_errors and self.budget_report.passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "turns": [
                {
                    "user": turn.user,
                    "response": turn.response,
                    "turn_id": turn.turn_id,
                    "latency_ms": turn.latency_ms,
                    "passed": turn.passed,
                    "assertion_errors": list(turn.assertion_errors),
                }
                for turn in self.turns
            ],
            "budget_report": self.budget_report.to_dict(),
        }


class EvalRunner:
    """Run text-mode :class:`EvalScenario` objects against an agent.

    The runner builds one text session per :meth:`run` call (audio stages are
    Noop stubs) and replays each turn, capturing journal records and per-turn
    latency. Pass an ``agent`` (any object ``create_text_session`` accepts) at
    construction, or supply one per :meth:`run`.
    """

    def __init__(self, agent: Any = None) -> None:
        self._agent = agent

    async def run(self, scenario: EvalScenario, *, agent: Any = None) -> ScenarioResult:
        """Replay ``scenario`` and return a journal-backed result.

        Raises :class:`ValueError` when a provider-stage latency budget is
        attached (text mode cannot sample those — TEST-4).
        """
        _reject_provider_stage_budgets(scenario)

        target = agent if agent is not None else self._agent
        if target is None:
            raise ValueError(
                "EvalRunner needs an agent; pass agent= to the constructor or to run()"
            )

        from easycat.config import create_text_session
        from easycat.debug.testing import run_text_turn

        session = create_text_session(agent=target, debug="light")
        turn_records: list[TurnRecord] = []
        try:
            for turn in scenario.turns:
                result = await run_text_turn(session, turn.user)
                turn_records.append(_score_turn(turn, result))
        finally:
            await session.stop(force=True)

        all_records = [record for turn in turn_records for record in turn.journal_records]
        latency_observations = [
            {"stage": _TEXT_MODE_LATENCY_STAGE, "observed_ms": turn.latency_ms}
            for turn in turn_records
        ]
        report = build_budget_report(all_records + latency_observations, scenario.budgets)

        return ScenarioResult(
            name=scenario.name,
            turns=tuple(turn_records),
            budget_report=report,
        )


def _reject_provider_stage_budgets(scenario: EvalScenario) -> None:
    """Raise if a latency budget targets a stage text mode cannot sample."""
    for budget in scenario.budgets:
        if not isinstance(budget, LatencyBudget):
            continue
        if normalize_budget_stage(budget.stage) != _TEXT_MODE_LATENCY_STAGE:
            raise ValueError(
                f"no samples for stage {budget.stage!r}: text scenarios emit only "
                f"{_TEXT_MODE_LATENCY_STAGE!r} latency records, so a provider-stage "
                "budget would pass vacuously. Assert provider-stage latency budgets "
                "with the audio-simulation runner instead."
            )


def _score_turn(turn: EvalTurn, result: Any) -> TurnRecord:
    """Apply a turn's expectations to a captured ``TurnResult``."""
    from easycat.debug.testing import assert_regex, assert_tool_called

    journal_records = tuple(result.records())
    errors: list[str] = []

    if turn.expect_response_regex is not None:
        try:
            assert_regex(result, pattern=turn.expect_response_regex)
        except AssertionError as exc:
            errors.append(str(exc))

    for tool_name in turn.expect_tools:
        try:
            assert_tool_called(result, tool_name=tool_name)
        except AssertionError as exc:
            errors.append(str(exc))

    return TurnRecord(
        user=turn.user,
        response=result.response,
        turn_id=result.turn_id,
        latency_ms=result.latency_ms,
        journal_records=journal_records,
        assertion_errors=tuple(errors),
    )
