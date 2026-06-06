from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github/workflows/ci.yml"
NIGHTLY_WORKFLOW = REPO_ROOT / ".github/workflows/nightly-validation.yml"
RELEASE_WORKFLOW = REPO_ROOT / ".github/workflows/release-validation.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _advertised_python_versions() -> list[str]:
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    classifiers = pyproject["project"]["classifiers"]
    versions = []
    for classifier in classifiers:
        match = re.fullmatch(r"Programming Language :: Python :: (3\.\d+)", classifier)
        if match is not None:
            versions.append(match.group(1))
    return sorted(versions, key=lambda version: tuple(map(int, version.split("."))))


def _workflow_text() -> str:
    return WORKFLOW.read_text()


def _validation_tasks_section(heading: str, next_heading: str) -> str:
    plan = (REPO_ROOT / "plan/validation/tasks.md").read_text(encoding="utf-8")
    return plan.split(heading, 1)[1].split(next_heading, 1)[0]


def test_quick_validation_ci_runs_declared_python_versions_without_fail_fast() -> None:
    text = _workflow_text()
    workflow = yaml.safe_load(text)
    matrix_versions = workflow["jobs"]["test"]["strategy"]["matrix"]["python-version"]

    assert matrix_versions == _advertised_python_versions()
    assert "fail-fast: false" in text
    assert "uv run --python ${{ matrix.python-version }} easycat validate quick" in text
    assert "pytest -x" not in text


def test_socket_validation_ci_runs_once_on_python_312() -> None:
    text = _workflow_text()
    socket_job = text.split("integration-socket:", maxsplit=1)[1].split(
        "integration-live:", maxsplit=1
    )[0]

    assert 'python-version: "3.12"' in socket_job
    assert "matrix:" not in socket_job
    assert "uv run --python 3.12 easycat validate socket" in socket_job


def test_socket_validation_ci_uploads_webrtc_stats_artifact_when_produced() -> None:
    text = _workflow_text()
    socket_job = text.split("integration-socket:", maxsplit=1)[1].split(
        "integration-live:", maxsplit=1
    )[0]

    assert "runs/**/webrtc/stats.jsonl" in socket_job


def test_validation_ci_uploads_reports_junit_and_logs_even_on_failure() -> None:
    text = _workflow_text()

    assert text.count("if: always()") >= 2
    assert "actions/upload-artifact@v4" in text
    assert "validation-report" in text
    assert "junit.xml" in text
    assert "stdout.log" in text
    assert "stderr.log" in text


def test_ci_has_package_build_smoke() -> None:
    text = _workflow_text()

    assert "build-smoke:" in text
    assert "uv build" in text
    assert 'python-version: "3.12"' in text


def test_validation_tasks_v12_current_state_tracks_ci_workflow() -> None:
    workflow = yaml.safe_load(_workflow_text())
    matrix_versions = workflow["jobs"]["test"]["strategy"]["matrix"]["python-version"]
    section = _validation_tasks_section(
        "### V1.2 Update CI Required Jobs",
        "### V1.3 Add Manual And Nightly Workflow Skeletons",
    )

    assert "Current verified state:" in section
    assert "`easycat validate quick`" in section
    assert "`easycat validate socket`" in section
    assert "`if: always()`" in section
    assert "`--junit-prefix`" in section
    assert "package build smoke" in section
    assert "workflow_dispatch" in section
    assert "CI no longer uses pytest `-x`" in section
    for version in matrix_versions:
        assert f"`{version}`" in section

    assert "CI does not upload validation JSON or JUnit artifacts" not in section
    assert "Current CI uses pytest `-x`" not in section
    assert "socket integration job also runs on Python 3.12 and 3.14" not in section
    assert '`-m "not integration_socket and not integration_live"`' not in section


def test_validation_tasks_v13_current_state_tracks_nightly_and_release_workflows() -> None:
    nightly = yaml.safe_load(NIGHTLY_WORKFLOW.read_text())
    release = yaml.safe_load(RELEASE_WORKFLOW.read_text())
    section = _validation_tasks_section(
        "### V1.3 Add Manual And Nightly Workflow Skeletons",
        "## V2: Structured Latency Validation",
    )

    assert "Current verified state:" in section
    for job_name in nightly["jobs"]:
        assert f"`{job_name}`" in section
    assert "`workflow_dispatch`" in section
    assert "protected, non-PR runs" in section
    assert "`live-validation` environment" in section
    assert "`easycat validate latency --require-samples`" in section
    assert "`OPENAI_API_KEY` only on the validation step" in section
    assert "`if: always()`" in section
    assert "`release-validation` environment" in section
    assert "clean temporary venv outside the workspace" in section
    assert "strict live validation" in section
    assert "latency sweep with `--require-samples`" in section
    assert "unexpected skips" in section
    assert "bounded retention" in section

    assert "latency-placeholder" not in nightly["jobs"]
    assert release["jobs"]["release-validation"]["environment"] == "release-validation"
    assert "placeholder live/latency jobs" not in section


