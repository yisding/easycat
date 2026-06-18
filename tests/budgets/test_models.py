from __future__ import annotations

import pytest

from easycat.budgets import CostBudget, LatencyBudget


def test_cost_budget_defaults() -> None:
    budget = CostBudget(max_session_usd=0.05)
    assert budget.max_session_usd == pytest.approx(0.05)
    assert budget.warn_at == pytest.approx(0.8)
    assert budget.action == "stop"
    assert budget.warn_at_usd == pytest.approx(0.04)


def test_cost_budget_is_frozen() -> None:
    budget = CostBudget(max_session_usd=1.0)
    with pytest.raises(AttributeError):
        budget.max_session_usd = 2.0  # type: ignore[misc]


def test_cost_budget_max_session_cost_usd_alias_matches_ceiling() -> None:
    budget = CostBudget(max_session_usd=0.25)
    assert budget.max_session_cost_usd == pytest.approx(0.25)
    assert budget.max_session_cost_usd == budget.max_session_usd


def test_cost_budget_serialization_round_trip() -> None:
    budget = CostBudget(max_session_usd=0.5, warn_at=0.6, action="warn")
    payload = budget.to_dict()
    assert payload == {
        "max_session_usd": 0.5,
        "warn_at": 0.6,
        "action": "warn",
    }
    assert CostBudget.from_dict(payload) == budget


def test_cost_budget_from_dict_accepts_config_alias_key() -> None:
    # The config field is spelled ``max_session_cost_usd``; ``from_dict`` must
    # accept it so config payloads round-trip into the value object.
    budget = CostBudget.from_dict({"max_session_cost_usd": 0.05})
    assert budget.max_session_usd == pytest.approx(0.05)
    assert budget.action == "stop"


def test_cost_budget_from_dict_prefers_canonical_key() -> None:
    budget = CostBudget.from_dict({"max_session_usd": 0.1, "max_session_cost_usd": 0.9})
    assert budget.max_session_usd == pytest.approx(0.1)


def test_cost_budget_from_dict_requires_a_ceiling() -> None:
    with pytest.raises(KeyError):
        CostBudget.from_dict({"warn_at": 0.5})


@pytest.mark.parametrize("ceiling", [0.0, -1.0])
def test_cost_budget_rejects_non_positive_ceiling(ceiling: float) -> None:
    with pytest.raises(ValueError):
        CostBudget(max_session_usd=ceiling)


@pytest.mark.parametrize("warn_at", [0.0, 1.5, -0.1])
def test_cost_budget_rejects_out_of_range_warn_at(warn_at: float) -> None:
    with pytest.raises(ValueError):
        CostBudget(max_session_usd=1.0, warn_at=warn_at)


def test_cost_budget_rejects_unknown_action() -> None:
    with pytest.raises(ValueError):
        CostBudget(max_session_usd=1.0, action="halt")  # type: ignore[arg-type]


def test_latency_budget_reexport_is_the_validation_symbol() -> None:
    from easycat.validation.latency import LatencyBudget as ValidationLatencyBudget

    assert LatencyBudget is ValidationLatencyBudget


def test_latency_budget_legacy_import_still_works() -> None:
    # The legacy import path must keep working unchanged.
    from easycat.validation.latency import LatencyBudget as Legacy

    budget = Legacy(stage="total_ms", max_ms=1500.0)
    assert budget.stage == "total_ms"
    assert budget.max_ms == pytest.approx(1500.0)
