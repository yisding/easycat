#!/usr/bin/env python3
"""Import-smoke one optional extra inside a freshly synced environment.

Usage::

    uv run --no-sync python scripts/extras_smoke.py <extra>

For the extra named on the command line this script checks that:

1. every requirement of the extra whose environment marker matches the
   running interpreter is actually installed,
2. the top-level modules of those directly required distributions import,
3. the EasyCat provider adapters that declare ``required_extra == <extra>``
   in ``tests/contracts/provider_surface_matrix.py`` import and expose their
   adapter class.

Provider import targets are derived from the existing ``required_extra``
mapping in the provider surface matrix — never from a parallel list — so a
new provider row is smoke-covered automatically. An empty marker extra such
as ``cartesia`` installs nothing but still proves its adapters import.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Versioned extras install different majors of the same bridge SDK. The provider
# surface matrix uses the unsuffixed adapter row; every nightly cell installs
# exactly one extra into a fresh environment.
MATRIX_EXTRA_ALIASES: dict[str, str] = {
    "langchain-v0": "langchain",
    "pydantic-ai-v2": "pydantic-ai",
}


def extra_requirements(extra: str) -> list:
    """Marker-filtered ``packaging`` requirements declared by ``extra``."""
    # packaging ships with the dev group (pytest depends on it).
    from packaging.requirements import Requirement

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = pyproject["project"]["optional-dependencies"]
    if extra not in extras:
        raise SystemExit(f"unknown extra {extra!r}; declared extras: {sorted(extras)}")
    requirements = [Requirement(raw) for raw in extras[extra]]
    return [req for req in requirements if req.marker is None or req.marker.evaluate()]


def adapter_targets(extra: str) -> list[str]:
    """Dotted EasyCat adapter class paths whose provider row requires ``extra``."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from tests.contracts.provider_surface_matrix import PROVIDER_SURFACE_CONTRACTS

    matrix_extra = MATRIX_EXTRA_ALIASES.get(extra, extra)
    return sorted(
        {row.adapter for row in PROVIDER_SURFACE_CONTRACTS if row.required_extra == matrix_extra}
    )


def distribution_modules(distribution_names: Iterable[str]) -> list[str]:
    """Top-level modules provided by the named installed distributions."""
    from packaging.utils import canonicalize_name

    wanted = {canonicalize_name(name) for name in distribution_names}
    modules: set[str] = set()
    for module, dists in importlib.metadata.packages_distributions().items():
        if wanted & {canonicalize_name(dist) for dist in dists}:
            modules.add(module)
    return sorted(modules)


def _import_adapter(target: str) -> None:
    module_name, _, class_name = target.rpartition(".")
    module = importlib.import_module(module_name)
    getattr(module, class_name)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: extras_smoke.py <extra>", file=sys.stderr)
        return 2
    extra = argv[0]
    requirements = extra_requirements(extra)

    missing: list[str] = []
    for requirement in requirements:
        try:
            version = importlib.metadata.version(requirement.name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(requirement.name)
        else:
            print(f"installed: {requirement.name}=={version}")
    if missing:
        print(f"extras-smoke[{extra}]: requirements not installed: {missing}", file=sys.stderr)
        return 1

    failures: list[str] = []
    modules = ["easycat", *distribution_modules(req.name for req in requirements)]
    for module_name in dict.fromkeys(modules):
        try:
            importlib.import_module(module_name)
        # Surface any import-time breakage loudly.
        except Exception as error:  # noqa: BLE001 boundary
            failures.append(f"{module_name}: {error!r}")
        else:
            print(f"imported: {module_name}")
    for target in adapter_targets(extra):
        try:
            _import_adapter(target)
        except Exception as error:  # noqa: BLE001 intentional boundary or best-effort cleanup
            failures.append(f"{target}: {error!r}")
        else:
            print(f"imported adapter: {target}")

    if failures:
        print(f"extras-smoke[{extra}]: import failures:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(f"extras-smoke[{extra}]: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