def test_nightly_validation_workflow_skeleton_exists() -> None:
    text = NIGHTLY_WORKFLOW.read_text()

    assert "schedule:" in text
    assert "workflow_dispatch:" in text
    assert "pull_request:" not in text
    assert "full-local:" in text
    assert "not integration_socket and not integration_live and not stress and not flaky" in text
    assert "validate quick" in text
    assert "validate socket" in text
    assert "validate stress" in text
    assert "flaky-quarantine:" in text
    assert "latency:" in text
    assert "live-canaries:" in text
    assert "actions/upload-artifact@v4" in text
    assert "if: always()" in text
    assert "retention-days:" in text


def test_release_validation_workflow_skeleton_exists() -> None:
    text = RELEASE_WORKFLOW.read_text()

    assert "workflow_dispatch:" in text
    assert "uv build --sdist --wheel" in text
    # RELEASE_VENV is exported via $GITHUB_ENV (not a job-level ``env:``)
    # because ``runner.temp`` is not resolvable when job env is evaluated —
    # see the workflow's "Configure release venv path" step.
    assert 'echo "RELEASE_VENV=$RUNNER_TEMP/easycat-release-venv" >> "$GITHUB_ENV"' in text
    assert 'uv venv "$RELEASE_VENV" --python 3.12' in text
    assert '"easycat[openai,openai-agents] @ file://$WHEEL_PATH"' in text
    assert 'PYTHONPATH: ""' in text
    assert "working-directory: ${{ runner.temp }}" in text
    assert "EASYCAT_VALIDATION_PYTEST_COMMAND" in text
    assert "EASYCAT_VALIDATION_TEST_PATHS: ${{ github.workspace }}/tests" in text
    assert "EASYCAT_VALIDATION_TEST_ROOT: ${{ github.workspace }}/tests" in text
    assert "site-packages" in text
    assert "not package_path.is_relative_to(workspace)" in text
    assert "tests/cli/test_app.py" in text
    assert '"$RELEASE_VENV/bin/easycat" doctor --json' in text
    assert '"$RELEASE_VENV/bin/easycat" validate quick' in text
    assert '"$RELEASE_VENV/bin/easycat" validate stress' in text
    assert '"$RELEASE_VENV/bin/easycat" validate live --release' in text
    assert '"$RELEASE_VENV/bin/easycat" validate latency --sweep --require-samples' in text
    assert 'python" -m pytest "$GITHUB_WORKSPACE/tests" --collect-only -q -m flaky' in text
    assert "unexpected release validation skips" in text
    assert "environment: release-validation" in text
    assert "actions/upload-artifact@v4" in text
    assert "if: always()" in text
    assert "retention-days:" in text


