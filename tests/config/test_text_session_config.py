from __future__ import annotations

import pytest

from easycat import create_text_session
from tests.config._helpers import (
    _DummyAgent,
)


def test_create_text_session_forwards_warmup():
    session = create_text_session(
        agent=_DummyAgent(),
        warmup=False,
    )

    assert session._easycat_config.warmup is False
    assert session._config.warmup is False
    assert session._warmup.enabled is False


def test_text_session_config_defaults_debug_to_light():
    from easycat.config import TextSessionConfig

    config = TextSessionConfig(agent=_DummyAgent())
    assert config.debug == "light"


def test_create_text_session_defaults_build_memory_journal(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from easycat.runtime import InMemoryRingBuffer, SqliteJournal

    monkeypatch.setenv("EASYCAT_DATA_DIR", str(tmp_path))
    session = create_text_session(agent=_DummyAgent())
    try:
        # Default ``debug="light"``: a read view is exposed but the backing
        # journal is the in-memory ring buffer, not the durable SqliteJournal
        # (which is the opt-in ``debug="full"`` mode).
        assert session.journal is not None
        assert isinstance(session._journal, InMemoryRingBuffer)
        assert not isinstance(session._journal, SqliteJournal)
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
    # The default is debug="light"; passing the matching default kwarg
    # is treated as "unset" by the config-vs-loose mutual-exclusion check.
    config = TextSessionConfig(agent=_DummyAgent(), debug="off")
    session = create_text_session(config, debug="light")
    assert session is not None
