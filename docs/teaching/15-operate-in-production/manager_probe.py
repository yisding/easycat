"""Provider-free probe of SessionManager's registry and lifecycle contract."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from dataclasses import dataclass, field

from easycat import SessionManager
from easycat.session_manager import SessionStopReport


@dataclass
class ProbeSession:
    """Small duck-typed session used to expose manager behavior."""

    name: str
    fail_start: bool = False
    fail_stop: bool = False
    start_calls: int = 0
    stop_calls: int = 0

    async def start(self) -> None:
        self.start_calls += 1
        if self.fail_start:
            raise RuntimeError(f"{self.name} start failed")

    async def stop(self) -> None:
        self.stop_calls += 1
        if self.fail_stop:
            raise RuntimeError(f"{self.name} stop failed")


@dataclass
class BlockingStartSession(ProbeSession):
    """Session whose start waits until the probe cancels its add task."""

    start_entered: asyncio.Event = field(default_factory=asyncio.Event)

    async def start(self) -> None:
        self.start_calls += 1
        self.start_entered.set()
        await asyncio.Event().wait()


async def _stop_all_with_captured_error(
    manager: SessionManager[str],
) -> tuple[SessionStopReport[str], str]:
    error_output = io.StringIO()
    manager_logger = logging.getLogger("easycat.session_manager")
    previous_handlers = manager_logger.handlers
    previous_level = manager_logger.level
    previous_propagate = manager_logger.propagate
    manager_logger.handlers = [logging.StreamHandler(error_output)]
    manager_logger.setLevel(logging.ERROR)
    manager_logger.propagate = False
    try:
        report = await manager.stop_all()
    finally:
        manager_logger.handlers = previous_handlers
        manager_logger.setLevel(previous_level)
        manager_logger.propagate = previous_propagate
    return report, error_output.getvalue().strip()


async def probe() -> dict[str, object]:
    manager: SessionManager[str] = SessionManager()
    alpha = ProbeSession("alpha")
    beta = ProbeSession("beta")
    duplicate = ProbeSession("duplicate")
    failed = ProbeSession("failed", fail_start=True)
    cancelled = BlockingStartSession("cancelled")
    replacement = ProbeSession("replacement")
    sweep_healthy = ProbeSession("sweep-healthy")
    sweep_failing = ProbeSession("sweep-failing", fail_stop=True)

    duplicate_error = ""
    failed_start_error = ""
    active_together = False
    failed_slot_released = False
    cancelled_start_error = ""

    async with manager.connection("alpha", alpha):  # type: ignore[arg-type]  # noqa: SIM117 nested scopes clarify setup and cleanup
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

    add_task = asyncio.create_task(
        manager.add("cancelled", cancelled)  # type: ignore[arg-type]
    )
    await cancelled.start_entered.wait()
    add_task.cancel()
    try:
        await add_task
    except asyncio.CancelledError as exc:
        cancelled_start_error = type(exc).__name__

    cancelled_slot_released = manager.get("cancelled") is None
    async with manager.connection("cancelled", replacement):  # type: ignore[arg-type]
        replacement_used_released_slot = manager.get("cancelled") is replacement

    await manager.add("sweep-healthy", sweep_healthy)  # type: ignore[arg-type]
    await manager.add("sweep-failing", sweep_failing)  # type: ignore[arg-type]
    stop_report, expected_stop_error = await _stop_all_with_captured_error(manager)

    return {
        "active_together": active_together,
        "all_context_slots_released": (
            manager.get("alpha") is None and manager.get("beta") is None
        ),
        "cancelled_start": {
            "cancelled_stop_calls": cancelled.stop_calls,
            "error": cancelled_start_error,
            "replacement_start_calls": replacement.start_calls,
            "replacement_stop_calls": replacement.stop_calls,
            "replacement_used_released_slot": replacement_used_released_slot,
            "slot_released": cancelled_slot_released,
        },
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
        "stop_all": {
            "expected_error": expected_stop_error,
            "failed_slot_retained": manager.get("sweep-failing") is sweep_failing,
            "healthy_slot_released": manager.get("sweep-healthy") is None,
            "report": {
                "attempted_keys": stop_report.attempted_keys,
                "failed_keys": stop_report.failed_keys,
                "failures": [
                    {"key": failure.key, "exception": str(failure.exception)}
                    for failure in stop_report.failures
                ],
                "ok": stop_report.ok,
                "stopped_keys": stop_report.stopped_keys,
            },
            "start_calls": {
                "sweep-failing": sweep_failing.start_calls,
                "sweep-healthy": sweep_healthy.start_calls,
            },
            "stop_calls": {
                "sweep-failing": sweep_failing.stop_calls,
                "sweep-healthy": sweep_healthy.stop_calls,
            },
        },
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe()), indent=2, sort_keys=True))