def test_validation_tasks_v53_current_state_tracks_release_validation_workflow() -> None:
    workflow_text = RELEASE_WORKFLOW.read_text()
    workflow = yaml.safe_load(workflow_text)
    runner_source = (REPO_ROOT / "src/easycat/validation/runner.py").read_text(encoding="utf-8")
    cli_source = (REPO_ROOT / "src/easycat/cli/validate.py").read_text(encoding="utf-8")
    validate_tests_source = (REPO_ROOT / "tests/cli/test_validate.py").read_text(encoding="utf-8")
    section = _validation_tasks_section(
        "### V5.3 Add Release Validation Workflow",
        "## V6: Optional Observability API",
    )

    assert "Current verified state:" in section
    assert workflow["jobs"]["release-validation"]["environment"] == "release-validation"
    for token in (
        "workflow_dispatch:",
        "Mask live provider secrets",
        "::add-mask::",
        "uv build --sdist --wheel",
        'echo "RELEASE_VENV=$RUNNER_TEMP/easycat-release-venv" >> "$GITHUB_ENV"',
        'uv venv "$RELEASE_VENV" --python 3.12',
        '"easycat[openai,openai-agents] @ file://$WHEEL_PATH"',
        "pytest pytest-asyncio hypothesis",
        "working-directory: ${{ runner.temp }}",
        'PYTHONPATH: ""',
        "site-packages",
        "not package_path.is_relative_to(workspace)",
        '"$RELEASE_VENV/bin/easycat" doctor --json',
        '"$RELEASE_VENV/bin/easycat" validate quick',
        '"$RELEASE_VENV/bin/easycat" validate stress',
        '"$RELEASE_VENV/bin/easycat" validate live --release',
        '"$RELEASE_VENV/bin/easycat" validate latency --sweep --require-samples',
        'python" -m pytest "$GITHUB_WORKSPACE/tests" --collect-only -q -m flaky',
        "unexpected release validation skips",
        "actions/upload-artifact@v4",
        "if: always()",
        "retention-days: 30",
        "dist/**",
    ):
        assert token in workflow_text
    for token in (
        "def run_release_validation",
        "release.build",
        "release.venv",
        "release.install",
        "release.install-test-tools",
        "release.import-smoke",
        "release.doctor",
        "release.cli-smoke",
        'RELEASE_SLICES = ("quick", "stress", "contracts")',
        'f"release.{slice_name}"',
        "release.live",
        "release.latency.",
        "PYTHONPATH",
        "outside_dir",
        "EASYCAT_VALIDATION_PYTEST_COMMAND",
        "run_validation_slice",
        "run_live_validation",
        "run_latency_validation",
        "require_samples=True",
    ):
        assert token in runner_source
    for token in (
        "run_release_validation",
        "validate release",
        "--latency-smoke",
        "--latency-sweep",
        "--json",
    ):
        assert token in cli_source
    for test_name in (
        "test_release_validation_builds_installed_wheel_and_aggregates_reports",
        "test_release_validation_fails_when_child_slice_fails",
        "test_validate_release_cli_json_uses_standard_stdout_envelope",
        "test_validate_release_cli_rejects_conflicting_latency_modes",
        '"release.quick"',
        '"release.stress"',
        '"release.contracts"',
    ):
        assert test_name in validate_tests_source

    for token in (
        ".github/workflows/release-validation.yml",
        "workflow_dispatch",
        "release-validation",
        "uv build --sdist --wheel",
        "RELEASE_VENV",
        "$RUNNER_TEMP/easycat-release-venv",
        "easycat[openai,openai-agents]",
        "pytest",
        "pytest-asyncio",
        "hypothesis",
        "${{ runner.temp }}",
        'PYTHONPATH: ""',
        "easycat.__file__",
        "site-packages",
        "easycat doctor --json",
        "tests/cli/test_app.py",
        "validate quick",
        "validate stress",
        "-m flaky",
        "validate live --release --provider openai --surface stt --surface tts",
        "validate latency --sweep --require-samples",
        "OPENAI_API_KEY",
        "unexpected release validation skips",
        "VALIDATION_ARTIFACTS_DIR",
        "dist/**",
        "actions/upload-artifact@v4",
        "if: always()",
        "retention-days: 30",
        "run_release_validation(...)",
        "easycat validate release",
        "release.build",
        "release.venv",
        "release.install",
        "release.install-test-tools",
        "release.import-smoke",
        "release.doctor",
        "release.cli-smoke",
        "release.quick",
        "release.stress",
        "release.contracts",
        "release.live",
        "release.latency.<mode>",
        "EASYCAT_VALIDATION_PYTEST_COMMAND",
        "tests/test_ci_workflow.py",
        "tests/cli/test_validate.py",
    ):
        assert f"`{token}`" in section


def test_validation_workflows_parse_as_yaml() -> None:
    yaml.safe_load(WORKFLOW.read_text())
    yaml.safe_load(NIGHTLY_WORKFLOW.read_text())
    yaml.safe_load(RELEASE_WORKFLOW.read_text())


