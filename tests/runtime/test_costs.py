from __future__ import annotations

import math

import pytest

from easycat.runtime import (
    COST_WARNING_FRACTION,
    cost_budget_status,
    finite_number,
    max_session_cost_usd_from_snapshot,
)


def test_finite_number_rejects_bool_nan_and_non_numbers() -> None:
    assert finite_number(1) == 1.0
    assert finite_number(1.5) == 1.5
    assert finite_number(True) is None
    assert finite_number("1.0") is None
    assert finite_number(math.nan) is None
    assert finite_number(math.inf) is None


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        ({"max_session_cost_usd": 1.25}, 1.25),
        ({"max_session_cost_usd": "1.25"}, 1.25),
        ({"max_session_cost_usd": "1e-3"}, 0.001),
        ({"max_session_cost_usd": " 1.25 "}, 1.25),
        ({"max_session_cost_usd": "[1.25]"}, None),
        ({"max_session_cost_usd": "1" * 65}, None),
        ({"max_session_cost_usd": 0}, None),
        ({"max_session_cost_usd": -1}, None),
        ({"max_session_cost_usd": True}, None),
        ({"max_session_cost_usd": "not-a-number"}, None),
        (None, None),
    ],
)
def test_max_session_cost_usd_from_snapshot(snapshot: dict[str, object] | None, expected: float):
    assert max_session_cost_usd_from_snapshot(snapshot) == expected


def test_max_session_cost_usd_rejects_large_literal_string_without_parsing() -> None:
    malicious_literal = "[" + ",".join(["1"] * 10_000) + "]"

    assert max_session_cost_usd_from_snapshot({"max_session_cost_usd": malicious_literal}) is None


def test_cost_budget_status_unconfigured() -> None:
    assert cost_budget_status(0.25, None) == {
        "configured": False,
        "max_session_cost_usd": None,
        "warning_threshold_usd": None,
        "usage_fraction": None,
        "remaining_usd": None,
        "overage_usd": None,
        "status": "unconfigured",
        "warning": False,
        "exceeded": False,
    }


def test_cost_budget_status_ok_warning_and_exceeded() -> None:
    ok = cost_budget_status(0.25, 1.0)
    assert ok["status"] == "ok"
    assert ok["warning"] is False
    assert ok["exceeded"] is False
    assert ok["warning_threshold_usd"] == pytest.approx(COST_WARNING_FRACTION)

    warning = cost_budget_status(0.8, 1.0)
    assert warning["status"] == "warning"
    assert warning["warning"] is True
    assert warning["exceeded"] is False
    assert warning["remaining_usd"] == pytest.approx(0.2)

    exceeded = cost_budget_status(1.25, 1.0)
    assert exceeded["status"] == "exceeded"
    assert exceeded["warning"] is True
    assert exceeded["exceeded"] is True
    assert exceeded["remaining_usd"] == 0.0
    assert exceeded["overage_usd"] == pytest.approx(0.25)
