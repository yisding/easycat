from __future__ import annotations

import pytest

from easycat import (
    ObservabilityConfig,
    create_text_session,
)
from easycat.validation import LatencyBudget
from tests.config._helpers import (
    _DummyAgent,
)


def test_create_text_session_forwards_observability_advanced_aliases():
    budget = LatencyBudget(stage="total_ms", max_ms=1000.0)
    session = create_text_session(
        agent=_DummyAgent(),
        latency_budget=budget,
        warmup=False,
        max_session_cost_usd=0.25,
    )

    assert session._easycat_config.latency_budget == (budget,)
    assert session._easycat_config.warmup is False
    assert session._easycat_config.max_session_cost_usd == 0.25
    assert session._run_ctx.latency_budgets == (budget,)
    assert session._config.warmup is False
    assert session._warmup.enabled is False


def test_create_text_session_accepts_config_object():
    from easycat.config import TextSessionConfig, create_text_session

    config = TextSessionConfig(agent=_DummyAgent(), debug="off")
    session = create_text_session(config)
    assert session is not None


def test_create_text_session_kwargs_still_supported():
    from easycat.config import create_text_session

    session = create_text_session(agent=_DummyAgent(), debug="off")
    assert session is not None


def test_text_session_config_validates_debug():
    from easycat.config import TextSessionConfig

    with pytest.raises(ValueError, match="Invalid debug"):
        TextSessionConfig(agent=_DummyAgent(), debug="loud")  # type: ignore[arg-type]


def test_text_session_config_observability_keeps_legacy_top_level_aliases():
    from easycat.config import TextSessionConfig

    config = TextSessionConfig(
        agent=_DummyAgent(),
        observability=ObservabilityConfig(journal_backend="libsql"),
        debug="light",
        journal_retention="delete",
    )

    assert config.observability == ObservabilityConfig(
        debug="light",
        journal_backend="libsql",
        journal_retention="delete",
    )
    assert config.debug == "light"
    assert config.journal_backend == "libsql"
    assert config.journal_retention == "delete"

    config.journal_backend = "sqlite"
    assert config.observability.journal_backend == "sqlite"


def test_create_text_session_rejects_config_plus_loose_kwargs():
    from easycat.config import TextSessionConfig, create_text_session

    config = TextSessionConfig(agent=_DummyAgent())
    with pytest.raises(ValueError, match="not both"):
        create_text_session(config, agent=_DummyAgent())


def test_create_text_session_rejects_config_plus_loose_record_to(tmp_path):
    from easycat.config import TextSessionConfig, create_text_session

    config = TextSessionConfig(agent=_DummyAgent())
    with pytest.raises(ValueError, match="record_to"):
        create_text_session(config, record_to=tmp_path)


def test_create_text_session_config_with_default_kwargs_ok():
    from easycat.config import TextSessionConfig, create_text_session

    # Passing config alongside only default-valued kwargs is allowed.
    config = TextSessionConfig(agent=_DummyAgent(), debug="off")
    session = create_text_session(config, debug="off")
    assert session is not None
