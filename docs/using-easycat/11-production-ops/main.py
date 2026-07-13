"""Chapter 11 — Check EasyCat's production operating contract offline.

Dependencies:
    uv sync --group dev

Run:
    uv run python docs/using-easycat/11-production-ops/main.py
    uv run python docs/using-easycat/11-production-ops/main.py \
        --data-dir .easycat/tutorial/ch11
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from easycat.runtime import (
    JournalRecordKind,
    ReadonlySqliteJournal,
    SqliteJournal,
    sweep_crashed_journals,
)
from easycat.server import BearerTokenAuth, VoiceServerConfig, VoiceServerHealth
from easycat.server import metrics as server_metrics

SESSION_ID = "chapter-11-ops-checkpoint"


def parse_data_dir() -> Path | None:
    parser = argparse.ArgumentParser(description="Run the offline production-ops checkpoint.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Keep the SQLite journal under this EasyCat data directory.",
    )
    return parser.parse_args().data_dir


def check_process_policy() -> VoiceServerConfig:
    config = VoiceServerConfig(
        host="0.0.0.0",
        port=8080,
        max_sessions=4,
        drain_timeout_s=20.0,
        force_shutdown_timeout_s=5.0,
        auth=BearerTokenAuth(token="offline-checkpoint-token"),
    )
    assert config.auth is not None
    assert config.drain_timeout_s + config.force_shutdown_timeout_s == 25.0
    print("PASS policy: public bind has auth, capacity, and bounded drain windows")
    return config


def check_health_and_metrics(config: VoiceServerConfig) -> None:
    serving = VoiceServerHealth(
        state="serving",
        active_sessions=2,
        max_sessions=config.max_sessions,
        draining=False,
        route_stack_ready=True,
        manifest_loaded=True,
        plan_blocking_errors=(),
    )
    draining = replace(serving, state="draining", draining=True)
    assert serving.is_ready() is True
    assert draining.is_ready() is False
    assert draining.readiness_failures() == ("draining",)

    server_metrics.record_request(
        "/health/ready",
        duration_s=0.01,
        server_state="serving",
    )
    server_metrics.observe_connections_active(2, server_state="serving")
    server_metrics.observe_draining(False)
    try:
        server_metrics.record_request(
            "/health/ready?token=secret",
            duration_s=0.01,
            server_state="serving",
        )
    except ValueError as exc:
        assert "enumerated route template" in str(exc)
    else:
        raise AssertionError("a raw path reached the metric-label surface")
    print("PASS health: draining fails readiness and raw metric paths are rejected")


def check_durable_postmortem(data_dir: Path) -> Path:
    journal = SqliteJournal(SESSION_ID, data_dir=data_dir)
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="deployment_started",
        session_id=SESSION_ID,
        data={"revision": "offline-demo", "environment": "test"},
    )
    journal.append(
        kind=JournalRecordKind.METRIC,
        name="readiness_checked",
        session_id=SESSION_ID,
        data={"ready": True, "active_sessions": 2},
    )
    db_path = journal.db_path
    journal.close()

    postmortem = ReadonlySqliteJournal(db_path)
    records = postmortem.read()
    assert [record.name for record in records] == ["deployment_started", "readiness_checked"]
    assert postmortem.degraded is False
    assert sweep_crashed_journals(data_dir) == 0
    assert (
        postmortem.append(
            kind=JournalRecordKind.EVENT,
            name="late_write",
            session_id=SESSION_ID,
        )
        == -1
    )
    print("PASS durability: clean SQLite journal reopened as a read-only postmortem")
    return db_path


def checkpoint(data_dir: Path | None) -> None:
    context = TemporaryDirectory(prefix="easycat-ch11-") if data_dir is None else nullcontext()
    with context as temporary:
        root = data_dir or Path(temporary)
        config = check_process_policy()
        check_health_and_metrics(config)
        db_path = check_durable_postmortem(root)
        if data_dir is not None:
            print(f"Journal: {db_path}")


def main() -> None:
    checkpoint(parse_data_dir())


if __name__ == "__main__":
    main()
