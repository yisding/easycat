"""Downstream static contracts for VoiceApp keyword ergonomics."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_MYPY_TIMEOUT_S = 90


@pytest.mark.timeout(_MYPY_TIMEOUT_S + 10)
def test_voice_app_keywords_are_mode_specific_for_type_checkers(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid_voice_app.py"
    invalid.write_text(
        """\
from easycat import EasyConfig, VoiceApp

config = EasyConfig.mic()
VoiceApp(agnet=object())
VoiceApp(host=8080)
VoiceApp(config=config, tts="openai")
app = VoiceApp()
app.run("browser", stream_url="wss://example.test/media")
app.run("websocket", announce=False)
app.run("twilio", serve_token="secret")
app.run("local", port=9000)
app.run("nonsense")
app.session("browser")

async def wrong_async_mode_option() -> None:
    await app.serve("websocket", announce=False)
""",
        encoding="utf-8",
    )
    consumer = Path("tests/typecheck/voice_app_consumer.py")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--no-incremental",
            "--show-error-codes",
            str(consumer),
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
    assert reported_lines == {4, 5, 6, 8, 9, 10, 11, 12, 13, 16}, output
    assert 'Unexpected keyword argument "agnet"' in output
    assert 'No overload variant of "run"' in output
    assert 'No overload variant of "serve"' in output
    assert 'Argument 1 to "session"' in output