def test_nightly_validation_has_real_latency_job() -> None:
    data = yaml.safe_load(NIGHTLY_WORKFLOW.read_text())
    jobs = data["jobs"]

    assert "latency" in jobs, "expected a real `latency` job in nightly-validation.yml"
    assert "latency-placeholder" not in jobs, (
        "`latency-placeholder` job should be removed once the real `latency` job exists"
    )

    latency = jobs["latency"]
    steps = latency.get("steps", [])
    run_bodies = [step.get("run", "") for step in steps if isinstance(step, dict)]

    assert any(
        "easycat validate latency" in body
        and '--artifacts-dir "$VALIDATION_ARTIFACTS_DIR"' in body
        for body in run_bodies
    ), (
        "latency job must run `easycat validate latency` with "
        '`--artifacts-dir "$VALIDATION_ARTIFACTS_DIR"`'
    )

    # Latency must fail loudly when no samples are produced (e.g. missing
    # OPENAI_API_KEY causes the smoke probe to skip). Without --require-samples
    # an empty run is indistinguishable from a passing run.
    for body in run_bodies:
        if "easycat validate latency" in body:
            assert "--release" not in body, "nightly latency must not use --release"
            assert "--require-samples" in body, (
                "nightly latency must pass --require-samples so a skipped/empty "
                "smoke run fails loudly instead of going green silently"
            )

    # Live-credential gating mirrors live-canaries: protected branches only and
    # the live-validation environment. Scope OPENAI_API_KEY only to the step
    # that needs it so checkout/setup/install/upload steps cannot read it.
    assert (
        latency.get("if") == "github.event_name != 'pull_request' && github.ref_protected == true"
    ), "latency job must be gated to protected, non-PR runs (same as live-canaries)"
    assert latency.get("environment") == "live-validation", (
        "latency job must run in the live-validation GitHub environment "
        "so OPENAI_API_KEY is gated and audited"
    )

    env = latency.get("env", {})
    assert "VALIDATION_ARTIFACTS_DIR" in env, (
        "latency job must define VALIDATION_ARTIFACTS_DIR env like other nightly jobs"
    )
    expected_env = jobs["quick"]["env"]["VALIDATION_ARTIFACTS_DIR"]
    assert env["VALIDATION_ARTIFACTS_DIR"] == expected_env, (
        "VALIDATION_ARTIFACTS_DIR must mirror the shape used by quick/socket/stress jobs"
    )
    assert "OPENAI_API_KEY" not in env, (
        "latency job must not expose OPENAI_API_KEY at job scope; only the "
        "validation step should receive the live provider secret"
    )

    validation_steps = [
        step
        for step in steps
        if isinstance(step, dict) and "easycat validate latency" in str(step.get("run", ""))
    ]
    assert len(validation_steps) == 1, "latency job must have one validation command step"
    validation_step = validation_steps[0]
    assert (
        validation_step.get("env", {}).get("OPENAI_API_KEY") == "${{ secrets.OPENAI_API_KEY }}"
    ), (
        "latency validation step must receive OPENAI_API_KEY from secrets; otherwise "
        "the smoke probe skips and the job is a silent no-op"
    )
    assert "add-mask" in str(validation_step.get("run", "")), (
        "latency validation step must mask OPENAI_API_KEY before invoking the live probe"
    )

    non_validation_steps_with_openai_key = [
        step
        for step in steps
        if isinstance(step, dict)
        and step is not validation_step
        and "OPENAI_API_KEY" in step.get("env", {})
    ]
    assert not non_validation_steps_with_openai_key, (
        "OPENAI_API_KEY must not be available to checkout/setup/install/upload steps"
    )

    assert isinstance(latency.get("timeout-minutes"), int), (
        "latency job must declare a timeout-minutes so a hung live probe "
        "cannot consume the full workflow budget"
    )

    upload_steps = [
        step
        for step in steps
        if isinstance(step, dict) and step.get("uses", "").startswith("actions/upload-artifact@")
    ]
    assert upload_steps, "latency job must upload artifacts like other nightly jobs"
    upload = upload_steps[0]
    assert upload.get("if") == "always()", "upload step must run with if: always()"
    assert upload.get("uses") == "actions/upload-artifact@v4"
    with_block = upload.get("with", {})
    assert with_block.get("path") == "${{ env.VALIDATION_ARTIFACTS_DIR }}"
    assert "retention-days" in with_block
    artifact_name = with_block.get("name", "")
    assert artifact_name.startswith("nightly-validation-report-latency-"), (
        f"latency upload artifact name {artifact_name!r} must start with "
        "'nightly-validation-report-latency-' so it does not collide with "
        "the quick/socket/stress artifact slots"
    )


def test_nightly_validation_has_no_placeholder_jobs() -> None:
    data = yaml.safe_load(NIGHTLY_WORKFLOW.read_text())
    jobs = data["jobs"]

    offenders: list[tuple[str, str]] = []
    for job_name, job in jobs.items():
        for step in job.get("steps", []) or []:
            if not isinstance(step, dict):
                continue
            run_body = step.get("run", "")
            if not isinstance(run_body, str):
                continue
            lowered = run_body.lower()
            if "placeholder" in lowered or "until v2 lands" in lowered:
                offenders.append((job_name, run_body))

    assert not offenders, (
        f"nightly-validation.yml still contains placeholder run bodies: {offenders}"
    )


