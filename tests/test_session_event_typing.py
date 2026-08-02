"""Downstream static contract for Session event callback inference."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_MYPY_TIMEOUT_S = 90


@pytest.mark.timeout(_MYPY_TIMEOUT_S + 10)
def test_session_event_type_is_linked_to_callback_parameter(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid_session_events.py"
    invalid.write_text(
        """\
from easycat import AgentFinal, STTFinal, Session

def wrong_event(event: AgentFinal) -> None:
    pass

def register(session: Session) -> None:
    session.subscribe_event(STTFinal, wrong_event)
    session.subscribe_event(STTFinal, lambda event: print(event.does_not_exist))
""",
        encoding="utf-8",
    )
    valid = Path("tests/typecheck/session_event_consumer.py")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--no-incremental",
            "--show-error-codes",
            str(valid),
            str(invalid),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=_MYPY_TIMEOUT_S,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    diagnostics = [line for line in output.splitlines() if ": error:" in line]
    unexpected = [line for line in diagnostics if not line.startswith(str(invalid))]
    assert unexpected == [], output
    reported_lines = {
        int(line.removeprefix(f"{invalid}:").split(":", 1)[0]) for line in diagnostics
    }
    assert reported_lines == {7, 8}, output
    assert 'incompatible type "Callable[[AgentFinal], None]"' in output
    assert 'expected "Callable[[STTFinal]' in output
    assert '"STTFinal" has no attribute "does_not_exist"' in output
