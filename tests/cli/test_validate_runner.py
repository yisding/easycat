from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from easycat.validation._environment import RUNTIME_SECRET_ENV_VARS, runtime_secret_values
from easycat.validation.latency import (
    LatencySample,
    LatencyStageDurations,
    ReliabilitySample,
    ReliabilitySignals,
)
from easycat.validation.runner import (
    VALIDATION_SELECTORS,
    CommandResult,
    main,
    run_release_validation,
    run_validation_slice,
)
from scripts._justfile import just_recipe_commands

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_justfile_test_fast_and_cov_recipes_match_quick_validation_selector() -> None:
    recipes = just_recipe_commands(REPO_ROOT)
    for recipe in ("test-fast", "cov"):
        command = recipes[recipe]
        match = re.search(r'-m "(?P<expr>[^"]+)"', command)
        assert match is not None, f"justfile `{recipe}` recipe has no -m expression"
        assert match.group("expr") == VALIDATION_SELECTORS["quick"], (
            f"justfile `{recipe}` -m expression drifted from VALIDATION_SELECTORS['quick']"
        )


def test_validation_runner_quick_writes_report_junit_logs_and_latest(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    secret = "sk-" + ("b" * 32)

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        commands.append(command)
        junit_arg = next(arg for arg in command if arg.startswith("--junitxml="))
        Path(junit_arg.removeprefix("--junitxml=")).write_text("<testsuite />")
        return CommandResult(exit_code=0, stdout=f"ok {secret}", stderr="")

    result = run_validation_slice(
        "quick",
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC),
    )

    assert result.exit_code == 0
    assert len(commands) == 1
    command = commands[0]
    assert command[:4] == ["uv", "run", "pytest", "-q"]
    # The quick slice is the only parallel lane (xdist-safe by design).
    assert command[4:8] == ["-n", "auto", "--dist", "load"]
    assert command[-2:] == [
        "-m",
        (
            "not integration_socket and not integration_live and not integration_external "
            "and not contract and not slow and not stress and not serial and not flaky "
            "and not guard"
        ),
    ]
    assert any(arg.startswith("--junitxml=") for arg in command)

    report_path = result.run_dir / "report.json"
    latest_path = tmp_path / "latest.json"
    stdout_path = result.run_dir / "stdout.log"
    assert report_path.exists()
    assert latest_path.read_text() == report_path.read_text()
    assert secret not in stdout_path.read_text()

    payload = json.loads(report_path.read_text())
    assert payload["status"] == "pass"
    assert payload["exit_code"] == 0
    assert payload["tool_exit_codes"] == {"pytest": 0}
    assert payload["checks"][0]["name"] == "pytest.quick"
    assert payload["checks"][0]["artifacts"]["junit"]["path"].endswith("/junit.xml")


def test_validation_runner_embeds_reliability_samples_for_stress_slices(tmp_path: Path) -> None:
    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        reliability_path = Path(env["EASYCAT_RELIABILITY_SAMPLES_PATH"])
        reliability_path.write_text(
            json.dumps(
                [
                    ReliabilitySample(
                        sample_id="stress-1",
                        condition_id="fifty_turns_single_session_scripted",
                        mode="stress",
                        informational=True,
                        eligible=False,
                        signals=ReliabilitySignals(
                            journal_degraded=False,
                            active_sessions=1,
                            memory_growth_kib=128,
                        ),
                    ).to_dict()
                ]
            )
        )
        return CommandResult(exit_code=0, stdout="", stderr="")

    result = run_validation_slice(
        "stress",
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
    )

    report = json.loads(result.report_path.read_text())
    assert report["status"] == "pass"
    assert report["reliability"]["kind"] == "reliability_validation"
    assert report["reliability"]["samples"][0]["sample_id"] == "stress-1"
    assert report["reliability"]["samples"][0]["signals"]["journal_degraded"] is False
    assert "queue_depth" not in report["reliability"]["samples"][0]["signals"]
    assert "reliability" in report["checks"][0]["artifacts"]


