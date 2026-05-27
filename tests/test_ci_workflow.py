from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/ci.yml")
NIGHTLY_WORKFLOW = Path(".github/workflows/nightly-validation.yml")
RELEASE_WORKFLOW = Path(".github/workflows/release-validation.yml")


def _workflow_text() -> str:
    return WORKFLOW.read_text()


def test_quick_validation_ci_runs_declared_python_versions_without_fail_fast() -> None:
    text = _workflow_text()

    assert 'python-version: ["3.11", "3.12", "3.14"]' in text
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
    assert "RELEASE_VENV: ${{ runner.temp }}/easycat-release-venv" in text
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

    # Live-credential gating mirrors live-canaries: protected branches only,
    # OPENAI_API_KEY plumbed through the live-validation environment.
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
    assert env.get("OPENAI_API_KEY") == "${{ secrets.OPENAI_API_KEY }}", (
        "latency job must export OPENAI_API_KEY from secrets; otherwise the "
        "smoke probe skips and the job is a silent no-op"
    )

    mask_steps = [
        step for step in steps if isinstance(step, dict) and "add-mask" in str(step.get("run", ""))
    ]
    assert mask_steps, "latency job must mask OPENAI_API_KEY in logs"

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