def test_live_canary_workflows_are_guarded_and_redacted() -> None:
    nightly = NIGHTLY_WORKFLOW.read_text()
    release = RELEASE_WORKFLOW.read_text()

    assert "pull_request:" not in nightly
    assert "github.event_name != 'pull_request' && github.ref_protected == true" in nightly
    assert "environment: live-validation" in nightly
    assert "OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}" in nightly
    assert "DEEPGRAM_API_KEY: ${{ secrets.DEEPGRAM_API_KEY }}" in nightly
    assert "ELEVENLABS_API_KEY: ${{ secrets.ELEVENLABS_API_KEY }}" in nightly
    assert "CARTESIA_API_KEY: ${{ secrets.CARTESIA_API_KEY }}" in nightly
    assert "::add-mask::" in nightly
    assert "easycat validate live --provider openai --surface stt --surface tts" in nightly
    assert "Upload redacted live validation artifacts" in nightly

    assert "environment: release-validation" in release
    assert "OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}" in release
    assert "::add-mask::" in release
    release_live_command = (
        '"$RELEASE_VENV/bin/easycat" validate live --release --provider openai '
        "--surface stt --surface tts"
    )
    assert release_live_command in release


def test_validation_tasks_v43_current_state_tracks_live_canary_ci() -> None:
    nightly = yaml.safe_load(NIGHTLY_WORKFLOW.read_text())
    release = yaml.safe_load(RELEASE_WORKFLOW.read_text())
    nightly_text = NIGHTLY_WORKFLOW.read_text()
    release_text = RELEASE_WORKFLOW.read_text()
    section = _validation_tasks_section(
        "### V4.3 Harden Live Canary CI",
        "## V5: Stress, Benchmarks, And Release Gates",
    )
    normalized_section = " ".join(section.split())

    assert "pull_request" not in (nightly.get(True) or {})
    live_canaries = nightly["jobs"]["live-canaries"]
    latency = nightly["jobs"]["latency"]
    assert live_canaries["if"] == (
        "github.event_name != 'pull_request' && github.ref_protected == true"
    )
    assert latency["if"] == "github.event_name != 'pull_request' && github.ref_protected == true"
    assert live_canaries["environment"] == "live-validation"
    assert latency["environment"] == "live-validation"
    for env_var in (
        "OPENAI_API_KEY",
        "DEEPGRAM_API_KEY",
        "ELEVENLABS_API_KEY",
        "CARTESIA_API_KEY",
    ):
        assert live_canaries["env"][env_var] == f"${{{{ secrets.{env_var} }}}}"
        assert f"{env_var}: ${{{{ secrets.{env_var} }}}}" in release_text
    assert "Mask live provider secrets" in nightly_text
    assert "Mask live provider secrets" in release_text
    assert "::add-mask::" in nightly_text
    assert "::add-mask::" in release_text
    assert "easycat validate live --provider openai --surface stt --surface tts" in nightly_text
    assert "--release --provider openai --surface stt --surface tts" in release_text
    assert "easycat validate latency --require-samples" in nightly_text
    assert "actions/upload-artifact@v4" in nightly_text
    assert "actions/upload-artifact@v4" in release_text
    assert "retention-days: 14" in nightly_text
    assert "retention-days: 30" in release_text
    assert release["jobs"]["release-validation"]["environment"] == "release-validation"
    assert "unexpected release validation skips" in release_text
    assert "placeholder live/latency jobs" not in normalized_section

    assert "Current verified state:" in section
    for token in (
        ".github/workflows/nightly-validation.yml",
        "pull_request",
        "live-canaries",
        "latency",
        "github.event_name != 'pull_request' && github.ref_protected == true",
        "live-validation",
        "OPENAI_API_KEY",
        "DEEPGRAM_API_KEY",
        "ELEVENLABS_API_KEY",
        "CARTESIA_API_KEY",
        "::add-mask::",
        "easycat validate live --provider openai --surface stt --surface tts",
        "--strict",
        "--release",
        "easycat validate latency --require-samples",
        ".github/workflows/release-validation.yml",
        "workflow_dispatch",
        "release-validation",
        (
            '"$RELEASE_VENV/bin/easycat" validate live --release --provider openai '
            "--surface stt --surface tts"
        ),
        "VALIDATION_ARTIFACTS_DIR",
        "dist/**",
        "tests/test_ci_workflow.py",
        "actions/upload-artifact@v4",
    ):
        assert f"`{token}`" in section
    for phrase in (
        "no `pull_request` trigger",
        "GitHub secrets at job scope",
        "without `--strict` or `--release`",
        "non-strict",
        "expected provider-check skips",
        "redacted provider capability reports",
        "scopes `OPENAI_API_KEY` only to the latency validation step",
        "manual `workflow_dispatch`",
        "explicitly from GitHub secrets",
        "installed-package execution outside the workspace",
        "unexpected skips",
        "bounded retention",
        "absence of placeholder jobs",
    ):
        assert phrase in normalized_section
