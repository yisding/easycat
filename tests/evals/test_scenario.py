from __future__ import annotations

import json
from pathlib import Path

import pytest

from easycat.budgets import CostBudget, LatencyBudget
from easycat.evals.scenario import EvalScenario, EvalTurn, load_scenario


def test_eval_turn_from_dict_nested_expect() -> None:
    turn = EvalTurn.from_dict(
        {"user": "I need a refund", "expect": {"response_regex": "refund", "tools": ["lookup"]}}
    )
    assert turn.user == "I need a refund"
    assert turn.expect_response_regex == "refund"
    assert turn.expect_tools == ["lookup"]


def test_eval_turn_from_dict_flat_shape() -> None:
    turn = EvalTurn.from_dict(
        {"user": "hi", "expect_response_regex": "hello", "expect_tools": ["a", "b"]}
    )
    assert turn.expect_response_regex == "hello"
    assert turn.expect_tools == ["a", "b"]


def test_eval_turn_defaults() -> None:
    turn = EvalTurn(user="hi")
    assert turn.expect_response_regex is None
    assert turn.expect_tools == []


def test_scenario_from_dict_parses_budgets() -> None:
    scenario = EvalScenario.from_dict(
        {
            "name": "refund_flow",
            "budgets": {
                "latency": [{"stage": "total_ms", "max_ms": 1500}],
                "cost": {"max_session_cost_usd": 0.05},
            },
            "turns": [{"user": "I need a refund", "expect": {"response_regex": "refund"}}],
        }
    )
    assert scenario.name == "refund_flow"
    assert len(scenario.turns) == 1
    latency = [b for b in scenario.budgets if isinstance(b, LatencyBudget)]
    cost = [b for b in scenario.budgets if isinstance(b, CostBudget)]
    assert latency[0].stage == "total_ms"
    assert latency[0].max_ms == 1500.0
    assert cost[0].max_session_usd == 0.05


def test_scenario_requires_name() -> None:
    with pytest.raises(ValueError, match="non-empty 'name'"):
        EvalScenario.from_dict({"turns": [{"user": "hi"}]})


def test_scenario_requires_turns() -> None:
    with pytest.raises(ValueError, match="at least one turn"):
        EvalScenario.from_dict({"name": "empty", "turns": []})


def test_load_scenario_json(tmp_path: Path) -> None:
    path = tmp_path / "scenario.json"
    path.write_text(
        json.dumps(
            {
                "name": "json_flow",
                "turns": [{"user": "ping", "expect": {"response_regex": "ping"}}],
            }
        ),
        encoding="utf-8",
    )
    scenario = load_scenario(path)
    assert scenario.name == "json_flow"
    assert scenario.turns[0].expect_response_regex == "ping"


def test_load_scenario_yaml(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    path = tmp_path / "scenario.yaml"
    path.write_text(
        "\n".join(
            [
                "name: yaml_flow",
                "budgets:",
                "  latency:",
                "    - stage: total_ms",
                "      max_ms: 1500",
                "turns:",
                "  - user: I need a refund",
                "    expect:",
                "      response_regex: refund|order",
            ]
        ),
        encoding="utf-8",
    )
    scenario = load_scenario(path)
    assert scenario.name == "yaml_flow"
    assert scenario.budgets[0].stage == "total_ms"
    assert scenario.turns[0].expect_response_regex == "refund|order"
