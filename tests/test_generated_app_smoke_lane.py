"""Pin the CI lane that proves a generated project from the built wheel.

``ci.yml``'s ``generated-app-smoke`` job is the *only* continuously running
lane in this repo where a scaffolded project's SDK-bound tests actually
execute: every other lane syncs ``--group dev`` only, so the generated
``importorskip("agents")`` half silently skips, and it imports ``easycat``
from ``src/`` rather than from an installed wheel. The acceptance sentences
"import never starts audio or a server", "a generated project runs offline
tests outside this checkout" and "breaking a tested behavior fails its test"
all name that job as their proof.

Nothing else in the suite reads it, so without this module the job could be
weakened — or deleted — with every test still green. This is the same
workflow-text pinning ``tests/test_dependency_policy.py`` applies to the
``minimum-dependencies`` and ``langchain-versions`` jobs.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.cli.e2e.test_scaffold_smoke import _NETGUARD_MARKER

REPO_ROOT = Path(__file__).resolve().parents[1]

_JOB_KEY = re.compile(r"^  [A-Za-z0-9_-]+:$", re.MULTILINE)


def _workflow(name: str) -> str:
    return (REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def _job(workflow: str, job_id: str) -> str:
    """Return one job's block, from its key to the next top-level job key."""
    start = workflow.index(f"\n  {job_id}:\n") + 1
    following = _JOB_KEY.search(workflow, start + len(job_id) + 4)
    return workflow[start : following.start()] if following else workflow[start:]


def test_generated_app_smoke_runs_on_every_push_and_pull_request() -> None:
    """The job is worthless as a gate if it only fires on demand.

    ``release-validation.yml`` already covers the release-time case; this job
    exists precisely because that workflow is ``workflow_dispatch`` /
    ``workflow_call`` only and therefore never sees a PR.
    """
    workflow = _workflow("ci.yml")
    triggers = workflow[: workflow.index("\n  lint:")]

    assert "  push:\n    branches: [main]" in triggers
    assert "  pull_request:\n    branches: [main]" in triggers
    assert "\n  generated-app-smoke:\n" in workflow

    job = _job(workflow, "generated-app-smoke")
    assert "needs:" not in job, "the job must stay off the critical path"


def test_generated_app_smoke_installs_the_wheel_outside_the_workspace() -> None:
    """A2's "outside this checkout" half: a wheel, an extra, a throwaway venv.

    Installing from ``src/`` or into the repo's own environment would make
    the whole lane a duplicate of ``coverage``.
    """
    job = _job(_workflow("ci.yml"), "generated-app-smoke")

    assert "uv build --wheel" in job
    assert 'uv venv "$RUNNER_TEMP/easycat-app-venv"' in job
    assert '"easycat[openai-agents] @ file://$WHEEL_PATH" pytest' in job
    # The generated project is scaffolded and tested outside $GITHUB_WORKSPACE,
    # and no step may put the source tree back on sys.path.
    assert "$RUNNER_TEMP/easycat-app-smoke" in job
    assert '! grep -q "tool.uv.sources"' in job
    assert 'timeout 30 "$APP_VENV/bin/python" -c "import agent, tools"' in job


def test_generated_app_smoke_canaries_the_network_guard_before_trusting_it() -> None:
    """A guard that never loaded must fail the job, not be assumed.

    Asserting only that the connection failed cannot tell an active guard
    from an egress-restricted runner, so the marker grep is load-bearing.
    """
    job = _job(_workflow("ci.yml"), "generated-app-smoke")

    assert 'cp "$GITHUB_WORKSPACE/tests/_netguard/sitecustomize.py"' in job
    assert "2>guard.err" in job
    assert f'grep -q "{_NETGUARD_MARKER}" guard.err' in job
    # The guard reaches the pytest run through PYTHONPATH, per step (never
    # job-level: it would break the `uv pip install` that needs PyPI).
    assert "PYTHONPATH: ${{ runner.temp }}/netguard" in job
    assert "OPENAI_API_KEY: sk-ambient-not-used" in job


def test_generated_app_smoke_fails_when_the_generated_suite_fails() -> None:
    """`set -o pipefail` is the whole assertion.

    GitHub's default step shell is ``bash -e {0}`` -- errexit without
    pipefail -- so ``pytest ... | tee run.log`` would otherwise report
    ``tee``'s exit status and a failing generated suite would leave this job
    green, which is the exact regression the lane exists to catch.
    """
    job = _job(_workflow("ci.yml"), "generated-app-smoke")
    run_step = job[job.index("Generated tests pass with an ambient credential") :]

    assert "set -o pipefail" in run_step
    assert run_step.index("set -o pipefail") < run_step.index("| tee run.log")
    assert '"$APP_VENV/bin/pytest" tests -q -rs -p no:cacheprovider -p no:randomly' in run_step
    # Negative half: nothing may skip once the SDK is installed. `grep -qv`
    # would succeed on any line lacking the word and can never fail.
    assert 'if grep -q "skipped" run.log; then' in run_step
    assert "grep -qv" not in run_step
    # Positive half: `-rs` prints "file:line: reason" and never a test name,
    # so the exact nodeid is re-run -- a skip reports "1 skipped".
    assert "tests/test_agent.py::test_agent_wires_its_instructions_and_tools" in run_step
    assert 'grep -q "1 passed" wiring.log' in run_step


def test_generated_app_smoke_executes_the_seeded_break() -> None:
    """A3, executed rather than asserted: a broken tool must fail the suite."""
    job = _job(_workflow("ci.yml"), "generated-app-smoke")
    break_step = job[job.index("A broken tool must fail the generated tests") :]

    seed_guard = 'assert \'"%H:%M"\' in text, "seeded-break target moved; update this step"'
    assert seed_guard in break_step
    assert 'if "$APP_VENV/bin/pytest" tests -q -p no:cacheprovider; then' in break_step
    assert "generated tests did not detect a broken app" in break_step


def test_release_validation_generated_app_smoke_greps_the_guard_marker() -> None:
    """The release-time peer must canary the guard the same way CI does."""
    workflow = _workflow("release-validation.yml")
    step = workflow[workflow.index("DX3 - generated app smoke") :]
    step = step[: step.index("\n      - name: Run installed wheel quick validation")]

    assert f'grep -q "{_NETGUARD_MARKER}" "$RUNNER_TEMP/guard.err"' in step
    assert '2>"$RUNNER_TEMP/guard.err"' in step
    assert 'OPENAI_API_KEY="sk-ambient-not-used"' in step
    assert '"seeded-break target moved; update this step"' in step
