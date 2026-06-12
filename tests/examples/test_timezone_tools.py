from __future__ import annotations

import ast
from pathlib import Path

from tests.examples._examples_helpers import REPO_ROOT

_TIME_TOOL_EXAMPLES = (
    "agent_event_subscription.py",
    "function_tools_langchain.py",
    "function_tools_langgraph.py",
    "function_tools_openai.py",
    "function_tools_pydantic.py",
)


def _except_handler_names(handler: ast.ExceptHandler) -> set[str]:
    if isinstance(handler.type, ast.Name):
        return {handler.type.id}
    if isinstance(handler.type, ast.Tuple):
        return {elt.id for elt in handler.type.elts if isinstance(elt, ast.Name)}
    return set()


def test_time_tool_examples_treat_malformed_timezones_as_unknown() -> None:
    missing: list[str] = []

    for example_name in _TIME_TOOL_EXAMPLES:
        path = REPO_ROOT / "examples" / example_name
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        catches_value_error = any(
            "ValueError" in _except_handler_names(handler)
            for node in ast.walk(module)
            if isinstance(node, ast.Try)
            for handler in node.handlers
            if "ZoneInfoNotFoundError" in _except_handler_names(handler)
        )
        if not catches_value_error:
            missing.append(str(Path("examples") / example_name))

    assert not missing, (
        "Timezone tools must catch ValueError from malformed ZoneInfo keys: " + ", ".join(missing)
    )
