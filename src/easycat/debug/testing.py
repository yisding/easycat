"""Fixture helpers for loading debug bundles in tests."""

from __future__ import annotations

from pathlib import Path

from easycat.debug.bundle import RunBundle


def load_bundle(path: str | Path, *, fast: bool = False) -> RunBundle:
    """Pytest helper to load a debug bundle."""
    return RunBundle.load(Path(path), fast=fast)
