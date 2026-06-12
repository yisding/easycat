from __future__ import annotations

from datetime import UTC, datetime

from easycat.validation.report import (
    ArtifactRef,
    GitMetadata,
    ValidationCheck,
    ValidationEnvironment,
    ValidationRun,
)


def _validation_run(**overrides) -> ValidationRun:  # noqa: ANN003
    values = {
        "run_id": "20260521T120000Z-quick-12345",
        "command": ["uv", "run", "pytest", "-q"],
        "started_at": datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 5, 21, 12, 0, 3, tzinfo=UTC),
        "duration_s": 3.25,
        "status": "pass",
        "exit_code": 0,
        "tool_exit_codes": {"pytest": 0},
        "git": GitMetadata(sha="abc123", branch="feature/validation", dirty=True),
        "environment": ValidationEnvironment(
            python="3.12.13",
            platform="Linux",
            ci=False,
            env_vars={"OPENAI_API_KEY": True, "DEEPGRAM_API_KEY": False},
        ),
        "checks": [
            ValidationCheck(
                name="pytest.quick",
                status="pass",
                duration_s=2.75,
                command=["uv", "run", "pytest", "-q"],
                artifacts={
                    "junit": ArtifactRef(
                        kind="junit",
                        path=".easycat/validation/runs/20260521T120000Z-quick-12345/junit.xml",
                    )
                },
            )
        ],
    }
    values.update(overrides)
    return ValidationRun(**values)
