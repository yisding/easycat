"""Provider-free probe of SessionManager's registry and lifecycle contract."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from easycat import SessionManager


@dataclass
class ProbeSession:
    """Small duck-typed session used to expose manager behavior."""

    name: str
    fail_start: bool = False
    start_calls: int = 0
    stop_calls: int = 0

    async def start(self) -> None:
        self.start_calls += 1
        if self.fail_start:
            raise RuntimeError(f"{self.name} start failed")

    async def stop(self) -> None:
        self.stop_calls += 1


async def probe() -> dict[str, object]:
    manager: SessionManager[str] = SessionManager()
    alpha = ProbeSession("alpha")
    beta = ProbeSession("beta")
    duplicate = ProbeSession("duplicate")
    failed = ProbeSession("failed", fail_start=True)

    duplicate_error = ""
    failed_start_error = ""
    active_together = False
    failed_slot_released = False

    async with manager.connection("alpha", alpha):  # type: ignore[arg-type]
        async with manager.connection("beta", beta):  # type: ignore[arg-type]
            active_together = manager.get("alpha") is alpha and manager.get("beta") is beta

            try:
                await manager.add("alpha", duplicate)  # type: ignore[arg-type]
            except ValueError as exc:
                duplicate_error = str(exc)

            try:
                await manager.add("failed", failed)  # type: ignore[arg-type]
            except RuntimeError as exc:
                failed_start_error = str(exc)
            failed_slot_released = manager.get("failed") is None

    return {
        "active_together": active_together,
        "all_context_slots_released": (
            manager.get("alpha") is None and manager.get("beta") is None
        ),
        "duplicate_key_error": duplicate_error,
        "duplicate_start_calls": duplicate.start_calls,
        "failed_slot_released": failed_slot_released,
        "failed_start_error": failed_start_error,
        "start_calls": {
            "alpha": alpha.start_calls,
            "beta": beta.start_calls,
            "failed": failed.start_calls,
        },
        "stop_calls": {
            "alpha": alpha.stop_calls,
            "beta": beta.stop_calls,
            "failed": failed.stop_calls,
        },
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe()), indent=2, sort_keys=True))
