"""CPU resource detection for the Smart Turn ONNX runtime."""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

_MAX_INTRA_OP_THREADS = 4
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_SELF_CGROUP = Path("/proc/self/cgroup")


def _quota_cpu_count(quota: str, period: str) -> int | None:
    """Convert a cgroup CPU quota/period pair into schedulable CPU units."""
    if quota.strip() in {"max", "-1"}:
        return None
    try:
        quota_value = int(quota)
        period_value = int(period)
    except ValueError:
        return None
    if quota_value <= 0 or period_value <= 0:
        return None
    return max(1, math.ceil(quota_value / period_value))


def _cgroup_path(value: str) -> Path | None:
    """Return a safe path relative to a cgroup controller mount."""
    parts = Path(value.strip()).parts
    if any(part == ".." for part in parts):
        return None
    return Path(*(part for part in parts if part not in {"/", "."}))


def _current_cgroup_paths(cgroup_file: Path) -> tuple[Path | None, tuple[str, Path] | None]:
    """Read the process's unified and legacy CPU controller paths."""
    unified: Path | None = None
    legacy_cpu: tuple[str, Path] | None = None
    try:
        lines = cgroup_file.read_text().splitlines()
    except OSError:
        return unified, legacy_cpu

    for line in lines:
        try:
            _hierarchy, controllers_value, path_value = line.split(":", 2)
        except ValueError:
            continue
        path = _cgroup_path(path_value)
        if path is None:
            continue
        controllers = controllers_value.split(",") if controllers_value else []
        if not controllers:
            unified = path
        elif "cpu" in controllers:
            legacy_cpu = controllers_value, path
    return unified, legacy_cpu


def _cgroup_ancestors(root: Path, relative: Path | None) -> list[Path]:
    """List a process cgroup and its ancestors up to the controller root."""
    current = root / relative if relative is not None else root
    paths: list[Path] = []
    while True:
        paths.append(current)
        if current == root:
            return paths
        current = current.parent


def _quota_from_paths(paths: list[Path], quota_name: str, period_name: str) -> int | None:
    """Return the tightest CPU quota found along a cgroup hierarchy."""
    limits: list[int] = []
    for path in paths:
        try:
            quota = (path / quota_name).read_text()
            period = (path / period_name).read_text()
        except OSError:
            continue
        count = _quota_cpu_count(quota, period)
        if count is not None:
            limits.append(count)
    return min(limits, default=None)


def _cgroup_cpu_count(
    root: Path = _CGROUP_ROOT,
    cgroup_file: Path = _SELF_CGROUP,
) -> int | None:
    """Read the process's effective cgroup v2 or v1 CPU bandwidth limit."""
    unified, legacy_cpu = _current_cgroup_paths(cgroup_file)

    v2_limits: list[int] = []
    for path in _cgroup_ancestors(root, unified):
        try:
            quota, period = (path / "cpu.max").read_text().split()[:2]
        except (OSError, ValueError):
            continue
        count = _quota_cpu_count(quota, period)
        if count is not None:
            v2_limits.append(count)
    if v2_limits:
        return min(v2_limits)

    controller_name, legacy_path = legacy_cpu or ("cpu", None)
    controller_roots = (root / controller_name, root / "cpu", root)
    v1_limits = [
        limit
        for controller_root in dict.fromkeys(controller_roots)
        if (
            limit := _quota_from_paths(
                _cgroup_ancestors(controller_root, legacy_path),
                "cpu.cfs_quota_us",
                "cpu.cfs_period_us",
            )
        )
        is not None
    ]
    return min(v1_limits, default=None)


def _intra_op_thread_count(
    *,
    os_module: Any = os,
    cgroup_cpu_count: Callable[[], int | None] = _cgroup_cpu_count,
    max_threads: int = _MAX_INTRA_OP_THREADS,
) -> int:
    """Size ONNX's inference pool to the worker's available CPU set."""
    get_affinity = getattr(os_module, "sched_getaffinity", None)
    if get_affinity is not None:
        try:
            available = len(get_affinity(0))
        except OSError:
            available = os_module.cpu_count() or 1
    else:
        available = os_module.cpu_count() or 1
    quota_count = cgroup_cpu_count()
    if quota_count is not None:
        available = min(available, quota_count)
    return max(1, min(max_threads, available))
