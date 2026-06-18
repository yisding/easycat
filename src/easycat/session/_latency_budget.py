"""Runtime latency-budget records for session-level metrics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from easycat.runtime.records import JournalRecordKind


class JournalSink(Protocol):
    """Journal surface needed for latency-budget records."""

    def append_record(
        self,
        *,
        name: str,
        kind: JournalRecordKind = JournalRecordKind.EVENT,
        turn_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class LatencyBudgetMonitor:
    """Tag and alert on aggregate runtime latency budget violations."""

    journal_sink: JournalSink
    budgets: Sequence[Any]

    def record_metric(
        self,
        *,
        name: str,
        turn_id: str | None,
        stage: str,
        observed_ms: float,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Append a metric record and one alert record per exceeded budget."""
        payload = dict(data or {})
        payload.setdefault("value", observed_ms)
        violations = self.violations(stage=stage, observed_ms=observed_ms)
        if violations:
            payload["latency_budget_exceeded"] = True
            payload["latency_budget_violations"] = violations

        self.journal_sink.append_record(
            kind=JournalRecordKind.METRIC,
            name=name,
            turn_id=turn_id,
            data=payload,
        )
        for violation in violations:
            self.journal_sink.append_record(
                kind=JournalRecordKind.METRIC,
                name="latency_budget_exceeded",
                turn_id=turn_id,
                data={
                    **violation,
                    "trigger_record_name": name,
                },
            )

    def has_budget_for(self, stage: str) -> bool:
        """Return True when at least one configured budget targets ``stage``."""
        return any(
            _budget_matches_stage(stage, _budget_value(budget, "stage")) for budget in self.budgets
        )

    def violations(self, *, stage: str, observed_ms: float) -> list[dict[str, float | str]]:
        """Return budget violations for a single observed aggregate metric."""
        results: list[dict[str, float | str]] = []
        for budget in self.budgets:
            budget_stage = _budget_value(budget, "stage")
            if not _budget_matches_stage(stage, budget_stage):
                continue
            budget_ms = _float_or_none(_budget_value(budget, "max_ms"))
            if budget_ms is None or observed_ms <= budget_ms:
                continue
            results.append(
                {
                    "stage": str(budget_stage),
                    "observed_ms": observed_ms,
                    "budget_ms": budget_ms,
                    "percentile": str(_budget_value(budget, "percentile") or "p95"),
                    "scope": "turn_metric",
                }
            )
        return results


# Waterfall ``*_to_*_ms`` milestone names that map onto the flat runtime
# metric stages emitted by the turn runner. Kept in lock-step with
# ``easycat.budgets.report.WATERFALL_STAGE_ALIASES`` so a single
# ``LatencyBudget(stage=...)`` matches whether it is expressed as the flat
# runtime stage or the waterfall milestone name.
_WATERFALL_STAGE_ALIASES: dict[str, str] = {
    "vad_endpoint_to_stt_final_ms": "stt_final_latency_ms",
    "agent_request_to_first_token_ms": "llm_ttft_ms",
    "agent_first_token_to_tts_first_byte_ms": "tts_ttfb_ms",
    "vad_endpoint_to_tts_first_byte_ms": "first_audio_ms",
    "user_speech_start_to_bot_stopped_ms": "barge_in_ack_ms",
}


def _budget_matches_stage(stage: str, budget_stage: Any) -> bool:
    if budget_stage is None:
        return False
    key = str(budget_stage).strip()
    # A budget may target the flat runtime stage directly, the bare stage with
    # an ``_ms``/``_latency_ms`` suffix, or the waterfall milestone name that
    # lifts onto the same flat stage.
    key = _WATERFALL_STAGE_ALIASES.get(key, key)
    return key in {stage, f"{stage}_ms", f"{stage}_latency_ms"}


def _budget_value(budget: Any, name: str) -> Any:
    if isinstance(budget, Mapping):
        return budget.get(name)
    return getattr(budget, name, None)


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = ["LatencyBudgetMonitor"]
