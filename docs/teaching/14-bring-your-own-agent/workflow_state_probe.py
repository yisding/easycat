"""Inspect Chapter 14's explicit workflow-state boundary without providers.

This is an inspection probe, not an application API. It exercises the
provider-free goodbye path, then compares the public bridge snapshot with the
payload the bridge would store for an interruption artifact.

Run with::

    uv run python docs/teaching/14-bring-your-own-agent/workflow_state_probe.py
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from main import MyWorkflow

from easycat.integrations.agents import GenericWorkflowBridge
from easycat.integrations.agents.base import NULL_RECORDER
from easycat.session.actions import EndCallAction, SessionActions


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
