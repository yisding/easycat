"""Cold downstream type contracts, checked together in one mypy process."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from easycat.cli.scaffold._schema import InitConfig
from easycat.cli.scaffold.init import _render_text, _substitutions

_MYPY_TIMEOUT_S = 90


def _write_invalid_consumers(tmp_path: Path) -> dict[Path, set[int]]:
    consumers = {
        tmp_path / "invalid_session_events.py": (
            """\
from easycat import AgentFinal, STTFinal, Session

def wrong_event(event: AgentFinal) -> None:
    pass

def register(session: Session) -> None:
    session.subscribe_event(STTFinal, wrong_event)
    session.subscribe_event(STTFinal, lambda event: print(event.does_not_exist))
""",
            {7, 8},
        ),
        tmp_path / "invalid_voice_app.py": (
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
            {4, 5, 6, 8, 9, 10, 11, 12, 13, 16},
        ),
        tmp_path / "invalid_easyconfig_presets.py": (
            """\
from easycat import EasyConfig

EasyConfig.mic(agnet=object())
EasyConfig.browser(debug="verbose")
EasyConfig.phone(handler_error_policy="ignore")
EasyConfig.mic(journal_capacity="10000")
EasyConfig.browser(journal_backend="memory")
EasyConfig.phone(enable_echo_cancellation="yes")
EasyConfig.mic(smart_turn_sensitivity="high")
EasyConfig.browser(turn_taking="vad")
EasyConfig.phone(caller_id_exposure="public")
EasyConfig.mic(record_to=123)
EasyConfig.browser(transport=123)
EasyConfig.phone(mcp_servers="stdio://tool")
""",
            set(range(3, 15)),
        ),
    }
    for path, (source, _) in consumers.items():
        path.write_text(source, encoding="utf-8")
    return {path: lines for path, (_, lines) in consumers.items()}


def _render_twilio_server(tmp_path: Path) -> Path:
    template = Path("src/easycat/cli/scaffold/templates/twilio-phone/server.py")
    mapping = _substitutions(InitConfig(template="twilio-phone"), project_name="demo")
    rendered = tmp_path / "generated_twilio_server.py"
    rendered.write_text(
        _render_text(template.read_text(encoding="utf-8"), mapping),
        encoding="utf-8",
    )
    return rendered


@pytest.mark.slow
@pytest.mark.timeout(_MYPY_TIMEOUT_S + 10)
def test_downstream_consumer_type_contracts(tmp_path: Path) -> None:
    """Prove every valid and invalid consumer shape with one cold mypy graph."""
    invalid_consumers = _write_invalid_consumers(tmp_path)
    valid_consumers = (
        Path("tests/typecheck/agent_bridge_consumer.py"),
        Path("tests/typecheck/easyconfig_preset_consumer.py"),
        Path("tests/typecheck/session_event_consumer.py"),
        Path("tests/typecheck/voice_app_consumer.py"),
        Path("tests/typecheck/websocket_runtime_consumer.py"),
        Path("examples/twilio_app.py"),
        _render_twilio_server(tmp_path),
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--no-incremental",
            "--show-error-codes",
            *(str(path) for path in valid_consumers),
            *(str(path) for path in invalid_consumers),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=_MYPY_TIMEOUT_S,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    diagnostics = [line for line in output.splitlines() if ": error:" in line]
    unexpected = [
        line
        for line in diagnostics
        if not any(line.startswith(f"{path}:") for path in invalid_consumers)
    ]
    assert unexpected == [], output

    for path, expected_lines in invalid_consumers.items():
        reported_lines = {
            int(line.removeprefix(f"{path}:").split(":", 1)[0])
            for line in diagnostics
            if line.startswith(f"{path}:")
        }
        assert reported_lines == expected_lines, output

    assert 'incompatible type "Callable[[AgentFinal], None]"' in output
    assert 'expected "Callable[[STTFinal]' in output
    assert '"STTFinal" has no attribute "does_not_exist"' in output
    assert 'Unexpected keyword argument "agnet"' in output
    assert 'No overload variant of "run"' in output
    assert 'No overload variant of "serve"' in output
    assert 'Argument 1 to "session"' in output
    assert 'Argument "debug" to "browser"' in output
    assert 'Argument "caller_id_exposure" to "phone"' in output
