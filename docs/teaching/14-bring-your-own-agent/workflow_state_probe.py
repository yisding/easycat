"""Inspect Chapter 14's explicit workflow-state boundary without providers.

This is an inspection probe, not an application API. It exercises the
provider-free goodbye path, then compares the public bridge snapshot with the
payload the bridge would store for an interruption artifact.

Run with::

    uv run python docs/teaching/14-bring-your-own-agent/workflow_state_probe.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from easycat.integrations.agents import GenericWorkflowBridge
from easycat.integrations.agents.base import NULL_RECORDER
from easycat.session.actions import EndCallAction, SessionActions

_MISSING = object()


def _load_workflow_class():
    """Load the lesson with a temporary stand-in for its optional SDK."""
    module_name = "easycat_teaching_chapter_14_main"
    spec = importlib.util.spec_from_file_location(module_name, Path(__file__).with_name("main.py"))
    if spec is None or spec.loader is None:  # pragma: no cover - importlib contract guard
        raise RuntimeError("could not load Chapter 14")
    module = importlib.util.module_from_spec(spec)
    previous_openai = sys.modules.get("openai", _MISSING)
    sys.modules["openai"] = SimpleNamespace(AsyncOpenAI=object)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
        if previous_openai is _MISSING:
            sys.modules.pop("openai", None)
        else:
            sys.modules["openai"] = previous_openai
    return module.MyWorkflow


MyWorkflow = _load_workflow_class()


async def probe() -> dict[str, object]:
    actions = SessionActions()
    # The goodbye branch never calls the client; this sentinel keeps the probe
    # provider-free while still exercising the chapter's real workflow class.
    workflow = MyWorkflow(SimpleNamespace(), actions)  # type: ignore[arg-type]
    bridge = GenericWorkflowBridge(workflow)

    chunks = [
        chunk
        async for chunk in workflow.on_user_turn(
            "goodbye",
            recorder=NULL_RECORDER,
            cancel_token=None,
        )
    ]
    snapshot = bridge.snapshot_state()
    artifact_payload = json.loads(bridge._serialize_framework_state())
    queued = actions.drain()
    assert len(queued) == 1 and isinstance(queued[0], EndCallAction)

    return {
        "reply": "".join(chunks),
        "action": {
            "type": queued[0].type.value,
            "reason": queued[0].reason,
        },
        "bridge_snapshot": snapshot.fields,
        "artifact_payload": artifact_payload,
    }


async def main() -> None:
    print(json.dumps(await probe(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
