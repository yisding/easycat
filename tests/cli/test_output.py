"""Shared CLI output helper behavior."""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from easycat.cli import _output as output_module


def _plain_console() -> tuple[StringIO, Console]:
    stream = StringIO()
    return stream, Console(file=stream, force_terminal=False, no_color=True, width=120)


def test_status_helpers_render_message_text_literally(monkeypatch) -> None:
    stream, console = _plain_console()
    monkeypatch.setattr(output_module, "stderr_console", console)

    output_module.info("using easycat[openai-agents]")
    output_module.success("created demo[red]")
    output_module.warn("skipped [/]")

    text = stream.getvalue()
    assert "using easycat[openai-agents]" in text
    assert "created demo[red]" in text
    assert "skipped [/]" in text


def test_error_helper_renders_message_and_fix_text_literally(monkeypatch) -> None:
    stream, console = _plain_console()
    monkeypatch.setattr(output_module, "stderr_console", console)

    output_module.error(
        "EASYCAT_E202",
        "Missing optional extra: easycat[openai-agents]",
        fix="uv add 'easycat[openai-agents]'",
    )

    text = stream.getvalue()
    assert "Missing optional extra: easycat[openai-agents]" in text
    assert "uv add 'easycat[openai-agents]'" in text
    assert "easycat explain E202" in text


def test_emit_command_error_renders_message_text_literally() -> None:
    stream, console = _plain_console()

    output_module.emit_command_error(
        "init",
        "Unknown template: demo[red]",
        json_output=False,
        human_console=console,
    )

    assert "Unknown template: demo[red]" in stream.getvalue()