def test_socket_validation_reports_webrtc_stats_artifact_when_produced(tmp_path: Path) -> None:
    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        stats_path = Path(env["EASYCAT_WEBRTC_STATS_PATH"])
        stats_path.write_text(
            json.dumps(
                {
                    "kind": "webrtc_client_stats",
                    "schema_version": 1,
                    "sample_id": "socket-1",
                    "label": "teardown",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return CommandResult(exit_code=0, stdout="", stderr="")

    result = run_validation_slice(
        "socket",
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
    )

    payload = result.run.to_dict()
    artifact = payload["checks"][0]["artifacts"]["webrtc_stats"]
    assert artifact["kind"] == "webrtc_stats"
    assert (
        Path(artifact["path"])
        .read_text(encoding="utf-8")
        .startswith('{"kind": "webrtc_client_stats"')
    )
    assert payload["artifacts"]["webrtc_stats"] == artifact


def test_validation_runner_failed_pytest_still_writes_report(tmp_path: Path) -> None:
    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        return CommandResult(exit_code=5, stdout="", stderr="no tests collected")

    result = run_validation_slice(
        "socket",
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC),
    )

    assert result.exit_code == 1
    payload = json.loads((result.run_dir / "report.json").read_text())
    assert payload["status"] == "fail"
    assert payload["exit_code"] == 1
    assert payload["tool_exit_codes"] == {"pytest": 5}
    assert (tmp_path / "latest.json").exists()


def test_validation_runner_missing_command_still_writes_failed_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable pytest launcher must not skip the validation report."""

    def missing_command(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError(2, "No such file or directory", "missing-validation-command")

    monkeypatch.setattr("easycat.validation._runner_support.subprocess.run", missing_command)

    result = run_validation_slice(
        "quick",
        artifacts_dir=tmp_path,
        started_at=datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC),
    )

    assert result.exit_code == 1
    payload = json.loads(result.report_path.read_text())
    assert payload["status"] == "fail"
    assert payload["tool_exit_codes"] == {"pytest": 127}
    assert "missing-validation-command" in (result.run_dir / "stderr.log").read_text()
    assert (tmp_path / "latest.json").exists()


@pytest.mark.parametrize(
    "runtime_secret_env_var",
    RUNTIME_SECRET_ENV_VARS,
)
def test_validation_runner_redacts_exact_runtime_secret_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_secret_env_var: str,
) -> None:
    secret = "plain-runtime-token-value"
    monkeypatch.setenv(runtime_secret_env_var, secret)

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        junit_arg = next(arg for arg in command if arg.startswith("--junitxml="))
        Path(junit_arg.removeprefix("--junitxml=")).write_text(f"<testsuite>{secret}</testsuite>")
        return CommandResult(
            exit_code=1,
            stdout=f"stdout {secret}",
            stderr=f"stderr {secret}",
        )

    result = run_validation_slice(
        "quick",
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC),
    )

    assert result.exit_code == 1
    assert secret not in (result.run_dir / "stdout.log").read_text()
    assert secret not in (result.run_dir / "stderr.log").read_text()
    assert secret not in (result.run_dir / "junit.xml").read_text()

    report_text = result.report_path.read_text()
    assert secret not in report_text
    payload = json.loads(report_text)
    assert payload["failures"]
    assert all(secret not in failure["message"] for failure in payload["failures"])


@pytest.mark.parametrize(
    "identifier_env_var",
    [
        "TWILIO_ACCOUNT_SID",
        "TURN_USERNAME",
        "AWS_ACCESS_KEY_ID",
        "WEBRTC_EXPOSE_ICE_CREDENTIALS",
    ],
)
def test_runtime_secret_collection_excludes_harmless_identifiers(
    monkeypatch: pytest.MonkeyPatch,
    identifier_env_var: str,
) -> None:
    identifier = f"visible-{identifier_env_var.lower()}"
    monkeypatch.setenv(identifier_env_var, identifier)

    assert identifier not in runtime_secret_values()


def test_validation_runner_ignores_whitespace_only_runtime_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EASYCAT_WS_TOKEN", " ")

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        junit_arg = next(arg for arg in command if arg.startswith("--junitxml="))
        Path(junit_arg.removeprefix("--junitxml=")).write_text('<testsuite name="plain output" />')
        return CommandResult(
            exit_code=0,
            stdout="plain output remains readable",
            stderr="plain stderr remains readable",
        )

    result = run_validation_slice(
        "quick",
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC),
    )

    assert " " not in runtime_secret_values()
    assert (result.run_dir / "stdout.log").read_text() == "plain output remains readable"
    assert (result.run_dir / "stderr.log").read_text() == "plain stderr remains readable"
    assert (result.run_dir / "junit.xml").read_text() == '<testsuite name="plain output" />'


def test_validation_runner_creates_isolated_run_directories(tmp_path: Path) -> None:
    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        return CommandResult(exit_code=0, stdout="", stderr="")

    first = run_validation_slice(
        "quick",
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC),
    )
    second = run_validation_slice(
        "quick",
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC),
    )

    assert first.run_dir != second.run_dir
    assert first.run_dir.exists()
    assert second.run_dir.exists()


def test_release_validation_builds_installed_wheel_and_aggregates_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + ("c" * 32))
    commands: list[list[str]] = []
    command_envs: list[dict[str, str]] = []
    command_cwds: list[Path | None] = []

    def fake_command_runner(
        command: list[str],
        *,
        env: dict[str, str],
        cwd: Path | None = None,
    ) -> CommandResult:
        commands.append(command)
        command_envs.append(dict(env))
        command_cwds.append(cwd)
        if command[:4] == ["uv", "build", "--sdist", "--wheel"]:
            out_dir = Path(command[-1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "easycat-0.1.0-py3-none-any.whl").write_text("wheel")
            (out_dir / "easycat-0.1.0.tar.gz").write_text("sdist")
        for arg in command:
            if arg.startswith("--junitxml="):
                Path(arg.removeprefix("--junitxml=")).write_text("<testsuite />")
        samples_path = env.get("EASYCAT_LATENCY_SAMPLES_PATH")
        if samples_path:
            Path(samples_path).write_text(
                json.dumps(
                    [
                        LatencySample(
                            sample_id="release-latency-1",
                            condition_id="release",
                            warmup=False,
                            timestamp_source="time.monotonic",
                            stages=LatencyStageDurations(total_ms=1000.0),
                        ).to_dict()
                    ]
                )
            )
        return CommandResult(exit_code=0, stdout="ok", stderr="")

    result = run_release_validation(
        artifacts_dir=tmp_path,
        python_version="3.12",
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
    )

    payload = json.loads(result.report_path.read_text())
    check_names = {check["name"] for check in payload["checks"]}
    assert result.exit_code == 0
    assert payload["status"] == "pass"
    assert {
        "release.build",
        "release.metadata",
        "release.venv",
        "release.install",
        "release.import-smoke",
        "release.public-api-smoke",
        "release.help-smoke",
        "release.module-smoke",
        "release.init-smoke",
        "release.doctor",
        "release.cli-smoke",
        "release.quick",
        "release.guard",
        "release.stress",
        "release.contracts",
        "release.live",
        "release.latency.sweep",
    } <= check_names
    assert payload["artifacts"]["quick_report"]["kind"] == "validation_report"
    assert payload["artifacts"]["latency_sweep_report"]["kind"] == "validation_report"
    assert (tmp_path / "latest.json").read_text() == result.report_path.read_text()
    build_command = next(command for command in commands if command[:2] == ["uv", "build"])
    assert "--no-sources" in build_command
    assert any(
        command[:4]
        == [
            "uvx",
            "twine",
            "check",
            str(tmp_path / "runs" / result.run.run_id / "dist" / "easycat-0.1.0-py3-none-any.whl"),
        ]
        for command in commands
    )
    assert any(command[:2] == ["uv", "venv"] and "--python" in command for command in commands)
    assert any(command[-1] == "--help" for command in commands)
    assert any(command[-2:] == ["-m", "easycat"] for command in commands)
    assert any(
        "init" in command and "--no-git" in command and "--json" in command for command in commands
    )
    assert any("doctor" in command for command in commands)
    assert any(
        "-m" in command
        and any(
            "not integration_socket and not integration_live and not integration_external" in arg
            and "not contract" in arg
            for arg in command
        )
        for command in commands
    )
    assert any(env.get("PYTHONPATH") == "" for env in command_envs)
    external_cwds = [
        cwd for cwd in command_cwds if cwd is not None and not cwd.is_relative_to(Path.cwd())
    ]
    assert external_cwds
    assert all(not cwd.exists() for cwd in external_cwds)


def test_release_validation_fails_when_child_slice_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + ("d" * 32))

    def fake_command_runner(
        command: list[str],
        *,
        env: dict[str, str],
        cwd: Path | None = None,
    ) -> CommandResult:
        if command[:4] == ["uv", "build", "--sdist", "--wheel"]:
            out_dir = Path(command[-1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "easycat-0.1.0-py3-none-any.whl").write_text("wheel")
            (out_dir / "easycat-0.1.0.tar.gz").write_text("sdist")
        for arg in command:
            if arg.startswith("--junitxml="):
                Path(arg.removeprefix("--junitxml=")).write_text("<testsuite />")
        samples_path = env.get("EASYCAT_LATENCY_SAMPLES_PATH")
        if samples_path:
            Path(samples_path).write_text(
                json.dumps(
                    [
                        LatencySample(
                            sample_id="release-latency-1",
                            condition_id="release",
                            warmup=False,
                            timestamp_source="time.monotonic",
                            stages=LatencyStageDurations(total_ms=1000.0),
                        ).to_dict()
                    ]
                )
            )
        quick_selector = (
            "not integration_socket and not integration_live and not integration_external "
            "and not contract and not slow and not stress and not serial and not flaky "
            "and not guard"
        )
        if quick_selector in command:
            return CommandResult(exit_code=1, stdout="", stderr="quick failed")
        return CommandResult(exit_code=0, stdout="", stderr="")

    result = run_release_validation(
        artifacts_dir=tmp_path,
        python_version="3.12",
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
    )

    payload = json.loads(result.report_path.read_text())
    assert result.exit_code == 1
    assert payload["status"] == "fail"
    assert payload["tool_exit_codes"]["release.quick"] == 1
    assert payload["failures"][-1]["name"] == "release.quick"


def test_release_validation_stops_after_wheel_install_failure(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def fake_command_runner(
        command: list[str],
        *,
        env: dict[str, str],
        cwd: Path | None = None,
    ) -> CommandResult:
        commands.append(command)
        if command[:4] == ["uv", "build", "--sdist", "--wheel"]:
            out_dir = Path(command[-1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "easycat-0.1.0-py3-none-any.whl").write_text("wheel")
            (out_dir / "easycat-0.1.0.tar.gz").write_text("sdist")
        if command[:3] == ["uv", "pip", "install"] and any(
            arg.startswith("easycat[") for arg in command
        ):
            return CommandResult(exit_code=1, stderr="wheel install failed")
        return CommandResult(exit_code=0)

    result = run_release_validation(
        artifacts_dir=tmp_path,
        python_version="3.12",
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
    )

    payload = json.loads(result.report_path.read_text())
    assert result.exit_code == 1
    assert payload["tool_exit_codes"]["release.install"] == 1
    assert "release.install-test-tools" not in payload["tool_exit_codes"]
    assert not any("pytest" in command for command in commands)
    assert not any(command[-1:] == ["--help"] for command in commands)


def test_release_validation_stops_after_metadata_failure(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def fake_command_runner(
        command: list[str],
        *,
        env: dict[str, str],
        cwd: Path | None = None,
    ) -> CommandResult:
        commands.append(command)
        if command[:4] == ["uv", "build", "--sdist", "--wheel"]:
            out_dir = Path(command[-1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "easycat-0.1.0-py3-none-any.whl").write_text("wheel")
            (out_dir / "easycat-0.1.0.tar.gz").write_text("sdist")
        if command[:3] == ["uvx", "twine", "check"]:
            return CommandResult(exit_code=1, stderr="invalid package metadata")
        return CommandResult(exit_code=0)

    result = run_release_validation(
        artifacts_dir=tmp_path,
        python_version="3.12",
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
    )

    payload = json.loads(result.report_path.read_text())
    assert result.exit_code == 1
    assert payload["tool_exit_codes"]["release.metadata"] == 1
    assert "release.venv" not in payload["tool_exit_codes"]
    assert not any(command[:2] == ["uv", "venv"] for command in commands)


def test_release_validation_requires_wheel_and_sdist(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def fake_command_runner(
        command: list[str],
        *,
        env: dict[str, str],
        cwd: Path | None = None,
    ) -> CommandResult:
        commands.append(command)
        if command[:4] == ["uv", "build", "--sdist", "--wheel"]:
            out_dir = Path(command[-1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "easycat-0.1.0-py3-none-any.whl").write_text("wheel")
        return CommandResult(exit_code=0)

    result = run_release_validation(
        artifacts_dir=tmp_path,
        python_version="3.12",
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
    )

    payload = json.loads(result.report_path.read_text())
    assert result.exit_code == 1
    assert any(failure["name"] == "release.sdist" for failure in payload["failures"])
    assert "release.venv" not in payload["tool_exit_codes"]
    assert not any(command[:2] == ["uv", "venv"] for command in commands)


def test_validation_main_dispatches_socket_slice(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        commands.append(command)
        return CommandResult(exit_code=0, stdout="", stderr="")

    exit_code = main(
        ["socket", "--artifacts-dir", str(tmp_path)],
        command_runner=fake_command_runner,
    )

    assert exit_code == 0
    assert commands[0][-2:] == ["-m", "integration_socket and not integration_live and not flaky"]


def test_validation_main_dispatches_guard_slice(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        commands.append(command)
        return CommandResult(exit_code=0, stdout="", stderr="")

    exit_code = main(
        ["guard", "--artifacts-dir", str(tmp_path)],
        command_runner=fake_command_runner,
    )

    assert exit_code == 0
    assert commands[0][-2:] == [
        "-m",
        "guard and not integration_live and not integration_external and not flaky",
    ]


def test_validation_main_dispatches_stress_slice(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        commands.append(command)
        return CommandResult(exit_code=0, stdout="", stderr="")

    exit_code = main(
        ["stress", "--artifacts-dir", str(tmp_path)],
        command_runner=fake_command_runner,
    )

    assert exit_code == 0
    assert commands[0][-2:] == ["-m", "stress and not integration_live and not flaky"]


def test_validation_main_dispatches_contracts_slice(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        commands.append(command)
        return CommandResult(exit_code=0, stdout="", stderr="")

    exit_code = main(
        ["contracts", "--artifacts-dir", str(tmp_path)],
        command_runner=fake_command_runner,
    )

    assert exit_code == 0
    assert commands[0][-2:] == ["-m", "contract and not integration_live and not flaky"]


def test_validation_runner_can_use_installed_wheel_pytest_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setenv("EASYCAT_VALIDATION_PYTEST_COMMAND", "/tmp/venv/bin/python -m pytest")
    monkeypatch.setenv("EASYCAT_VALIDATION_TEST_PATHS", f"/repo/tests{os.pathsep}/repo/smoke")

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        commands.append(command)
        return CommandResult(exit_code=0, stdout="", stderr="")

    run_validation_slice(
        "quick",
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    assert commands[0][:10] == [
        "/tmp/venv/bin/python",
        "-m",
        "pytest",
        "-q",
        "-n",
        "auto",
        "--dist",
        "load",
        "/repo/tests",
        "/repo/smoke",
    ]
