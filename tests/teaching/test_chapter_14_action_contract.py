"""Keep Chapter 14's custom-action exercise executable."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from easycat import EasyConfig
from easycat.session.actions import CustomAction, SessionActionResult, SessionActions

ROOT = Path(__file__).resolve().parents[2]
EXERCISES = ROOT / "docs" / "teaching" / "14-bring-your-own-agent" / "EXERCISES.md"


def _custom_action_example() -> dict[str, Any]:
    exercises = EXERCISES.read_text(encoding="utf-8")
    section = exercises.split("## 2. Custom action with a custom executor", 1)[1]
    code = re.search(r"```python\n(?P<code>.*?)\n   ```", section, flags=re.DOTALL)
    assert code is not None

    source = "\n".join(line.removeprefix("   ") for line in code.group("code").splitlines())
    namespace: dict[str, Any] = {}
    exec(  # noqa: S102 trusted documentation example executed as a contract test
        compile(source, str(EXERCISES), "exec"), namespace
    )
    return namespace


@pytest.mark.asyncio
async def test_custom_action_example_uses_the_live_executor_contract(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "teaching-contract-test-key")
    example = _custom_action_example()

    config = example["config"]
    assert isinstance(config, EasyConfig)
    assert isinstance(config.session_actions, SessionActions)
    action = config.session_actions.drain()[0]
    assert isinstance(action, CustomAction)
    assert action.payload == {"freq": 440}

    executor = config.action_executors[0]
    assert executor.supports(action)
    result = await executor.execute(object(), action)

    assert isinstance(result, SessionActionResult)
    assert result.metadata == {"frequency": 440}
    assert capsys.readouterr().out == "BEEP at 440 Hz\n"


def test_custom_action_exercise_names_the_journal_lifecycle() -> None:
    exercises = EXERCISES.read_text(encoding="utf-8")

    assert "session_action.enqueued" not in exercises
    assert "session_action_executors" not in exercises
    for name in (
        "session_action_requested",
        "session_action_started",
        "session_action_completed",
        "session_action_failed",
    ):
        assert f"`{name}`" in exercises
