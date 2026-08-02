"""Downstream type-checking contracts for EasyConfig preset ergonomics."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import fields
from pathlib import Path

import pytest

from easycat import EasyConfig
from easycat.config.easy import _EasyConfigPresetKwargs

_MYPY_TIMEOUT_S = 90


def test_preset_keyword_schema_tracks_every_easyconfig_constructor_field() -> None:
    """A new dataclass field must also become discoverable on every preset."""
    constructor_fields = {item.name for item in fields(EasyConfig) if item.init}
    typed_fields = set(_EasyConfigPresetKwargs.__required_keys__) | set(
        _EasyConfigPresetKwargs.__optional_keys__
    )

    assert _EasyConfigPresetKwargs.__required_keys__ == frozenset()
    assert typed_fields == constructor_fields


@pytest.mark.timeout(_MYPY_TIMEOUT_S + 10)
def test_preset_keywords_reject_typos_and_wrong_types_for_consumers(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid_easyconfig_presets.py"
    invalid.write_text(
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
        encoding="utf-8",
    )
    consumer = Path("tests/typecheck/easyconfig_preset_consumer.py")
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
    assert reported_lines == set(range(3, 15)), output
    assert 'Unexpected keyword argument "agnet"' in output
    assert 'Argument "debug" to "browser"' in output
    assert 'Argument "caller_id_exposure" to "phone"' in output
