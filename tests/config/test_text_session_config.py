from __future__ import annotations

import pytest

from easycat import (
    ObservabilityConfig,
    create_text_session,
)
from tests.config._helpers import (
    _DummyAgent,
)


def test_create_text_session_forwards_observability_advanced_aliases():
    session = create_text_session(
        agent=_DummyAgent(),
        warmup=False,
    )

    assert session._easycat_config.warmup is False
    assert session._config.warmup is False
    assert session._warmup.enabled is False


def test_text_session_config_defaults_debug_to_full():
    from easycat.config import TextSessionConfig

    config = TextSessionConfig(agent=_DummyAgent())
    assert config.debug == "full"
    assert config.observability.debug == "full"


def test_create_text_session_defaults_build_sqlite_journal(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from easycat.runtime import InMemoryRingBuffer, SqliteJournal

    monkeypatch.setenv("EASYCAT_DATA_DIR", str(tmp_path))
    session = create_text_session(agent=_DummyAgent())
    try:
        # Durable journaling on by default: a read view is exposed and the
        # backing journal is a SqliteJournal, not the in-memory ring buffer.
        assert session.journal is not None
        assert isinstance(session._journal, SqliteJournal)
        assert not isinstance(session._journal, InMemoryRingBuffer)
    finally:
        session._journal.close()


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
    # The default is now debug="full"; passing the matching default kwarg
    # is treated as "unset" by the config-vs-loose mutual-exclusion check.
    config = TextSessionConfig(agent=_DummyAgent(), debug="off")
    session = create_text_session(config, debug="full")
    assert session is not None
